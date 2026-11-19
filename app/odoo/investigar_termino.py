"""Agente investigador de términos: ¿a qué cartucho corresponde esto que buscó el cliente?

Lo dispara un humano desde Odoo (botón en «Términos no encontrados» y «Sugerencias de búsqueda»).
SOLO LEE: busca en internet y devuelve un informe con evidencia; no escribe nada en Odoo. Quien
decide si lo agrega como término exacto o de similitud, y a qué producto, es la persona.

Dos fases, con confianza distinta (decisión del dueño, 2026-11-19: fabricante + compatibles):
  1. FABRICANTE — dominios oficiales de la marca (`fabricante_impresoras.DOMINIOS`), que es lo
     único que afirma «este cartucho sirve en esta impresora». Confianza «fabricante».
  2. COMPATIBLES — búsqueda abierta (tiendas y fichas de cartuchos compatibles) para proponer
     equivalencias que ningún fabricante afirma (ej. CRG137 ≈ CF283X). Confianza «terceros»:
     siempre se marca como NO confirmada por el fabricante.

Regla de oro: la IA solo extrae de extractos citados y todo código que devuelva tiene que aparecer
LITERALMENTE en alguno (`armar_informe`, pura). Lo que no aparece se descarta, no se corrige.
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.odoo import fabricante_impresoras as fab

logger = logging.getLogger(__name__)

MODELO_IA = fab.MODELO_IA
_MAX_EXTRACTOS = 14000


def _contiene(texto: str, token: str) -> bool:
    return fab._contiene_token(texto, token)


def _es_oficial(url: str, marca: Optional[str]) -> bool:
    """Oficial = host de la marca (si se conoce) y no foro/comunidad."""
    if not fab._es_pagina_oficial(url):
        return False
    if marca:
        return fab._host_permitido(url, marca)
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == d or host.endswith("." + d) for v in fab.DOMINIOS.values() for d in v)


def armar_informe(termino: str, marca: Optional[str], citas: List[Dict[str, str]],
                  extraido: Dict[str, Any], respaldo_fabricante: Optional[Dict[str, Any]] = None
                  ) -> Dict[str, Any]:
    """Convierte lo extraído por la IA en un informe verificable (función PURA, sin red).

    :param citas: [{url, title, cited_text}] de la búsqueda.
    :param extraido: {tipo, marca, modelos, codigos_oem, equivalentes, impresoras_compatibles,
                      respalda_sugerido, resumen} — solo de los extractos.
    :param respaldo_fabricante: resultado ya verificado de `fabricante_impresoras` (si aplica).
    """
    extraido = extraido or {}
    texto_citas = "\n".join(c.get("cited_text", "") for c in citas)
    oficiales = [c for c in citas if _es_oficial(c.get("url", ""), marca)]
    texto_oficial = "\n".join(c.get("cited_text", "") for c in oficiales)

    def _filtrar(lista, fuente_texto):
        vistos, salida = set(), []
        for x in lista or []:
            x = str(x or "").strip()
            if x and x.lower() not in vistos and _contiene(fuente_texto, x):
                vistos.add(x.lower())
                salida.append(x)
        return salida

    codigos_oem = _filtrar(extraido.get("codigos_oem"), texto_citas)
    equivalentes = _filtrar(extraido.get("equivalentes"), texto_citas)
    modelos = _filtrar(extraido.get("modelos"), texto_citas)
    impresoras = _filtrar(extraido.get("impresoras_compatibles"), texto_citas)
    descartados = [x for x in (extraido.get("codigos_oem") or []) + (extraido.get("equivalentes") or [])
                   if str(x).strip() and not _contiene(texto_citas, str(x).strip())]

    # Confianza: «fabricante» solo si algún código OEM está en un extracto OFICIAL (o lo verificó
    # la ruta estricta de impresoras). Cualquier equivalente de compatibles es «terceros».
    del_fabricante = bool(respaldo_fabricante and respaldo_fabricante.get("ok")) or any(
        _contiene(texto_oficial, c) for c in codigos_oem)
    if del_fabricante:
        confianza = "fabricante"
    elif codigos_oem or equivalentes or modelos:
        confianza = "terceros"
    else:
        confianza = "ninguna"

    tipo = extraido.get("tipo") if extraido.get("tipo") in ("cartucho", "impresora", "otro") else "no_claro"
    if respaldo_fabricante and respaldo_fabricante.get("ok"):
        tipo = "impresora"
        for c in respaldo_fabricante.get("codigos") or []:
            if c not in codigos_oem:
                codigos_oem.append(c)

    fuentes = []
    vistas = set()
    for c in citas:
        u = c.get("url") or ""
        if u and u not in vistas:
            vistas.add(u)
            fuentes.append({"url": u, "titulo": (c.get("title") or "")[:140],
                            "extracto": (c.get("cited_text") or "")[:400],
                            "oficial": _es_oficial(u, marca)})
    respalda = extraido.get("respalda_sugerido")
    return {
        "ok": confianza != "ninguna",
        "tipo": tipo, "marca": extraido.get("marca") or marca or "",
        "confianza": confianza,
        "codigos_oem": codigos_oem, "equivalentes": equivalentes,
        "modelos": modelos, "impresoras_compatibles": impresoras[:12],
        "respalda_sugerido": respalda if isinstance(respalda, bool) else None,
        "resumen": (extraido.get("resumen") or "")[:900],
        "descartados": descartados,
        "fuentes": fuentes[:8],
    }


# ───────────────────────── búsqueda y extracción (con red) ─────────────────────────

def _buscar_abierta(termino: str, marca: Optional[str]) -> List[Dict[str, str]]:
    """Búsqueda sin restricción de dominio: fichas y tiendas de cartuchos compatibles."""
    r = fab._cliente_anthropic().messages.create(
        model=MODELO_IA, max_tokens=2500, thinking={"type": "disabled"},
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4,
                "blocked_domains": ["pinterest.com", "facebook.com", "youtube.com", "tiktok.com"]}],
        messages=[{"role": "user", "content": (
            f"En Colombia un cliente buscó «{termino}»{' (marca ' + marca + ')' if marca else ''}. "
            "Averigua si es un cartucho de tóner/tinta o una impresora. Si es un cartucho: su "
            "número de pieza OEM, para qué impresoras sirve y qué cartuchos compatibles o "
            "equivalentes lo reemplazan. Si es una impresora: qué cartuchos usa. Responde con los "
            "códigos exactos tal como aparecen en las páginas.")}])
    citas = []
    for b in r.content:
        d = b.model_dump()
        if d.get("type") == "text":
            for ci in d.get("citations") or []:
                if ci.get("url") and ci.get("cited_text"):
                    citas.append({"url": ci["url"], "title": ci.get("title") or "",
                                  "cited_text": ci["cited_text"]})
    return citas


_PROMPT = (
    "Analizas EXTRACTOS de páginas web para identificar un cartucho. Usa SOLO lo escrito "
    "literalmente en los extractos; si algo no está, no lo pongas ni lo deduzcas. Responde "
    "únicamente con JSON: {\"tipo\": \"cartucho\"|\"impresora\"|\"otro\"|\"no_claro\", "
    "\"marca\": str, \"modelos\": [str], \"codigos_oem\": [str], \"equivalentes\": [str], "
    "\"impresoras_compatibles\": [str], \"respalda_sugerido\": true|false|null, "
    "\"resumen\": str}. codigos_oem: número de pieza del fabricante del cartucho (CF283X, "
    "9435B001), sin la marca delante. equivalentes: códigos de cartuchos compatibles o "
    "alternativos que los extractos dicen que lo reemplazan (otro código distinto del OEM). "
    "impresoras_compatibles: modelos de impresora que los extractos dicen que lo usan. "
    "respalda_sugerido: true/false solo si te dan un «producto sugerido» y los extractos lo "
    "confirman o lo contradicen; null si no hay evidencia. resumen: 2-3 frases en español, "
    "sin inventar."
)


def _extraer(termino: str, citas: List[Dict[str, str]], sugerido: str = "") -> Dict[str, Any]:
    extractos = "\n\n".join(
        f"[{c['url']}] {c['title']}\n{c['cited_text']}" for c in citas)[:_MAX_EXTRACTOS]
    pedido = f"Término buscado por el cliente: {termino}\n"
    if sugerido:
        pedido += f"Producto sugerido por nuestro catálogo: {sugerido}\n"
    r = fab._cliente_anthropic().messages.create(
        model=MODELO_IA, max_tokens=1500, thinking={"type": "disabled"}, system=_PROMPT,
        messages=[{"role": "user", "content": f"{pedido}\nEXTRACTOS:\n{extractos}"}])
    raw = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else {}


def investigar(termino: str, producto_sugerido: str = "") -> Dict[str, Any]:
    """Punto de entrada. Devuelve el informe de `armar_informe`; NUNCA lanza."""
    try:
        termino = (termino or "").strip()
        if len(termino) < 2:
            return {"ok": False, "confianza": "ninguna", "motivo": "termino_vacio"}
        marca = fab.detectar_marca(termino)
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_fab = pool.submit(fab.consultar_fabricante, termino) if fab.parece_impresora(termino) else None
            f_abierta = pool.submit(_buscar_abierta, termino, marca)
            try:
                respaldo = f_fab.result() if f_fab else None
            except Exception as e:
                logger.warning("investigar fabricante '%s': %s", termino, e)
                respaldo = None
            try:
                citas = f_abierta.result()
            except Exception as e:
                logger.warning("investigar búsqueda '%s': %s", termino, e)
                citas = []
        if not citas:
            return {"ok": False, "confianza": "ninguna", "motivo": "sin_resultados"}
        informe = armar_informe(termino, marca, citas, _extraer(termino, citas, producto_sugerido), respaldo)
        logger.info("investigar '%s': confianza=%s oem=%s equiv=%s", termino, informe["confianza"],
                    informe["codigos_oem"], informe["equivalentes"])
        return informe
    except Exception as e:
        logger.warning("investigar '%s': error %s", termino, e)
        return {"ok": False, "confianza": "ninguna", "motivo": "error: %s" % e}
