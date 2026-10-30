#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Módulo de capturas para el estudio del término "rojo".

Dos tipos de captura:
  1. web     -> captura de pantalla de la página del artículo (el titular tal como
                se publica en la web del periódico).
  2. impresa -> descarga de la edición impresa (PDF) de La Razón, localiza la página
                donde aparece la palabra y la convierte en imagen con la palabra
                resaltada (tal como se ve en el diario en papel).

Salida: PNG en capturas/web y capturas/impresa, más un índice (fichas.json).
"""

import csv
import io
import json
import re
import threading
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pymupdf

BASE = Path(__file__).resolve().parent
CARPETA = BASE / "capturas"
CARPETA_WEB = CARPETA / "web"
CARPETA_IMPRESA = CARPETA / "impresa"
FICHAS = CARPETA / "fichas.json"
# Avance de la edición impresa: qué fechas ya se revisaron (para retomar después)
PROCESADAS = CARPETA / "procesadas.json"

# Edicion impresa de La Republica (suscripcion S/5 + captura de imagen via CDN).
# Flujo: web de la edicion (impreso.larepublica.pe) con Playwright visible -> login
# con credenciales (email+pass) -> para cada fecha abrir la edicion y capturar la
# imagen del CDN que contiene la palabra (por defecto la portada).
LAREPUBLICA_HOME = "https://impreso.larepublica.pe/larepublica"
LAREPUBLICA_ARCHIVE = "https://impreso.larepublica.pe/larepublica/impresa-archive/larepublica-norte/{}"
LAREPUBLICA_EMAIL = "fjhoan0604@gmail.com"
LAREPUBLICA_PASS = "Software20"

# Evita que dos capturas se ejecuten a la vez (Playwright no admite uso concurrente).
BLOQUEO = threading.Lock()

VARIANTES = r"roj(?:o|a|os|as|ito|ita|itos|itas|izo|iza|ismo|ismos|ista|istas)"

# Convenciones observadas en el archivo de La Razón (¡hay varias!):
#   La_Razon_21-10-2026.pdf · la_razon_24-10-26.pdf · Diario-Impreso-La-Razon14-01-2023.pdf
#   y páginas edicion-impresa-… / ediciones-impresas-… con año de 4 o 2 dígitos.
def _variantes_fecha(fecha_iso: str):
    d = date.fromisoformat(fecha_iso)
    f4 = d.strftime("%d-%m-%Y")
    f2 = d.strftime("%d-%m-%y")
    anio, mes = d.year, d.month
    pdfs = [
        "https://larazon.pe/wp-content/uploads/{a}/{m:02d}/La_Razon_{f}.pdf".format(a=anio, m=mes, f=f4),
        "https://larazon.pe/wp-content/uploads/{a}/{m:02d}/la_razon_{f}.pdf".format(a=anio, m=mes, f=f4),
        "https://larazon.pe/wp-content/uploads/{a}/{m:02d}/La_Razon_{f}.pdf".format(a=anio, m=mes, f=f2),
        "https://larazon.pe/wp-content/uploads/{a}/{m:02d}/la_razon_{f}.pdf".format(a=anio, m=mes, f=f2),
        "https://larazon.pe/wp-content/uploads/{a}/{m:02d}/Diario-Impreso-La-Razon{f}.pdf".format(a=anio, m=mes, f=f4),
    ]
    paginas = [
        "https://larazon.pe/edicion_impresa/ediciones-impresas-{f}/".format(f=f4),
        "https://larazon.pe/edicion_impresa/edicion-impresa-{f}/".format(f=f4),
        "https://larazon.pe/edicion_impresa/ediciones-impresas-{f}/".format(f=f2),
        "https://larazon.pe/edicion_impresa/ediciones-impresas-{f}-2/".format(f=f4),
    ]
    return pdfs, paginas


def _resolver_pdf(contexto, fecha_iso: str):
    """Busca el PDF de la edición impresa de esa fecha.

    Devuelve (url, estado) donde estado es:
      "ok"          -> se encontró el PDF
      "sin_edicion" -> el archivo responde que esa fecha no existe (definitivo)
      "pendiente"   -> falló la conexión o el PDF aún no está publicado (reintentar)
    """
    pdfs, paginas = _variantes_fecha(fecha_iso)

    for url in pdfs:
        try:
            r = contexto.request.head(url, timeout=20000, fail_on_status_code=False)
            if r.status == 200:
                return url, "ok"
        except Exception:  # noqa: BLE001
            continue

    estados = []
    for url in paginas:
        pagina = contexto.new_page()
        try:
            resp = pagina.goto(url, timeout=45000, wait_until="domcontentloaded")
            status = resp.status if resp else 0
            estados.append(status)
            if status == 404:
                continue
            pagina.wait_for_timeout(2200)
            fuentes = pagina.eval_on_selector_all(
                "[source]", "els => els.map(e => e.getAttribute('source'))")
            pdfs_en = [s for s in fuentes
                       if s and s.lower().endswith(".pdf") and "razon" in s.split("/")[-1].lower()]
            if pdfs_en:
                return pdfs_en[0], "ok"
        except Exception:  # noqa: BLE001
            estados.append(0)
        finally:
            try:
                pagina.close()
            except Exception:  # noqa: BLE001
                pass

    if estados and all(s == 404 for s in estados):
        return None, "sin_edicion"
    return None, "pendiente"


def preparar_carpetas():
    for c in (CARPETA_WEB, CARPETA_IMPRESA):
        c.mkdir(parents=True, exist_ok=True)


def patron_palabra(palabra: str, incluir_variantes: bool = True) -> re.Pattern:
    p = (palabra or "").strip()
    base = VARIANTES if (incluir_variantes and p.lower() == "rojo") else re.escape(p)
    return re.compile(r"\b(?:%s)\b" % base, re.IGNORECASE)


def tiene_palabra_adjetiva(texto: str, patron: re.Pattern) -> bool:
    """True si el texto contiene la palabra como ADJETIVO (no como sustantivo/nombre)."""
    for m in patron.finditer(texto or ""):
        if not m.group(0)[0].isupper():
            return True
    return False


def _leer_fichas() -> list:
    if FICHAS.exists():
        try:
            return json.loads(FICHAS.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
    return []


def _guardar_fichas(registros: list):
    preparar_carpetas()
    FICHAS.write_text(json.dumps(registros, ensure_ascii=False, indent=2), encoding="utf-8")


def agregar_fichas(nuevas: list) -> list:
    """Añade registros; si uno repite un archivo ya guardado, lo reemplaza (sin duplicar)."""
    registros = _leer_fichas()
    por_archivo = {r.get("archivo"): i for i, r in enumerate(registros) if r.get("archivo")}
    for nueva in nuevas:
        clave = nueva.get("archivo")
        if clave in por_archivo:
            registros[por_archivo[clave]] = nueva
        else:
            registros.append(nueva)
            if clave:
                por_archivo[clave] = len(registros) - 1
    _guardar_fichas(registros)
    return registros


def limpiar_capturas() -> int:
    """Borra todas las capturas y el avance. Devuelve cuántos archivos eliminó."""
    if not CARPETA.exists():
        return 0
    n = 0
    for f in CARPETA.rglob("*"):
        if f.is_file():
            f.unlink()
            n += 1
    if FICHAS.exists():
        FICHAS.unlink()
    if PROCESADAS.exists():
        PROCESADAS.unlink()
    return n


# ---------------------------------------------------------------------------
# Avance de la edición impresa (retomar donde se quedó)
# ---------------------------------------------------------------------------
def _leer_procesadas() -> dict:
    if PROCESADAS.exists():
        try:
            return json.loads(PROCESADAS.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _guardar_procesadas(datos: dict):
    preparar_carpetas()
    PROCESADAS.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")


def marcar_procesada(fecha_iso: str, capturadas: int = 0, pdf: str = "", nota: str = ""):
    datos = _leer_procesadas()
    datos[fecha_iso] = {
        "capturadas": capturadas,
        "pdf": pdf,
        "nota": nota,
        "procesada": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _guardar_procesadas(datos)


def _cubierta(reg, modo) -> bool:
    """¿Ya quedó cubierta esta fecha en el modo pedido?"""
    if reg is None:
        return False
    nota = reg.get("nota", "")
    if nota == "sin_edicion":
        return True                     # no hay edición: hecha en cualquier modo
    if modo is None or nota not in ("portada", "todas"):
        return True                     # registros antiguos sin modo: se dan por hechos
    if nota == "todas":
        return True                     # "todas" ya incluye la portada
    return modo == "portada"            # "portada" solo cubre la primera página


def fechas_pendientes(desde: str, hasta: str, limite: int = 0, modo=None) -> list:
    """Fechas del rango que todavía no se han revisado (de más reciente a más antigua)."""
    proc = _leer_procesadas()
    pendientes = [f for f in fechas_entre(desde, hasta)
                  if not _cubierta(proc.get(f), modo)]
    return pendientes[:limite] if limite else pendientes


def estado_impresa(desde: str, hasta: str, modo=None) -> dict:
    """Resumen del avance dentro del rango elegido."""
    todas = fechas_entre(desde, hasta)
    proc = _leer_procesadas()
    pendientes = fechas_pendientes(desde, hasta, 0, modo)
    hechas = [f for f in todas if f not in set(pendientes)]
    return {
        "total": len(todas),
        "hechas": len(hechas),
        "restantes": len(pendientes),
        "capturadas": sum(int(proc[f].get("capturadas", 0)) for f in todas if f in proc),
        "proxima": pendientes[0] if pendientes else None,
        "ultima": hechas[0] if hechas else None,
    }


def reiniciar_avance() -> int:
    """Borra solo el registro de fechas procesadas (mantiene las imágenes)."""
    if PROCESADAS.exists():
        PROCESADAS.unlink()
        return 1
    return 0


def _nombre_seguro(texto: str) -> str:
    texto = re.sub(r"[^\wáéíóúüñÁÉÍÓÚÑ\- ]", "", texto or "").strip()
    texto = re.sub(r"\s+", "_", texto)
    return texto[:60] or "sin_titulo"


# ---------------------------------------------------------------------------
# 1) Captura web de la página del artículo
# ---------------------------------------------------------------------------
def _capturar_pagina(contexto, fila: dict, destino: Path) -> dict:
    """Abre la URL y guarda una imagen con la zona del titular."""
    pagina = contexto.new_page()
    resultado = {"url": fila.get("url", ""), "ok": False}
    try:
        pagina.set_viewport_size({"width": 1366, "height": 1000})
        resp = pagina.goto(fila["url"], timeout=45000, wait_until="domcontentloaded")
        resultado["http"] = resp.status if resp else None
        pagina.wait_for_timeout(1800)

        # Cierra avisos de cookies si aparecen
        for sel in (
            "#onetrust-accept-btn-handler",
            'button:has-text("Aceptar")',
            'button:has-text("Acepto")',
            'button:has-text("同意")',
            '[aria-label="Aceptar cookies"]',
        ):
            try:
                pagina.locator(sel).first.click(timeout=700)
                break
            except Exception:  # noqa: BLE001
                continue

        pagina.evaluate("window.scrollTo(0, 0)")
        pagina.wait_for_timeout(400)

        # Busca el titular (h1)
        caja = None
        try:
            h1 = pagina.locator("h1").first
            if h1.count():
                caja = h1.bounding_box()
        except Exception:  # noqa: BLE001
            caja = None

        if caja and (caja["y"] + caja["height"]) <= 960:
            # El titular ya se ve arriba: captura la cabecera + titular
            clip = {"x": 0, "y": 0, "width": 1366, "height": 1000}
        elif caja:
            # El titular está más abajo: lo centramos en la captura
            try:
                pagina.locator("h1").first.scroll_into_view_if_needed(timeout=3000)
                pagina.wait_for_timeout(500)
                caja = pagina.locator("h1").first.bounding_box()
            except Exception:  # noqa: BLE001
                caja = None
            if caja:
                y = max(0, caja["y"] - 240)
                alto = min(1000 - y, (caja["y"] - y) + caja["height"] + 200)
                clip = {"x": 0, "y": y, "width": 1366, "height": max(300, alto)}
            else:
                clip = {"x": 0, "y": 0, "width": 1366, "height": 900}
        else:
            clip = {"x": 0, "y": 0, "width": 1366, "height": 900}

        pagina.screenshot(path=str(destino), clip=clip)
        resultado["ok"] = destino.exists()
        resultado["archivo"] = str(destino.relative_to(CARPETA))
    except Exception as exc:  # noqa: BLE001
        resultado["error"] = str(exc).splitlines()[0][:180]
    finally:
        try:
            pagina.close()
        except Exception:  # noqa: BLE001
            pass
    return resultado


def _ficha_web(fila: dict) -> dict:
    """Campos comunes de la ficha de una captura web."""
    return {
        "tipo": "web",
        "medio": fila.get("medio", ""),
        "fecha": fila.get("fecha", ""),
        "titular": fila.get("titular", ""),
        "palabra": fila.get("palabra", ""),
    }


def _nombre_archivo_web(fila: dict, i: int) -> Path:
    """Nombre único: se basa en el último tramo de la URL (slug de la noticia)."""
    fecha = (fila.get("fecha") or "")[:10]
    medio = _nombre_seguro(fila.get("medio") or "medio")
    slug = _nombre_seguro((fila.get("url") or "").rstrip("/").split("/")[-1])
    if slug:
        base = "%s_%s_%s" % (medio, fecha or "sfecha", slug[:70])
    else:
        base = "%s_%s_%d%d" % (medio, fecha or "sfecha", i, int(time.time()))
    return CARPETA_WEB / (base + ".png")


def capturar_web(items: list) -> dict:
    """Captura la página de cada titular. items: [{medio,fecha,titular,url,palabra}].

    Los artículos de Expreso se capturan con su VENTANA VISIBLE (Cloudflare
    bloquea los navegadores ocultos); el resto, en modo oculto.
    """
    preparar_carpetas()
    con_expreso = lambda f: "expreso" in (f.get("medio") or "").lower()
    otros = [f for f in items if not con_expreso(f)]
    de_expreso = [f for f in items if con_expreso(f)]

    if not BLOQUEO.acquire(blocking=False):
        return {"ok": False, "error": "Hay una captura en curso. Espera unos segundos."}
    capturadas, errores = [], []
    try:
        if otros:
            r = _capturar_lote_oculto(otros)
            capturadas += r["capturas"]
            errores += r["errores"]
        if de_expreso:
            r = _capturar_lote_expreso(de_expreso)
            capturadas += r["capturas"]
            errores += r["errores"]
    finally:
        BLOQUEO.release()

    if capturadas:
        agregar_fichas(capturadas)
    return {"ok": True, "capturas": capturadas, "errores": errores}


def _capturar_lote_oculto(items: list) -> dict:
    """Lote en modo oculto (cualquier medio salvo Expreso)."""
    from playwright.sync_api import sync_playwright

    capturadas, errores = [], []
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        contexto = navegador.new_context(
            viewport={"width": 1366, "height": 1000},
            locale="es-PE",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        for i, fila in enumerate(items, 1):
            destino = _nombre_archivo_web(fila, i)
            r = _capturar_pagina(contexto, fila, destino)
            r.update(_ficha_web(fila))
            (capturadas if r.get("ok") else errores).append(r)
        navegador.close()
    return {"capturas": capturadas, "errores": errores}


def _capturar_lote_expreso(items: list) -> dict:
    """Lote de Expreso usando su ventana visible compartida (expreso.py).

    Todo el lote corre en el hilo permanente de Playwright; 'procesar' se
    ejecuta allí dentro con la sesión ya garantizada.
    """
    import expreso

    def procesar(contexto, pagina, fila, indice):
        # ¿Venció la sesión (cf_clearance)? Entonces la ventana mostraría el
        # desafío: se renueva antes de capturar (aquí ya estamos en el hilo
        # correcto de Playwright).
        if expreso._clasificar_titulo(pagina) != "ok":
            try:
                expreso.cerrar_sesion()
                contexto, pagina = expreso.sesion()
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False, "tipo": "web",
                    "url": fila.get("url", ""),
                    "error": "Se perdió la sesión de Expreso: %s"
                             % str(exc).splitlines()[0][:150],
                    **_ficha_web(fila),
                }
        destino = _nombre_archivo_web(fila, indice)
        r = _capturar_pagina(contexto, fila, destino)
        r.update(_ficha_web(fila))
        return r

    try:
        return expreso.capturar_lote(items, procesar)
    except Exception as exc:  # noqa: BLE001 - candado ocupado o ventana no abierta
        return {"capturas": [], "errores": [
            {"ok": False, "tipo": "web", "url": f.get("url", ""),
             "error": str(exc).splitlines()[0][:180],
             **_ficha_web(f)}
            for f in items
        ]}


# ---------------------------------------------------------------------------
# 2) Captura de la edición impresa (La Razón)
# ---------------------------------------------------------------------------
def fechas_entre(desde: str, hasta: str) -> list:
    """Lista de fechas ISO (desc) entre 'desde' y 'hasta' (YYYY-MM-DD)."""
    try:
        d1 = date.fromisoformat(desde)
        d2 = date.fromisoformat(hasta)
    except ValueError:
        return []
    if d1 > d2:
        d1, d2 = d2, d1
    salida, actual = [], d2
    while actual >= d1:
        salida.append(actual.isoformat())
        actual -= timedelta(days=1)
    return salida


def _fusionar_rectas(rectas):
    """Une rectángulos solapados (variantes repetidas) en uno solo."""
    salida = []
    for r in rectas:
        uni = pymupdf.Rect(r)
        a_unionar = []
        for s in salida:
            inter = pymupdf.Rect(s) & uni
            if not inter.is_empty and (inter.width > 1 and inter.height > 1):
                a_unionar.append(s)
        for s in a_unionar:
            uni |= pymupdf.Rect(s)
            salida.remove(s)
        salida.append(uni)
    return salida


def _variantes_de(palabra: str) -> list:
    p = (palabra or "").strip().lower()
    if p == "rojo":
        return ["rojo", "roja", "rojos", "rojas", "rojito", "rojita", "rojitos",
                "rojitas", "rojizo", "rojiza", "rojizos", "rojizas", "rojismo",
                "rojismos", "rojista", "rojistas"]
    return [p]


def capturar_impresa_fechas(fechas: list, palabra: str, incluir_variantes: bool = True,
                            solo_portada: bool = False) -> dict:
    """Para cada fecha: descarga el PDF de La Razón y renderiza las páginas con la palabra.

    - solo_portada=True: únicamente se revisa/guarda la página 1 (la portada).
    - Cada fecha revisada queda registrada en 'procesadas.json' para poder retomar
      el trabajo en otra sesión sin repetir días (los fallos de descarga NO se
      marcan, para que se reintente en la siguiente tanda).
    """
    from playwright.sync_api import sync_playwright

    preparar_carpetas()
    patron = patron_palabra(palabra, incluir_variantes)
    variantes = _variantes_de(palabra) if incluir_variantes else [(palabra or "").strip().lower()]

    capturadas, errores = [], []
    if not BLOQUEO.acquire(blocking=False):
        return {"ok": False, "error": "Hay una captura en curso. Espera unos segundos."}
    try:
        with sync_playwright() as p:
            navegador = p.chromium.launch(headless=True)
            contexto = navegador.new_context(locale="es-PE")
            cache = {}

            for fecha_iso in fechas:
                pdf_url, estado = cache.get(fecha_iso) or _resolver_pdf(contexto, fecha_iso)
                cache[fecha_iso] = (pdf_url, estado)
                if estado == "sin_edicion":
                    marcar_procesada(fecha_iso, capturadas=0, nota="sin_edicion")
                    errores.append({"fecha": fecha_iso, "tipo": "impresa",
                                    "error": "No se publicó la edición impresa de esta fecha."})
                    continue
                if not pdf_url:
                    errores.append({"fecha": fecha_iso, "tipo": "impresa",
                                    "error": "El PDF de esta fecha aún no está publicado "
                                             "o falló la conexión: se reintentará."})
                    continue  # NO se marca: se reintentará en la próxima tanda
                try:
                    r = contexto.request.get(pdf_url, timeout=120000)
                except Exception as exc:  # noqa: BLE001
                    errores.append({"fecha": fecha_iso, "tipo": "impresa",
                                    "error": "No se pudo descargar el PDF: %s" % str(exc)[:120]})
                    continue  # NO se marca: se reintentará en la próxima tanda
                if not r.ok:
                    errores.append({"fecha": fecha_iso, "tipo": "impresa",
                                    "error": "El PDF respondió HTTP %s" % r.status})
                    continue  # NO se marca: se reintentará en la próxima tanda

                try:
                    doc = pymupdf.open(stream=r.body(), filetype="pdf")
                except Exception as exc:  # noqa: BLE001
                    errores.append({"fecha": fecha_iso, "tipo": "impresa",
                                    "error": "PDF ilegible: %s" % str(exc)[:120]})
                    continue

                paginas_ok = 0
                rango = [(1, doc[0])] if (solo_portada and doc.page_count) else list(enumerate(doc, 1))
                for indice, page in rango:
                    texto = page.get_text() or ""
                    if not tiene_palabra_adjetiva(texto, patron):
                        continue
                    rectas = []
                    for v in variantes:
                        rectas.extend(page.search_for(v))
                    for rect in _fusionar_rectas(rectas):
                        page.draw_rect(rect, color=(1, 0, 0), width=1.6,
                                       fill=(1, 0.85, 0.85), fill_opacity=0.30,
                                       overlay=True)
                    nombre = "impresa_larazon_%s_p%02d.png" % (fecha_iso, indice)
                    destino = CARPETA_IMPRESA / nombre
                    page.get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6)).save(str(destino))
                    paginas_ok += 1
                    capturadas.append({
                        "ok": True,
                        "tipo": "impresa",
                        "medio": "La Razón (edición impresa)",
                        "fecha": fecha_iso,
                        "pagina": indice,
                        "palabra": palabra,
                        "titular": "",
                        "url": pdf_url,
                        "archivo": "impresa/" + destino.name,
                    })
                doc.close()

                marcar_procesada(fecha_iso, capturadas=paginas_ok, pdf=pdf_url,
                                 nota="portada" if solo_portada else "todas")
                if paginas_ok == 0:
                    errores.append({"fecha": fecha_iso, "tipo": "impresa",
                                    "error": "La edición no contiene la palabra '%s'%s."
                                             % (palabra, " en la portada" if solo_portada else "")})
            navegador.close()
    finally:
        BLOQUEO.release()

    if capturadas:
        agregar_fichas(capturadas)
    return {"ok": True, "capturas": capturadas, "errores": errores}


def capturar_impresa_larepublica_tanda(desde: str, hasta: str, palabra: str = "rojo",
                                      solo_portada: bool = True, creds: dict | None = None) -> dict:
    """Tanda: captura de la edicion impresa de La Republica (con credenciales).

    Abre la web de la edicion (impreso.larepublica.pe) con Playwright visible, inicia
    sesion con credenciales (suscripcion S/5), y para cada fecha del rango abre la
    edicion (archivo) y captura la imagen del CDN que contiene la palabra (por defecto
    la portada). Guarda PNG en capturas/impresa y registra la ficha.
    """
    from playwright.sync_api import sync_playwright

    preparar_carpetas()
    email = (creds or {}).get("email") or LAREPUBLICA_EMAIL
    password = (creds or {}).get("pass") or LAREPUBLICA_PASS

    capturadas, errores = [], []
    if not BLOQUEO.acquire(blocking=False):
        return {"ok": False, "error": "Hay una captura en curso. Espera unos segundos."}
    try:
        with sync_playwright() as p:
            navegador = p.chromium.launch(headless=False, args=["--window-position=4000,0"])
            contexto = navegador.new_context(viewport={"width": 1400, "height": 1000})
            pg = contexto.new_page()
            pg.goto(LAREPUBLICA_HOME, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(8000)
            pg.evaluate("() => { Array.from(document.querySelectorAll('div')).filter(d => (d.className||'').toString().includes('Popup')).forEach(d => d.remove()); }")
            pg.wait_for_timeout(1500)
            btns = pg.query_selector_all("button")
            for i, btn in enumerate(btns):
                try:
                    txt = (btn.inner_text() or "").strip()
                    if "Iniciar" in txt or "incia" in txt:
                        btn.click()
                        break
                except Exception:  # noqa: BLE001
                    pass
            pg.wait_for_timeout(5000)
            pg.fill('input[type="email"]', email)
            pg.fill('input[type="password"]', password)
            for btn in pg.query_selector_all("button"):
                try:
                    if (btn.inner_text() or "").strip().lower().startswith("ingresar"):
                        btn.click()
                        break
                except Exception:  # noqa: BLE001
                    pass
            pg.wait_for_timeout(15000)

            d = date.fromisoformat(desde)
            ULT = date.fromisoformat(hasta)
            while d <= ULT:
                url = LAREPUBLICA_ARCHIVE.format(d.strftime("%d-%m-%Y"))
                pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(2500)
                src = pg.evaluate(
                    "() => { for (const im of document.querySelectorAll('img')) {"
                    " const s = im.currentSrc || im.src || ''; if (s.includes('printed')) return s; } return null; }")
                if src:
                    r = contexto.request.get(src, timeout=45000)
                    if r.status == 200:
                        data = r.body()
                        destino = CARPETA_IMPRESA / ("impresa_larepublica_%s_p01.png" % d.isoformat())
                        destino.write_bytes(data)
                        capturadas.append({
                            "ok": True,
                            "tipo": "impresa",
                            "medio": "La República (edición impresa)",
                            "fecha": d.isoformat(),
                            "pagina": 1,
                            "palabra": palabra,
                            "titular": "",
                            "url": url,
                            "archivo": str(destino.relative_to(CARPETA)),
                        })
                        marcar_procesada(d.isoformat(), capturadas=1, nota="larepublica")
                    else:
                        errores.append({"fecha": d.isoformat(), "tipo": "impresa",
                                        "error": "La imagen de la edicion respondio HTTP %s" % r.status})
                else:
                    errores.append({"fecha": d.isoformat(), "tipo": "impresa",
                                    "error": "Sin imagen de portada en la edicion."})
                d += timedelta(days=1)
            navegador.close()
    finally:
        BLOQUEO.release()

    if capturadas:
        agregar_fichas(capturadas)
    return {"ok": True, "capturas": capturadas, "errores": errores}


# ---------------------------------------------------------------------------
# 3) Empaquetado: ZIP con imágenes y ficha CSV
# ---------------------------------------------------------------------------
def generar_zip() -> bytes:
    preparar_carpetas()
    buffer = io.BytesIO()
    registros = _leer_fichas()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for reg in registros:
            archivo = CARPETA / reg.get("archivo", "")
            if archivo.exists():
                z.write(archivo, arcname=reg["archivo"])
        # Fichas en CSV para abrir en Excel
        salida = io.StringIO()
        cols = ["tipo", "medio", "fecha", "pagina", "palabra", "titular", "url", "archivo"]
        w = csv.DictWriter(salida, fieldnames=cols, quoting=csv.QUOTE_ALL)
        w.writeheader()
        for reg in registros:
            w.writerow({c: reg.get(c, "") for c in cols})
        z.writestr("fichas.csv", "\ufeff" + salida.getvalue())
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 4) Informe PDF: una página por captura con su ficha
# ---------------------------------------------------------------------------
def generar_informe_pdf() -> bytes:
    import pymupdf

    registros = [r for r in _leer_fichas() if (CARPETA / r.get("archivo", "")).exists()]
    if not registros:
        raise ValueError("No hay capturas todavía.")

    doc = pymupdf.open()
    ancho, alto = 595, 842  # A4 vertical
    margen = 36

    for reg in registros:
        page = doc.new_page(width=ancho, height=alto)
        y = margen
        page.insert_text((margen, y + 12), "CAPTURA %s" % (reg.get("tipo", "").upper()),
                         fontsize=13, fontname="hebo", color=(0.55, 0.1, 0.08))
        y += 26

        # Imagen con proporción conservada, ajustada al ancho disponible
        ruta = CARPETA / reg["archivo"]
        try:
            with pymupdf.open(ruta) as img:
                rect_img = img[0].rect
            ancho_disp = ancho - 2 * margen
            escala = ancho_disp / rect_img.width
            alto_img = rect_img.height * escala
            disponible = alto - y - 130
            if alto_img > disponible:
                escala = disponible / rect_img.height
                ancho_img = rect_img.width * escala
                alto_img = disponible
            else:
                ancho_img = ancho_disp
            destino = pymupdf.Rect(margen, y, margen + ancho_img, y + alto_img)
            flujo = _jpeg_ligero(ruta)
            if flujo:
                page.insert_image(destino, stream=flujo, keep_proportion=True)
            else:
                page.insert_image(destino, filename=str(ruta), keep_proportion=True)
            y += alto_img + 16
        except Exception:  # noqa: BLE001
            page.insert_text((margen, y + 10), "(no se pudo incrustar la imagen)",
                             fontsize=10, color=(0.6, 0.6, 0.6))
            y += 24

        # Ficha
        campos = [
            ("Medio", reg.get("medio", "")),
            ("Fecha", reg.get("fecha", "")),
            ("Página del impreso", str(reg.get("pagina", ""))),
            ("Palabra buscada", reg.get("palabra", "")),
            ("Titular", reg.get("titular", "")),
            ("Fuente (URL)", reg.get("url", "")),
            ("Archivo", reg.get("archivo", "")),
        ]
        for etiqueta, valor in campos:
            if not valor:
                continue
            texto = "%s: %s" % (etiqueta, valor)
            while texto:
                linea, texto = texto[:105], texto[105:]
                page.insert_text((margen, y + 10), linea, fontsize=8.5,
                                 fontname="helv", color=(0.15, 0.15, 0.15))
                y += 12

    salida = doc.tobytes()
    doc.close()
    return salida


def _jpeg_ligero(ruta: Path, ancho_max: int = 1500, calidad: int = 78):
    """Convierte la imagen a JPEG comprimido para que el informe pesen poco."""
    try:
        with pymupdf.open(ruta) as img:
            pix = img[0].get_pixmap()
        n = 0
        while (pix.width >> n) > ancho_max:
            n += 1
        if n:
            pix.shrink(n)
        return pix.tobytes("jpeg", jpg_quality=calidad)
    except Exception:  # noqa: BLE001 (p. ej. si la imagen lleva transparencia)
        return None
