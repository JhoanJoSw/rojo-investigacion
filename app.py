#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Recolector de titulares para el estudio del término "rojo" (lingüística).
Busca titulares en medios digitales que contengan la palabra y los exporta a CSV.

Uso:
    python app.py
y luego abrir en el navegador:  http://127.0.0.1:8000

No requiere librerías externas (solo la biblioteca estándar de Python).
"""

import csv
import html
import io
import json
import re
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import capturas
import expreso

BASE_DIR = Path(__file__).resolve().parent
PUERTO = 8000

# ---------------------------------------------------------------------------
# Medios soportados. "larepublica" está activo; los demás son para la fase 2
# (su buscador aún no ha sido configurado).
# ---------------------------------------------------------------------------
MEDIOS = {
    "larepublica": {
        "nombre": "La República",
        "activo": True,
        "busqueda": "https://larepublica.pe/api/search/articles",
        "referencia": "https://larepublica.pe/buscar?buscar=rojo",
        "sitio": "https://larepublica.pe",
        "nota": "",
    },
    "expreso": {
        "nombre": "Expreso",
        "activo": True,
        "sitio": "https://www.expreso.com.pe",
        "nota": "Usa una ventana de navegador fuera de la pantalla (Cloudflare "
                "bloquea los navegadores ocultos). No la cierres mientras trabaja.",
    },
    "trome": {
        "nombre": "Trome",
        "activo": False,
        "nota": "Sitio reactivado (trome.com responde de nuevo; /edicion-impresa "
                "accesible). Búsqueda web por 'rojo' pendiente de reconexión "
                "(la API REST daba 404). Se reintentará cuando se pueda conectar "
                "su buscador.",
    },
    "larazon": {
        "nombre": "La Razón",
        "activo": False,
        "nota": "Búsqueda web sin conectar todavía (sí funciona su edición impresa).",
    },
}

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-PE,es;q=0.9",
    "Connection": "close",
}

# Variantes morfológicas de "rojo" que se pueden aceptar en el titular.
VARIANTES_RE = r"roj(?:o|a|os|as|ito|ita|itos|itas|izo|iza|ismo|ismos|ista|istas)"

ESPERA_ENTRE_PAGINAS = 0.4  # segundos; cortesía hacia el servidor del medio


# ---------------------------------------------------------------------------
# Lógica de recolección
# ---------------------------------------------------------------------------
def _headers(medio: str) -> dict:
    h = dict(HEADERS_BASE)
    ref = MEDIOS[medio].get("referencia")
    if ref:
        h["Referer"] = ref
    return h


def buscar_pagina(medio: str, palabra: str, pagina: int, por_pagina: int = 30):
    """Consulta una página del buscador del medio. Devuelve la lista de artículos."""
    cfg = MEDIOS[medio]
    qs = urllib.parse.urlencode(
        {
            "search": palabra,
            "limit": por_pagina,
            "page": pagina,
            "order_by": "update_date",
        }
    )
    req = urllib.request.Request(cfg["busqueda"] + "?" + qs, headers=_headers(medio))
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("articles") or {}).get("data") or []


def _seccion(articulo: dict) -> str:
    cats = (articulo.get("data") or {}).get("categories") or []
    for c in cats:
        if c.get("primary"):
            return c.get("name") or ""
    return cats[0].get("name", "") if cats else ""


def _etiquetas(articulo: dict) -> str:
    tags = (articulo.get("data") or {}).get("tags") or []
    return "; ".join(t.get("name", "") for t in tags if t.get("name"))


def _autores(articulo: dict) -> str:
    autores = (articulo.get("data") or {}).get("authors") or []
    return "; ".join(a.get("fullname", "") for a in autores if a.get("fullname"))


def _texto_wp(valor) -> str:
    """Limpia el HTML de WordPress (titulares/extractos vienen con etiquetas)."""
    if isinstance(valor, dict):
        valor = valor.get("rendered") or ""
    texto = re.sub(r"<[^>]+>", " ", str(valor or ""))
    texto = html.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


def normalizar(articulo: dict, medio: str) -> dict:
    """Convierte un artículo de la API al formato de fila de nuestro estudio."""
    cfg = MEDIOS[medio]

    if medio == "expreso":
        # Post crudo de la API REST de WordPress (con _seccion/_autor/_etiquetas
        # ya resueltos por expreso.py).
        return {
            "medio": cfg["nombre"],
            "fecha": (articulo.get("date") or "")[:16].replace("T", " "),
            "seccion": articulo.get("_seccion", ""),
            "titular": _texto_wp(articulo.get("title")),
            "bajada": _texto_wp(articulo.get("excerpt")),
            "url": articulo.get("link") or "",
            "autor": articulo.get("_autor", ""),
            "etiquetas": articulo.get("_etiquetas", ""),
        }

    slug = articulo.get("slug") or ""
    fecha = (articulo.get("update_date") or articulo.get("date") or "").strip()
    return {
        "medio": cfg["nombre"],
        "fecha": fecha[:16],
        "seccion": _seccion(articulo),
        "titular": (articulo.get("title") or "").strip(),
        "bajada": ((articulo.get("data") or {}).get("teaser") or "").strip(),
        "url": (cfg["sitio"] + slug) if slug.startswith("/") else slug,
        "autor": _autores(articulo),
        "etiquetas": _etiquetas(articulo),
    }


def contiene_palabra(titular: str, patron: "re.Pattern") -> bool:
    """True si el titular contiene la palabra (o variante) como ADJETIVO.

    Se aceptan solo usos adjetivales; coincidencias que empiezan con
    mayúscula (sustantivos o nombres propios, p. ej. "Rojas") se descartan.
    """
    for m in patron.finditer(titular or ""):
        if not m.group(0)[0].isupper():
            return True
    return False


def construir_patron(palabra: str, incluir_variantes: bool) -> "re.Pattern":
    base = re.escape(palabra.strip())
    if incluir_variantes and palabra.strip().lower() == "rojo":
        patron = VARIANTES_RE
    else:
        patron = base
    return re.compile(r"\b(?:%s)\b" % patron, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Servidor web
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- guardián: un fallo inesperado responde JSON en vez de cortar la conexión
    def do_GET(self):
        try:
            self._do_GET()
        except Exception as exc:  # noqa: BLE001
            self._fallo(exc)

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as exc:  # noqa: BLE001
            self._fallo(exc)

    def _fallo(self, exc):
        import traceback
        traceback.print_exc()
        try:
            self._json({"ok": False, "error": "Error interno: %s" % exc}, status=500)
        except Exception:  # noqa: BLE001
            pass

    # -- utilidades ---------------------------------------------------------
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, texto: str, status=200):
        body = texto.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silencia el log por consola
        pass

    # -- GET ---------------------------------------------------------------
    def _do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        ruta = parsed.path
        q = urllib.parse.parse_qs(parsed.query)

        def param(nombre, defecto=""):
            return (q.get(nombre) or [defecto])[0]

        if ruta in ("/", "/index.html"):
            archivo = BASE_DIR / "index.html"
            self._html(archivo.read_text(encoding="utf-8"))

        elif ruta == "/api/medios":
            self._json(
                {
                    "medios": [
                        {"id": k, "nombre": v["nombre"],
                         "activo": v.get("activo", False),
                         "nota": v.get("nota", "")}
                        for k, v in MEDIOS.items()
                    ]
                }
            )

        elif ruta == "/api/pagina":
            medio = param("medio", "larepublica")
            palabra = param("palabra", "rojo").strip()
            if not palabra:
                return self._json({"ok": False, "error": "Falta la palabra a buscar."})
            if medio not in MEDIOS or not MEDIOS[medio].get("activo"):
                return self._json({"ok": False, "error": "Medio no disponible todavía."})

            try:
                pagina = max(1, int(param("pagina", "1")))
            except ValueError:
                pagina = 1
            incluir_variantes = param("variantes", "0") == "1"
            desde = param("desde", "")  # YYYY-MM-DD, opcional
            hasta = param("hasta", "")

            try:
                if medio == "expreso":
                    articulos = expreso.buscar_pagina(
                        palabra, pagina, desde=desde, hasta=hasta,
                        variantes=incluir_variantes, por_pagina=100)
                else:
                    articulos = buscar_pagina(medio, palabra, pagina)
            except Exception as exc:  # noqa: BLE001 - se informa al usuario
                return self._json({"ok": False, "error": "No se pudo consultar el medio: %s" % exc})

            patron = construir_patron(palabra, incluir_variantes)
            filas = []
            fechas = []
            for art in articulos:
                fila = normalizar(art, medio)
                fecha_corta = fila["fecha"][:10]
                fechas.append(fecha_corta)
                if desde and fecha_corta and fecha_corta < desde:
                    continue
                if hasta and fecha_corta and fecha_corta > hasta:
                    continue
                if not contiene_palabra(fila["titular"], patron):
                    continue
                filas.append(fila)

            if medio == "expreso":
                # Cada página trae 100 posts de la API: menos de 100 (o llegar
                # a la última página) significa que no hay más.
                ultima = expreso.ULTIMA_META.get("paginas")
                fin = (not articulos) or len(articulos) < 100 or (
                    ultima is not None and pagina >= ultima)
            else:
                fin = not articulos
            motivo = "sin_mas_resultados" if fin else ""
            if not fin and desde and fechas and all(f and f < desde for f in fechas):
                fin, motivo = True, "fuera_de_rango"
            time.sleep(ESPERA_ENTRE_PAGINAS)
            respuesta = {"ok": True, "pagina": pagina, "items": filas,
                         "fin": fin, "motivo": motivo}
            if medio == "expreso":
                # La interfaz amplía su tope de páginas hasta llegar al final
                # del rango (cada página de Expreso trae 100 noticias crudas).
                tot = expreso.ULTIMA_META.get("paginas")
                if tot:
                    respuesta["paginas_totales"] = tot
            self._json(respuesta)

        elif ruta == "/api/avance-impresa":
            desde = (q.get("desde") or [""])[0]
            hasta = (q.get("hasta") or [""])[0]
            modo = (q.get("modo") or [""])[0] or None
            self._json({"ok": True, "avance": capturas.estado_impresa(desde, hasta, modo)})

        elif ruta == "/api/fichas":
            registros = capturas._leer_fichas()
            self._json({"ok": True, "capturas": registros})

        elif ruta == "/api/zip":
            try:
                contenido = capturas.generar_zip()
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": "No se pudo generar el ZIP: %s" % exc},
                                  status=500)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=\"capturas_rojo.zip\"")
            self.send_header("Content-Length", str(len(contenido)))
            self.end_headers()
            self.wfile.write(contenido)

        else:
            self._json({"ok": False, "error": "Ruta no encontrada"}, status=404)

    # -- POST --------------------------------------------------------------
    def _do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        ruta = parsed.path

        largo = int(self.headers.get("Content-Length") or 0)
        crudo = self.rfile.read(largo) if largo else b"{}"
        try:
            texto = crudo.decode("utf-8")
        except UnicodeDecodeError:
            texto = crudo.decode("latin-1", errors="replace")
        try:
            datos = json.loads(texto) if texto.strip() else {}
        except ValueError:
            return self._json({"ok": False, "error": "JSON inválido."}, status=400)

        # ---- CSV de los titulares
        if ruta == "/api/csv":
            filas = datos.get("items") or []
            nombre = (datos.get("nombre") or "titulares_rojo").strip() or "titulares_rojo"

            # BOM UTF-8 para que Excel reconozca los acentos correctamente.
            salida = io.StringIO()
            columnas = ["medio", "fecha", "seccion", "titular", "bajada", "url", "autor", "etiquetas"]
            escritor = csv.DictWriter(salida, fieldnames=columnas, quoting=csv.QUOTE_ALL)
            escritor.writeheader()
            for fila in filas:
                escritor.writerow({c: fila.get(c, "") for c in columnas})
            contenido = ("\ufeff" + salida.getvalue()).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=\"%s.csv\"" % nombre)
            self.send_header("Content-Length", str(len(contenido)))
            self.end_headers()
            self.wfile.write(contenido)
            return

        # ---- Capturas de las páginas web de los artículos
        if ruta == "/api/capturar-web":
            items = datos.get("items") or []
            if not items:
                return self._json({"ok": False, "error": "No hay titulares para capturar."})
            palabra = (datos.get("palabra") or "rojo").strip()
            for it in items:
                it["palabra"] = palabra
            try:
                resultado = capturas.capturar_web(items)
            except Exception as exc:  # noqa: BLE001
                resultado = {"ok": False, "error": "Fallo en la captura: %s" % exc}
            return self._json(resultado, status=200 if resultado.get("ok") else 500)

        # ---- Capturas de la edición impresa (La Razón) — tanda con retomada
        if ruta == "/api/capturar-impresa-tanda":
            desde = (datos.get("desde") or "").strip()
            hasta = (datos.get("hasta") or "").strip()
            palabra = (datos.get("palabra") or "rojo").strip()
            variantes = bool(datos.get("variantes", True))
            solo_portada = bool(datos.get("solo_portada", True))
            try:
                dias = max(1, min(90, int(datos.get("dias") or 30)))
            except (TypeError, ValueError):
                dias = 30
            if not desde or not hasta:
                return self._json({"ok": False, "error": "Faltan las fechas desde/hasta."})

            modo = "portada" if solo_portada else "todas"
            pendientes = capturas.fechas_pendientes(desde, hasta, dias, modo)
            if not pendientes:
                return self._json({"ok": True, "capturas": [], "errores": [],
                                   "avance": capturas.estado_impresa(desde, hasta, modo)})
            try:
                resultado = capturas.capturar_impresa_fechas(
                    pendientes, palabra, variantes, solo_portada)
            except Exception as exc:  # noqa: BLE001
                resultado = {"ok": False, "error": "Fallo en la captura: %s" % exc}
            if resultado.get("ok"):
                resultado["avance"] = capturas.estado_impresa(desde, hasta, modo)
            return self._json(resultado, status=200 if resultado.get("ok") else 500)

        # ---- Capturas de la edición impresa de La República (con credenciales) — tanda
        if ruta == "/api/capturar-impresa-larepublica-tanda":
            desde = (datos.get("desde") or "").strip()
            hasta = (datos.get("hasta") or "").strip()
            palabra = (datos.get("palabra") or "rojo").strip()
            solo_portada = bool(datos.get("solo_portada", True))
            try:
                dias = max(1, min(90, int(datos.get("dias") or 30)))
            except (TypeError, ValueError):
                dias = 30
            if not desde or not hasta:
                return self._json({"ok": False, "error": "Faltan las fechas desde/hasta."}, status=400)
            try:
                resultado = capturas.capturar_impresa_larepublica_tanda(
                    desde, hasta, palabra, solo_portada)
            except Exception as exc:  # noqa: BLE001
                resultado = {"ok": False, "error": "Fallo en la captura: %s" % exc}
            if resultado.get("ok"):
                resultado["avance"] = capturas.estado_impresa(desde, hasta, "portada" if solo_portada else "todas")
            return self._json(resultado, status=200 if resultado.get("ok") else 500)

        # ---- Capturas de la edición impresa (La Razón) — lote fijo (antiguo)
        if ruta == "/api/capturar-impresa":
            fechas = datos.get("fechas") or []
            palabra = (datos.get("palabra") or "rojo").strip()
            variantes = bool(datos.get("variantes", True))
            if not fechas:
                return self._json({"ok": False, "error": "No hay fechas para capturar."})
            try:
                resultado = capturas.capturar_impresa_fechas(fechas, palabra, variantes)
            except Exception as exc:  # noqa: BLE001
                resultado = {"ok": False, "error": "Fallo en la captura: %s" % exc}
            return self._json(resultado, status=200 if resultado.get("ok") else 500)

        # ---- Informe PDF con las capturas
        if ruta == "/api/informe-pdf":
            try:
                contenido = capturas.generar_informe_pdf()
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": "No se pudo generar: %s" % exc}, status=500)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", "attachment; filename=\"informe_capturas.pdf\"")
            self.send_header("Content-Length", str(len(contenido)))
            self.end_headers()
            self.wfile.write(contenido)
            return

        # ---- Borrar capturas
        if ruta == "/api/limpiar":
            n = capturas.limpiar_capturas()
            return self._json({"ok": True, "eliminados": n})

        # ---- Reiniciar solo el avance de fechas revisadas (conserva imágenes)
        if ruta == "/api/reiniciar-avance":
            n = capturas.reiniciar_avance()
            return self._json({"ok": True, "reiniciado": bool(n)})

        self._json({"ok": False, "error": "Ruta no encontrada"}, status=404)


def main():
    servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO), Handler)
    print("Recolector de titulares 'rojo' listo.")
    print("Abre en el navegador:  http://127.0.0.1:%d" % PUERTO)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")


if __name__ == "__main__":
    main()
