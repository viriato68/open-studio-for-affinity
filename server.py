"""
Open Studio for Affinity - local bridge between a web page and ComfyUI.

Only uses the Python standard library (no pip install needed).

  Browser  <-->  this server (http://127.0.0.1:8190)  <-->  ComfyUI (http://127.0.0.1:8188)

The server loads workflows/relleno.json (Expand) or workflows/editar.json (Edit),
changes the positive prompt, the seed and the Affinity layer name, queues it in
ComfyUI and follows its progress through the ComfyUI websocket.
"""

import base64
import copy
import json
import os
import random
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8190
COMFY_HOST = "127.0.0.1"
COMFY_PORT = 8188
COMFY_URL = f"http://{COMFY_HOST}:{COMFY_PORT}"

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = {
    "expand": os.path.join(ROOT, "workflows", "relleno.json"),
    "edit": os.path.join(ROOT, "workflows", "editar.json"),
}
MODE_LABEL = {"expand": "Expand", "edit": "Edit"}

# Rough share of the progress bar for each step of the workflow
SAMPLER_START, SAMPLER_END = 15, 90

job = {"running": False}
job_lock = threading.Lock()


class FriendlyError(Exception):
    """An error for the user: what happened, what to do, and optional technical detail."""

    def __init__(self, message, help="", detail=""):
        super().__init__(message)
        self.message = message
        self.help = help
        self.detail = detail


# ======================================================================
#  Minimal websocket client (enough to read ComfyUI progress messages)
# ======================================================================
class WebSocket:
    def __init__(self, host, port, path):
        self.sock = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("ComfyUI refused the websocket connection")
        # A long step (e.g. loading Flux) can be silent for minutes
        self.sock.settimeout(900)

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("ComfyUI closed the connection")
        self.buf += chunk

    def _read(self, n):
        while len(self.buf) < n:
            self._fill()
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _send(self, opcode, payload=b""):
        mask = os.urandom(4)
        header = bytes([0x80 | opcode, 0x80 | len(payload)])  # control frames are < 126 bytes
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def receive(self):
        """Return the next complete text message (str). Binary messages are skipped."""
        parts, kind = [], None
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("ComfyUI closed the connection")
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                kind, parts = opcode, [payload]
            else:  # continuation frame
                parts.append(payload)
            if fin:
                if kind == 0x1:
                    return b"".join(parts).decode("utf-8", "replace")
                parts, kind = [], None  # binary preview image: ignore

    def close(self):
        try:
            self._send(0x8)
            self.sock.close()
        except OSError:
            pass


