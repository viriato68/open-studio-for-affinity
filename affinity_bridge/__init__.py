"""
Affinity Bridge para ComfyUI  (Maquina Creativa)  v0.2

Dos nodos:
  - "Affinity: Coger imagen"  -> pide a Affinity el documento abierto en PNG
                                 a resolucion completa y lo entrega a ComfyUI.
  - "Affinity: Enviar imagen" -> guarda la imagen de ComfyUI en PNG y la mete
                                 en Affinity como capa nueva.

Comunicacion: servidor MCP de Affinity (http://localhost:6767/sse).
Intercambio de archivos: Escritorio\\ComfyAffinity (Affinity solo puede
leer y escribir en el Escritorio).
Requisitos en Affinity: MCP activado + permiso de archivos activado.
"""

import json
import os
import queue
import threading
import time
import urllib.request

import numpy as np
import torch
from PIL import Image

BASE = "http://localhost:6767"
CARPETA = "ComfyAffinity"


# ======================================================================
#  Cliente MCP minimo para hablar con Affinity
# ======================================================================
class AffinityMCP:
    def __init__(self):
        self.mensajes = queue.Queue()
        self.endpoint = None
        self.resp = None
        self.contador = 0

    # --- conexion -----------------------------------------------------
    def __enter__(self):
        try:
            self.resp = urllib.request.urlopen(BASE + "/sse", timeout=30)
        except Exception as e:
            raise RuntimeError(
                "No puedo conectar con Affinity. Comprueba que Affinity esta "
                "abierto y que 'Habilitar MCP de Affinity' esta activado. "
                f"Detalle: {e}"
            )
        threading.Thread(target=self._escuchar, daemon=True).start()
        for _ in range(100):
            if self.endpoint:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("Affinity no responde (no llega el endpoint MCP).")
        self._llamar("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "affinity-bridge-comfyui", "version": "0.1"},
        })
        self._enviar({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # Affinity exige leer el preamble antes de ejecutar scripts.
        # A veces falla de forma pasajera ("File not found"): reintentar.
        intentos = 5
        for intento in range(1, intentos + 1):
            try:
                self.herramienta("read_sdk_documentation_topic", {"filename": "preamble"})
                break
            except RuntimeError as e:
                if intento == intentos:
                    raise RuntimeError(
                        "Affinity no puede leer su documentacion interna tras varios "
                        f"intentos. Suele arreglarse reiniciando Affinity. Detalle: {e}"
                    )
                print(f"[Affinity Bridge] Fallo al leer el preamble ({e}). Reintento {intento + 1}/{intentos}...")
                time.sleep(intento)
        return self

    def __exit__(self, *args):
        try:
            if self.resp:
                self.resp.close()
        except Exception:
            pass

    def _escuchar(self):
        evento = None
        try:
            while True:
                linea = self.resp.readline()
                if not linea:
                    break
                linea = linea.decode("utf-8").rstrip("\r\n")
                if linea.startswith("event:"):
                    evento = linea[6:].strip()
                elif linea.startswith("data:"):
                    dato = linea[5:].strip()
                    if evento == "endpoint":
                        self.endpoint = BASE + dato
                    elif evento == "message":
                        try:
                            self.mensajes.put(json.loads(dato))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass  # la conexion se cierra al terminar; es normal

    def _enviar(self, msg):
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(msg).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=30).read()

    def _llamar(self, metodo, params=None, espera=300):
        self.contador += 1
        id_ = self.contador
        self._enviar({"jsonrpc": "2.0", "id": id_, "method": metodo, "params": params or {}})
        limite = time.time() + espera
        while time.time() < limite:
            try:
                m = self.mensajes.get(timeout=1)
            except queue.Empty:
                continue
            if m.get("id") == id_:
                return m
        raise RuntimeError(f"Affinity no ha respondido a tiempo ({metodo}).")

    def herramienta(self, nombre, argumentos):
        r = self._llamar("tools/call", {"name": nombre, "arguments": argumentos})
        if "error" in r:
            raise RuntimeError(f"Error de Affinity: {r['error']}")
        res = r.get("result", {})
        texto = "".join(p.get("text", "") for p in res.get("content", []) if p.get("type") == "text")
        if res.get("isError"):
            raise RuntimeError(f"Error en el script de Affinity: {texto}")
        return texto

    def js(self, codigo):
        return self.herramienta("execute_script", {"script": codigo})


def leer_json(salida):
    for linea in salida.splitlines():
        linea = linea.strip()
        if linea.startswith("{"):
            try:
                return json.loads(linea)
            except json.JSONDecodeError:
                pass
    raise RuntimeError(f"Respuesta inesperada de Affinity: {salida[:300]}")


def carpeta_intercambio(mcp):
    salida = mcp.js("""
const { Application } = require('/application.js');
const { Document } = require('/document.js');
const doc = Document.current;
const info = { escritorio: Application.userDesktopPath, hayDocumento: !!doc };
if (doc) { info.ancho = Math.round(doc.widthPixels); info.alto = Math.round(doc.heightPixels); }
console.log(JSON.stringify(info));
""")
    info = leer_json(salida)
    if not info.get("hayDocumento"):
        raise RuntimeError("No hay ningun documento abierto en Affinity. Abre una imagen primero.")
    carpeta = os.path.join(info["escritorio"], CARPETA)
    os.makedirs(carpeta, exist_ok=True)
    return carpeta, (info["ancho"], info["alto"])


def esperar_archivo(ruta, segundos=60):
    limite = time.time() + segundos
    tam_anterior = -1
    while time.time() < limite:
        if os.path.exists(ruta):
            tam = os.path.getsize(ruta)
            if tam > 0 and tam == tam_anterior:
                return
            tam_anterior = tam
        time.sleep(0.3)
    raise RuntimeError(f"Affinity no ha escrito el archivo a tiempo: {ruta}")


# ======================================================================
#  Nodo 1: Affinity -> ComfyUI
# ======================================================================
class AffinityCogerImagen:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "lado_maximo": ("INT", {"default": 1024, "min": 0, "max": 8192, "step": 64,
                                        "tooltip": "Reduce la imagen para la IA. 0 = tamano original."}),
            }
        }

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("imagen", "mascara_alfa")
    FUNCTION = "coger"
    CATEGORY = "Affinity"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # Siempre volver a pedir la imagen: el documento puede haber cambiado
        return float("nan")

    def coger(self, lado_maximo):
        with AffinityMCP() as mcp:
            carpeta, _ = carpeta_intercambio(mcp)
            ruta = os.path.join(carpeta, "affinity_out.png")
            if os.path.exists(ruta):
                os.remove(ruta)
            mcp.js(f"""
const {{ Document, FileExportOptions }} = require('/document.js');
const doc = Document.current;
doc.export({json.dumps(ruta)}, FileExportOptions.createWithPresetName("PNG"), null, null);
console.log("ok");
""")
            esperar_archivo(ruta)

        img = Image.open(ruta)
        img.load()
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        original = img.size
        if lado_maximo and max(img.size) > lado_maximo:
            escala = lado_maximo / max(img.size)
            # multiplos de 8, que es lo que necesitan los modelos
            nuevo = (max(8, int(img.width * escala) // 8 * 8), max(8, int(img.height * escala) // 8 * 8))
            img = img.resize(nuevo, Image.LANCZOS)
        arr = np.array(img).astype(np.float32) / 255.0
        imagen = torch.from_numpy(arr[:, :, :3])[None, ...]
        # Mismo criterio que el nodo LoadImage de ComfyUI: mascara = 1 - alfa
        mascara = 1.0 - torch.from_numpy(arr[:, :, 3])[None, ...]
        print(f"[Affinity Bridge] Imagen recibida: {original[0]} x {original[1]} px -> para la IA: {img.width} x {img.height} px")
        return (imagen, mascara)


# ======================================================================
#  Nodo 2: ComfyUI -> Affinity
# ======================================================================
class AffinityEnviarImagen:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "imagen": ("IMAGE",),
                "nombre_capa": ("STRING", {"default": "ComfyUI"}),
                "ajustar_al_documento": ("BOOLEAN", {"default": True,
                                         "tooltip": "Amplia o reduce la imagen al tamano exacto del documento de Affinity."}),
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "enviar"
    OUTPUT_NODE = True
    CATEGORY = "Affinity"

    def enviar(self, imagen, nombre_capa, ajustar_al_documento):
        with AffinityMCP() as mcp:
            carpeta, tam_doc = carpeta_intercambio(mcp)
            marca = time.strftime("%Y%m%d_%H%M%S")
            total = imagen.shape[0]
            for i in range(total):
                arr = (imagen[i].cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
                ruta = os.path.join(carpeta, f"comfy_{marca}_{i + 1}.png")
                pil = Image.fromarray(arr)
                if ajustar_al_documento and pil.size != tam_doc:
                    pil = pil.resize(tam_doc, Image.LANCZOS)
                pil.save(ruta, compress_level=4)
                nombre = nombre_capa if total == 1 else f"{nombre_capa} {i + 1}"
                salida = mcp.js(f"""
const {{ Document }} = require('/document.js');
const {{ ImageNodeDefinition }} = require('/nodes.js');
const {{ Bitmap, RasterFormat }} = require('/rasterobject.js');
const doc = Document.current;
const bmp = Bitmap.loadFromFile({json.dumps(ruta)}, RasterFormat.RGBA8);
const def = ImageNodeDefinition.create(RasterFormat.RGBA8);
def.bitmap = bmp;
def.userDescription = {json.dumps(nombre)};
doc.addNode(def);
console.log("capa anadida: " + bmp.width + " x " + bmp.height);
""")
                print(f"[Affinity Bridge] {salida.strip()}  ({nombre})")
        return {}


NODE_CLASS_MAPPINGS = {
    "AffinityCogerImagen": AffinityCogerImagen,
    "AffinityEnviarImagen": AffinityEnviarImagen,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "AffinityCogerImagen": "Affinity: Coger imagen",
    "AffinityEnviarImagen": "Affinity: Enviar imagen",
}
