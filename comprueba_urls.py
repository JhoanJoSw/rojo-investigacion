#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Comprueba las URL reales de ediciones con año de 2 dígitos."""

from playwright.sync_api import sync_playwright

URLS = [
    "https://larazon.pe/edicion_impresa/ediciones-impresas-24-10-26/",
    "https://larazon.pe/edicion_impresa/ediciones-impresas-17-10-26/",
    "https://larazon.pe/edicion_impresa/ediciones-impresas-15-10-2026-2/",
]


def main():
    with sync_playwright() as p:
        nav = p.chromium.launch(headless=True)
        ctx = nav.new_context(locale="es-PE")
        for url in URLS:
            pg = ctx.new_page()
            etiqueta = url.rstrip("/").split("/")[-1]
            try:
                r = pg.goto(url, timeout=45000, wait_until="domcontentloaded")
                pg.wait_for_timeout(2500)
                src = pg.eval_on_selector_all(
                    "[source]", "els => els.map(e => e.getAttribute('source'))")
                pdfs = [s for s in src if s and s.lower().endswith(".pdf")]
                print("%s -> status %s -> %s" % (etiqueta, r.status if r else "?", pdfs[:2]))
            except Exception as exc:  # noqa: BLE001
                print("%s -> ERROR %s" % (etiqueta, str(exc)[:100]))
            finally:
                pg.close()
        nav.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