# ======================================================================
#  ComfyUI helpers
# ======================================================================
def comfy_request(path, data=None, timeout=10):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(COMFY_URL + path, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def comfy_is_running():
    try:
        comfy_request("/system_stats", timeout=3)
        return True
    except Exception:
        return False


NOT_RUNNING = FriendlyError(
    "ComfyUI is not running.",
    "Start ComfyUI, wait until it says it is ready, then try again.",
)


def find_upstream(workflow, link, class_type):
    """Follow a node link upstream until a node of class_type is found."""
    seen = set()
    todo = [link]
    while todo:
        item = todo.pop()
        if not isinstance(item, list) or len(item) != 2:
            continue
        node_id = str(item[0])
        if node_id in seen or node_id not in workflow:
            continue
        seen.add(node_id)
        node = workflow[node_id]
        if node["class_type"] == class_type:
            return node
        todo.extend(v for v in node["inputs"].values() if isinstance(v, list))
    return None


def prepare_workflow(mode, prompt):
    with open(WORKFLOWS[mode], encoding="utf-8") as f:
        workflow = json.load(f)
    workflow = copy.deepcopy(workflow)

    samplers = [n for n in workflow.values() if n["class_type"] == "KSampler"]
    senders = [n for n in workflow.values() if n["class_type"] == "AffinityEnviarImagen"]
    if not samplers or not senders:
        raise FriendlyError("This workflow file looks different than expected.", "",
                            f"{os.path.basename(WORKFLOWS[mode])} needs a KSampler and an 'Affinity: Enviar imagen' node.")

    for sampler in samplers:
        sampler["inputs"]["seed"] = random.randint(0, 2**50)
        # Only the positive prompt changes; the negative one stays as saved
        text_node = find_upstream(workflow, sampler["inputs"]["positive"], "CLIPTextEncode")
        if text_node is None:
            raise FriendlyError("This workflow file looks different than expected.", "",
                                "Could not find the positive prompt node (CLIPTextEncode).")
        text_node["inputs"]["text"] = prompt

    layer = f"{MODE_LABEL[mode]}: {prompt}"
    if len(layer) > 60:
        layer = layer[:59].rstrip() + "…"
    for sender in senders:
        sender["inputs"]["nombre_capa"] = layer
    return workflow


def explain_queue_error(err_body, workflow):
    """Turn the ComfyUI 'prompt rejected' response into a friendly error."""
    try:
        data = json.loads(err_body)
    except ValueError:
        return FriendlyError("ComfyUI couldn't start this workflow.", "", err_body[:500])
    error = data.get("error", {}) if isinstance(data.get("error"), dict) else {}
    detail = json.dumps(data, ensure_ascii=False)[:1500]
    if error.get("type") == "missing_node_type":
        return FriendlyError("ComfyUI is missing a node this workflow needs.",
                             "If it's an Affinity node, restart ComfyUI.", error.get("message", ""))
    for node_id, info in (data.get("node_errors") or {}).items():
        for e in info.get("errors", []):
            if e.get("type") == "value_not_in_list":
                name = e.get("extra_info", {}).get("input_name", "")
                value = workflow.get(node_id, {}).get("inputs", {}).get(name, "")
                return FriendlyError(f"A model file is missing in ComfyUI: {value}",
                                     "Check that the model is in the right ComfyUI models folder.", detail)
    return FriendlyError("ComfyUI couldn't start this workflow.", "", detail)


def explain_run_error(data):
    """Turn a ComfyUI execution_error into a friendly error."""
    msg = str(data.get("exception_message", "")).strip()
    node = data.get("node_type", "")
    low = msg.lower()
    detail = f"{node}: {msg}" if node else msg
    if "no hay ningun documento" in low:
        return FriendlyError("No document is open in Affinity.",
                             "Open an image in Affinity, then try again.", detail)
    if "no puedo conectar con affinity" in low:
        return FriendlyError("Can't reach Affinity.",
                             "Make sure Affinity is open and 'Enable Affinity MCP' is turned on.", detail)
    if "not found" in low or "no ha escrito el archivo" in low or "no responde" in low \
            or "no ha respondido" in low or "documentacion interna" in low:
        return FriendlyError("Affinity got stuck.",
                             "Close and reopen Affinity, open your image again, then try again.", detail)
    if "out of memory" in low or "outofmemory" in low or "allocation" in low:
        return FriendlyError("The graphics card ran out of memory.",
                             "Close other heavy programs, or try a smaller image.", detail)
    if node.startswith("Affinity"):
        return FriendlyError("Something went wrong while talking to Affinity.",
                             "Try again. If it keeps failing, restart Affinity.", detail)
    return FriendlyError("The AI step failed in ComfyUI.", "", detail)


# ======================================================================
#  Job runner
# ======================================================================
def set_job(**fields):
    with job_lock:
        job.update(fields)


def run_job(mode, prompt):
    ws = None
    try:
        workflow = prepare_workflow(mode, prompt)
        client_id = uuid.uuid4().hex
        try:
            # Connect first so no progress message is missed
            ws = WebSocket(COMFY_HOST, COMFY_PORT, f"/ws?clientId={client_id}")
        except OSError:
            raise NOT_RUNNING

        try:
            queued = comfy_request("/prompt", {"prompt": workflow, "client_id": client_id})
        except urllib.error.HTTPError as e:
            raise explain_queue_error(e.read().decode("utf-8", "replace"), workflow)
        except OSError:
            raise NOT_RUNNING
        prompt_id = queued["prompt_id"]
        set_job(stage="Waiting for ComfyUI…", percent=0)

        sampler_ids = {k for k, n in workflow.items() if n["class_type"] == "KSampler"}
        done_samplers = 0
        while True:
            try:
                msg = json.loads(ws.receive())
            except ValueError:
                continue
            except OSError:
                raise FriendlyError("ComfyUI stopped while working.",
                                    "ComfyUI may have closed or crashed. Check its window, restart it if needed, and try again.")
            kind, data = msg.get("type"), msg.get("data") or {}
            if data.get("prompt_id") not in (None, prompt_id):
                continue

            if kind == "status":
                remaining = data.get("status", {}).get("exec_info", {}).get("queue_remaining", 0)
                if job.get("stage") == "Waiting for ComfyUI…" and remaining > 1:
                    set_job(stage="Waiting in the ComfyUI queue…")
            elif kind == "executing":
                node_id = data.get("node")
                if node_id is None:
                    break
                cls = workflow.get(str(node_id), {}).get("class_type", "")
                if cls == "AffinityCogerImagen":
                    set_job(stage="Getting image from Affinity…", percent=3)
                elif cls == "AffinityEnviarImagen":
                    set_job(stage="Sending the result to Affinity…", percent=96)
                elif str(node_id) not in sampler_ids:
                    if job.get("percent", 0) < SAMPLER_START:
                        set_job(stage="AI working…", percent=max(job.get("percent", 0), 8))
                    elif job.get("percent", 0) >= SAMPLER_END:
                        set_job(stage="AI working…", percent=min(95, job["percent"] + 1))
            elif kind == "progress" and str(data.get("node")) in sampler_ids:
                value, maximum = data.get("value", 0), data.get("max", 1) or 1
                share = (SAMPLER_END - SAMPLER_START) / len(sampler_ids)
                pct = SAMPLER_START + share * (done_samplers + value / maximum)
                if value >= maximum:
                    done_samplers += 1
                set_job(stage="AI working…", percent=int(pct))
            elif kind == "execution_error":
                raise explain_run_error(data)
            elif kind == "execution_interrupted":
                raise FriendlyError("The job was cancelled in ComfyUI.")
            elif kind == "execution_success":
                break

        set_job(running=False, done=True, stage="Done! Check the new layer in Affinity", percent=100)
    except FriendlyError as e:
        set_job(running=False, done=True, error=e.message, help=e.help, detail=e.detail)
    except Exception as e:  # anything unexpected: still show something readable
        set_job(running=False, done=True, error="Something unexpected went wrong.",
                help="Try again. If it keeps failing, restart ComfyUI and Affinity.", detail=repr(e))
    finally:
        if ws:
            ws.close()


# ======================================================================
#  Web server
# ======================================================================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the console window quiet

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/health":
            self.send_json({"comfy": comfy_is_running()})
        elif self.path == "/api/status":
            with job_lock:
                self.send_json(dict(job))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/generate":
            return self.send_error(404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return self.send_json({"error": "Bad request."}, 400)
        mode = data.get("mode")
        prompt = " ".join(str(data.get("prompt", "")).split())
        if mode not in WORKFLOWS:
            return self.send_json({"error": "Choose Edit or Expand first."}, 400)
        if not prompt:
            return self.send_json({"error": "Write a prompt first."}, 400)
        with job_lock:
            if job.get("running"):
                return self.send_json({"error": "Already working on something. Please wait."}, 409)
            job.clear()
            job.update(running=True, done=False, mode=mode, stage="Starting…",
                       percent=0, started=time.time())
        threading.Thread(target=run_job, args=(mode, prompt), daemon=True).start()
        self.send_json({"ok": True})


def main():
    url = f"http://{HOST}:{PORT}"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print("Open Studio for Affinity is already running. Opening it in the browser...")
        webbrowser.open(url)
        return
    print("Open Studio for Affinity is running at " + url)
    print("Keep this window open while you use it. Close it to stop the app.")
    if not comfy_is_running():
        print("Note: ComfyUI does not seem to be running yet (" + COMFY_URL + ").")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
