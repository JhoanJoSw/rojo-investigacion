#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Expreso (www.expreso.com.pe): búsqueda y captura tras el muro de Cloudflare.

Sondeo del 25/10/2026:
  * expreso.pe es solo un redireccionador (302, Namecheap) cuyo HTTPS falla;
    el sitio real es www.expreso.com.pe (WordPress).
  * Cloudflare BLOQUEA a los navegadores ocultos (headless → "Attention
    Required") y deja pasar una ventana de navegador REAL. Por eso la ventana
    se abre visible, colocada FUERA de la pantalla (--window-position).
  * Las peticiones del "contexto de API" de Playwright reciben 403: los datos
    se piden con fetch() desde dentro de la propia página (fingerprint real).
  * API: /wp-json/wp/v2/posts?search=...&after=...&before=...
    Busca por título+contenido+extracto; el filtro del TITULAR (y variantes)
    lo hace después la app, igual que con La República.
    Truco: con variantes se busca la raíz "roj" para que entren rojo/roja/
    rojos/rojito/rojizo... y el patrón de la app descarta lo que no interesa.

HILO ÚNICO: la API síncrona de Playwright no sirve desde varios hilos
(el servidor HTTP atiende cada petición en un hilo distinto → "connection
closed"). Por eso todo lo que toca Playwright pasa por un hilo permanente
(_en_hilo); el candado _bloqueo se toma en el hilo llamante para que si hay
una operación en curso se conteste "ocupado" enseguida.
"""

import atexit
import json
import queue
import threading
import time
import urllib.parse
from concurrent.futures import Future

SITE = "https://www.expreso.com.pe"
VIGENCIA_SESION = 15 * 60   # cf_clearance dura poco: no reutilizar mucho

_bloqueo = threading.Lock()
_estado = {"p": None, "ctx": None, "page": None, "t": 0.0}
# Caché de nombres id → nombre (categorías, etiquetas, autores)
_cache_nombres = {"categories": {}, "tags": {}, "users": {}}
# Meta de la última búsqueda (total de noticias y páginas de 100 en el rango)
ULTIMA_META = {"total": None, "paginas": None}

# --- Hilo permanente de Playwright -----------------------------------------
_cola: "queue.Queue" = queue.Queue()
_hilo = {"t": None}
_hilo_creado = threading.Lock()


def _worker():
    while True:
        fn, args, futuro = _cola.get()
        try:
            futuro.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001 - se propaga al llamante
            futuro.set_exception(exc)


def _en_hilo(fn, *args, timeout=None):
    """Ejecuta fn en el hilo permanente de Playwright y devuelve su resultado."""
    with _hilo_creado:
        if _hilo["t"] is None or not _hilo["t"].is_alive():
            _hilo["t"] = threading.Thread(
                target=_worker, name="expreso-playwright", daemon=True)
            _hilo["t"].start()
    futuro: Future = Future()
    _cola.put((fn, args, futuro))
    return futuro.result(timeout=timeout)


def _ejecutar(fn, *args, timeout=None):
    """Candado en el llamante + ejecución en el hilo de Playwright."""
    if not _bloqueo.acquire(blocking=False):
        raise RuntimeError("Hay otra operación de Expreso en curso. "
                           "Espera unos segundos y vuelve a intentarlo.")
    try:
        return _en_hilo(fn, *args, timeout=timeout)
    finally:
        _bloqueo.release()


# ---------------------------------------------------------------------------
# Sesión visible con Cloudflare superado (SOLO desde el hilo de Playwright)
# ---------------------------------------------------------------------------
def _clasificar_titulo(page) -> str:
    """'ok' | 'desafio' (just a moment) | 'bloqueo' (attention required)."""
    try:
        t = (page.title() or "").lower()
    except Exception:  # noqa: BLE001
        return "desafio"
    if "just a moment" in t or "checking" in t:
        return "desafio"
    if "attention required" in t or "access denied" in t:
        return "bloqueo"
    return "ok"


def _esperar_paso(page, segs: int) -> str:
    fin = time.time() + segs
    while time.time() < fin:
        est = _clasificar_titulo(page)
        if est == "ok":
            return est
        page.wait_for_timeout(800)
    return _clasificar_titulo(page)


def cerrar_sesion():
    """Cierra ventana, contexto y controlador de Playwright (silencioso)."""
    for clave in ("page", "ctx", "p"):
        obj = _estado.get(clave)
        _estado[clave] = None
        if obj is None:
            continue
        try:
            if clave == "p":
                obj.stop()
            else:
                obj.close()
        except Exception:  # noqa: BLE001
            pass
    _estado["t"] = 0.0


def _lanzar():
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    try:
        nav = p.chromium.launch(
            headless=False,                       # Cloudflare bloquea el oculto
            args=[
                "--window-position=4000,2000",    # fuera de la pantalla
                "--window-size=1366,1000",
                "--no-first-run",
                "--disable-session-crashed-bubble",
            ],
        )
        ctx = nav.new_context(viewport={"width": 1366, "height": 1000}, locale="es-PE")
        page = ctx.new_page()
        page.goto(SITE + "/", timeout=60000, wait_until="domcontentloaded")
        est = _esperar_paso(page, 45)
        if est != "ok":
            raise RuntimeError(
                "Cloudflare no deja pasar Expreso en este momento (%s). "
                "Reintenta en unos minutos." % est)
        _estado.update(p=p, ctx=ctx, page=page, t=time.time())
    except Exception:
        _estado.update(p=p, ctx=None, page=None, t=0.0)
        cerrar_sesion()
        raise


def sesion():
    """(contexto, página) con Cloudflare ya superado. SOLO hilo de Playwright.

    Verifica que la sesión siga viva (por si el navegador/contexto se cerró
    sin pasar por cerrar_sesion) y la renueva si no.
    """
    viva = False
    try:
        if _estado["p"] and _estado["ctx"] and _estado["page"]:
            _estado["page"].title()   # lanza si la página/contexto está cerrado
            viva = True
    except Exception:  # noqa: BLE001
        viva = False
    if not viva or (time.time() - _estado["t"] > VIGENCIA_SESION):
        cerrar_sesion()
        _lanzar()
    return _estado["ctx"], _estado["page"]


# ---------------------------------------------------------------------------
# Peticiones desde el navegador (SOLO hilo de Playwright)
# ---------------------------------------------------------------------------
def _fetch(page, url: str) -> dict:
    """GET con fetch() dentro de la propia página (cookies + fingerprint reales)."""
    return page.evaluate(
        """async (url) => {
             try {
               const r = await fetch(url, {credentials: 'include'});
               const t = await r.text();
               return {status: r.status,
                       total: r.headers.get('X-WP-Total'),
                       paginas: r.headers.get('X-WP-TotalPages'),
                       cuerpo: t};
             } catch (e) { return {status: 0, total: null, paginas: null,
                                   cuerpo: String(e)}; }
           }""", url)


def _es_json(cuerpo) -> bool:
    return str(cuerpo or "").lstrip().startswith(("[", "{"))


def _get_paginado(page, url: str):
    """GET con renovación de sesión si Cloudflare venció (403/HTML)."""
    r = _fetch(page, url)
    if r.get("status") == 200 and _es_json(r.get("cuerpo")):
        return r, page
    cerrar_sesion()
    _ctx, page = sesion()
    return _fetch(page, url), page


def _nombres_usuarios(page, ids) -> dict:
    """El sitio prohíbe LISTAR usuarios (401); el endpoint individual sí es
    público para autores con publicaciones: se pide uno por uno."""
    for uid in ids:
        if uid in _cache_nombres["users"]:
            continue
        r = _fetch(page, SITE + "/wp-json/wp/v2/users/%d?_fields=id,name" % uid)
        nombre = ""
        if r.get("status") == 200 and _es_json(r.get("cuerpo")):
            try:
                nombre = (json.loads(r["cuerpo"]).get("name") or "")
            except (ValueError, TypeError, AttributeError):
                nombre = ""
        _cache_nombres["users"][uid] = nombre   # "" = no expuesto: no reintentar
    return _cache_nombres["users"]


def _anotar_nombres(page, posts: list):
    """Adjunta a cada post: _seccion (1ª categoría), _autor, _etiquetas."""
    if not posts:
        return

    def ids_de(campo):
        salida = set()
        for p in posts:
            for i in (p.get(campo) or []):
                try:
                    salida.add(int(i))
                except (TypeError, ValueError):
                    pass
        return sorted(salida)

    def nombres(tipo, ruta, ids):
        if not ids:
            return _cache_nombres[tipo]
        faltantes = [i for i in ids if i not in _cache_nombres[tipo]]
        if faltantes:
            u = SITE + ruta + "?include=%s&per_page=100&_fields=id,name" % (
                ",".join(str(i) for i in faltantes))
            r = _fetch(page, u)
            if r.get("status") == 200 and _es_json(r.get("cuerpo")):
                for reg in json.loads(r["cuerpo"]):
                    _cache_nombres[tipo][reg.get("id")] = reg.get("name") or ""
        return _cache_nombres[tipo]

    cats = nombres("categories", "/wp-json/wp/v2/categories", ids_de("categories"))
    tags = nombres("tags", "/wp-json/wp/v2/tags", ids_de("tags"))
    autores = sorted({p["author"] for p in posts if p.get("author")})
    users = _nombres_usuarios(page, autores)

    for p in posts:
        lista_cats = [cats.get(i, "") for i in (p.get("categories") or [])]
        p["_seccion"] = next((c for c in lista_cats if c), "")
        p["_etiquetas"] = "; ".join(
            n for n in (tags.get(i, "") for i in (p.get("tags") or [])) if n)
        p["_autor"] = users.get(p.get("author"), "") or ""


# ---------------------------------------------------------------------------
# Operaciones públicas (candado en el llamante; Playwright en su hilo)
# ---------------------------------------------------------------------------
def _a_entero(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _buscar_impl(palabra, pagina, desde, hasta, variantes, por_pagina) -> list:
    _ctx, page = sesion()
    # Raíz "roj" para que entren todas las variantes; el título se filtra
    # después con el patrón de la app (igual que en La República).
    termino = "roj" if (variantes and (palabra or "").strip().lower() == "rojo") \
        else (palabra or "").strip()
    params = {
        "search": termino,
        "per_page": max(1, min(100, por_pagina)),
        "page": max(1, pagina),
        "_fields": "id,date,link,title,excerpt,categories,tags,author",
    }
    if desde:
        params["after"] = desde + "T00:00:00"
    if hasta:
        params["before"] = hasta + "T23:59:59"
    url = SITE + "/wp-json/wp/v2/posts?" + urllib.parse.urlencode(params)

    r, page = _get_paginado(page, url)
    ULTIMA_META["total"] = _a_entero(r.get("total"))
    ULTIMA_META["paginas"] = _a_entero(r.get("paginas"))
    if r.get("status") != 200:
        raise RuntimeError("Expreso respondió HTTP %s para la página %s"
                           % (r.get("status"), pagina))
    if not _es_json(r.get("cuerpo")):
        texto = str(r.get("cuerpo"))[:120].replace("\n", " ")
        raise RuntimeError("Respuesta inesperada de Expreso: %s" % texto)
    posts = json.loads(r["cuerpo"])
    if not isinstance(posts, list):
        raise RuntimeError("La API de Expreso devolvió algo inesperado.")
    _anotar_nombres(page, posts)
    _estado["t"] = time.time()   # la ventana siguió viva: renovar reloj
    return posts


def buscar_pagina(palabra: str, pagina: int, desde: str = "", hasta: str = "",
                  variantes: bool = True, por_pagina: int = 100) -> list:
    """Una página de resultados de Expreso (posts crudos con _seccion/_autor/...)."""
    return _ejecutar(_buscar_impl, palabra, pagina, desde, hasta, variantes,
                     por_pagina, timeout=300)


def _capturar_lote_impl(filas, procesar) -> dict:
    """procesar(contexto, pagina, fila, indice) -> fila_resultado (dict)."""
    _ctx, pagina = sesion()
    capturadas, errores = [], []
    for indice, fila in enumerate(filas, 1):
        try:
            r = procesar(_ctx, pagina, fila, indice)
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "tipo": "web",
                 "medio": fila.get("medio", ""), "fecha": fila.get("fecha", ""),
                 "titular": fila.get("titular", ""), "palabra": fila.get("palabra", ""),
                 "url": fila.get("url", ""),
                 "error": str(exc).splitlines()[0][:180]}
        (capturadas if r.get("ok") else errores).append(r)
        _estado["t"] = time.time()
    return {"capturas": capturadas, "errores": errores}


def capturar_lote(filas: list, procesar) -> dict:
    """Captura un lote de artículos de Expreso en su ventana visible.

    'procesar' se ejecuta SIEMPRE en el hilo de Playwright; si la sesión venció
    y renovar falla, puede dejar el error en la fila devuelta.
    """
    return _ejecutar(_capturar_lote_impl, filas, procesar, timeout=None)


def estado() -> dict:
    """Diagnóstico para la UI/sondeo."""
    with _bloqueo:
        return {
            "sesion_activa": bool(_estado["ctx"]),
            "antiguedad_s": int(time.time() - _estado["t"]) if _estado["t"] else None,
        }


def _al_cerrar():
    try:
        _en_hilo(cerrar_sesion, timeout=10)
    except Exception:  # noqa: BLE001
        pass


atexit.register(_al_cerrar)
