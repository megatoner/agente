"""Aprender qué cartucho usa una impresora, SOLO desde el sitio del fabricante.

Caso: el cliente pide «HP LaserJet Enterprise M406», el catálogo no la conoce y antes el
bot escalaba. Ahora, cuando `_buscar_producto_core` no encuentra nada y el texto parece un
modelo de impresora, se consulta la web restringida a los dominios del fabricante, se
VERIFICA en código (no por confianza en el modelo) y, solo si todo cuadra, se registra el
modelo como término de similitud de todos los productos que usan esos cartuchos.

Regla del dueño (2026-11-12): se escribe únicamente con confianza total = lo dice el sitio
del fabricante. Ante cualquier duda NO se escribe y el bot sigue como siempre.

Cómo se garantiza (todo en `verificar`, función pura y probada sin red):
  1. la fuente es un dominio del fabricante de esa marca (lista blanca);
  2. cada código de cartucho aparece TEXTUALMENTE en un extracto citado de esa fuente;
  3. el modelo pedido por el cliente aparece en el extracto o en el título de la fuente;
  4. si la fuente distingue regiones (Europa, Asia...), el código debe estar junto a una
     mención de Américas/Latinoamérica — HP asigna 58A en Américas y 59A en Europa;
  5. marca única: si las fuentes son de dos marcas distintas se descarta;
  6. el servidor Odoo exige además que al menos un producto NUESTRO use esos cartuchos.
"""
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MODELO_IA = "claude-haiku-5-5"

# Dominios oficiales por marca de IMPRESORA (las que maneja el catálogo).
DOMINIOS: Dict[str, List[str]] = {
    "HP": ["support.hp.com", "hp.com"],
    # Samsung vendió su negocio de impresoras a HP en 2017: el soporte vive en hp.com.
    "Samsung": ["support.hp.com", "hp.com", "samsung.com"],
    "Epson": ["epson.com", "epson.com.co", "epson.la", "epson.com.mx"],
    "Canon": ["canon.com", "usa.canon.com", "latin.canon", "co.canon", "canon.com.co"],
    "Brother": ["brother.com", "support.brother.com", "brother.com.co", "brother.com.mx"],
    "Ricoh": ["ricoh.com", "ricoh-americas.com", "ricoh-usa.com", "support.ricoh.com"],
    "Kyocera": ["kyoceradocumentsolutions.com", "kyoceradocumentsolutions.com.co",
                "kyoceradocumentsolutions.us"],
    "Lexmark": ["lexmark.com", "support.lexmark.com"],
    "Pantum": ["pantum.com", "pantum.com.co"],
}

_PALABRAS_MARCA = {
    "HP": ("hp", "hewlett", "laserjet", "officejet", "deskjet", "smart tank hp"),
    "Samsung": ("samsung", "xpress"),
    "Epson": ("epson", "ecotank", "workforce"),
    "Canon": ("canon", "imageclass", "pixma", "maxify"),
    "Brother": ("brother",),
    "Ricoh": ("ricoh",),
    "Kyocera": ("kyocera", "ecosys"),
    "Lexmark": ("lexmark",),
    "Pantum": ("pantum",),
}

# Códigos de CARTUCHO (no de impresora): no se busca en el fabricante.
_RE_CARTUCHO = re.compile(
    r"^(?:[CQW]\d{3,4}[A-Z]{1,3}|C[EFB]\d{3,4}[A-Z]{1,3}|\d{2,3}[AXY]|T\d{2,4}[A-Z]?\d*|"
    r"TN-?\d{3,4}[A-Z]*|TK-?\d{3,4}[A-Z]*|CRG-?\d{2,4}[A-Z]*|MLT-?[DRT]\d{3,4}[A-Z]*|"
    r"DR-?\d{3,4}|LC-?\d{3,4}[A-Z]*|CLT-?[A-Z]\d{3,4}[A-Z]*|\d{3}XL?|D\d{3,4}[A-Z]?|W\d{4}[A-Z]{0,4})$", re.I)
_RE_MODELO = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,6}-?\d{2,5}[A-Za-z]{0,4})(?![A-Za-z0-9])")
_RE_REGIONES = re.compile(
    r"europa|oriente medio|asia|pac[ií]fico|[áa]frica|rusia|\bcis\b|\bemea\b|europe|"
    r"middle east|\bapac\b|africa|russia|asia pacific", re.I)
_RE_AMERICAS = re.compile(
    r"latinoam[eé]rica|am[eé]rica latina|norteam[eé]rica|am[eé]ricas?\b|latin america|"
    r"north america|colombia|estados unidos|\bu\.?s\.?a?\b", re.I)

_TTL_NEGATIVO = 24 * 3600
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_LOCK = threading.Lock()
_USOS_POR_CANAL: Dict[Any, List[float]] = {}
_MAX_POR_CANAL_HORA = 2


# ───────────────────────── utilidades puras ─────────────────────────

def _n(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def detectar_marca(texto: str) -> Optional[str]:
    t = (texto or "").lower()
    for marca, palabras in _PALABRAS_MARCA.items():
        for p in palabras:
            if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", t):
                return marca
    return None


def tokens_modelo(texto: str) -> List[str]:
    """Tokens que parecen el modelo de una impresora (M406, L3110, MF445dw)."""
    out = []
    for m in _RE_MODELO.findall(texto or ""):
        if _RE_CARTUCHO.match(m):
            continue
        if m.lower() not in [o.lower() for o in out]:
            out.append(m)
    return out


def parece_impresora(texto: str) -> bool:
    """Filtro barato ANTES de gastar una búsqueda web (y de avisarle al cliente).

    Debe verse como un modelo de impresora; un código de cartucho (CF258A, TN830XL), un
    número suelto (544, 711) o una sigla corta (X1, 2K) no lo son.
    """
    t = (texto or "").strip()
    if len(t) < 4 or len(t) > 120:
        return False
    return bool(tokens_modelo(t))


_RE_COMUNIDAD = re.compile(
    r"community|forum|answers\.|discussion|/t5/|/blog|blogs?\.|^h\d+\.www\d*\.hp\.com$", re.I)
# País/región de la URL (hp.com/co-es/, support.hp.com/us-en/...). Solo Américas: el
# fabricante asigna cartuchos distintos por región (HP: 58A Américas / 59A Europa).
_RE_LOCALE = re.compile(r"/([a-z]{2,7})-([a-z]{2})(?:/|$)", re.I)
_PAISES_AMERICAS = {
    "co", "mx", "ar", "cl", "pe", "br", "us", "ca", "ec", "uy", "pa", "cr", "gt", "bo",
    "py", "ve", "do", "sv", "hn", "ni", "pr", "latam", "la", "lamerica", "latin", "cla",
    "xl",
}


def _es_pagina_oficial(url: str) -> bool:
    """Descarta foros/comunidades/blogs: son texto de usuarios, no del fabricante."""
    p = urlparse(url or "")
    return not _RE_COMUNIDAD.search((p.hostname or "") + (p.path or ""))


def _region_url_ok(url: str) -> bool:
    """True si la URL es de Américas o no declara país en la ruta."""
    m = _RE_LOCALE.search(urlparse(url or "").path or "")
    if not m:
        return True
    return m.group(1).lower() in _PAISES_AMERICAS or m.group(2).lower() in _PAISES_AMERICAS \
        and m.group(1).lower() in ("es", "en", "pt")


def _host_permitido(url: str, marca: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in DOMINIOS.get(marca, []))


def _contiene_token(texto: str, token: str) -> bool:
    if not token:
        return False
    return bool(re.search(
        r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", texto or "", re.I))


# ───────────────────────── verificación (pura) ─────────────────────────

def verificar(texto_cliente: str, marca: str, citas: List[Dict[str, str]],
              extraido: Dict[str, Any]) -> Dict[str, Any]:
    """Decide si lo extraído es 100 % respaldado por el fabricante.

    :param citas: [{url, title, cited_text}] tal como las devolvió la búsqueda.
    :param extraido: {es_impresora, marca, modelos:[...], cartuchos:[...]} de la IA,
                     que solo vio los extractos.
    :return: {ok, motivo, ...}; si ok trae marca, modelos, terminos, codigos, url, extracto.
    """
    if not extraido or not extraido.get("es_impresora"):
        return {"ok": False, "motivo": "no_es_impresora"}
    marca_ia = (extraido.get("marca") or marca or "").strip()
    marca_ok = next((m for m in DOMINIOS if m.lower() == marca_ia.lower()), None)
    if not marca_ok:
        return {"ok": False, "motivo": "marca_desconocida", "detalle": marca_ia}

    validas = [
        c for c in citas
        if c.get("cited_text") and _host_permitido(c.get("url", ""), marca_ok)
        and _es_pagina_oficial(c.get("url", ""))
        # una página de otra región solo vale si el propio texto dice que aplica a Américas
        and (_region_url_ok(c.get("url", "")) or _RE_AMERICAS.search(c["cited_text"]))
    ]
    if not validas:
        return {"ok": False, "motivo": "sin_fuente_del_fabricante"}
    # Marca única: citas de otro fabricante que también hablan del modelo = ambigüedad.
    otras = {m for m in DOMINIOS if m != marca_ok and m not in ("Samsung",)
             for c in citas if _host_permitido(c.get("url", ""), m)
             and not _host_permitido(c.get("url", ""), marca_ok)}
    if otras and not detectar_marca(texto_cliente):
        return {"ok": False, "motivo": "ambiguo_marca", "detalle": sorted(otras)}

    cliente_tokens = tokens_modelo(texto_cliente)
    if not cliente_tokens:
        return {"ok": False, "motivo": "sin_token_modelo"}

    cuerpo = "\n".join(c["cited_text"] for c in validas)
    titulos = "\n".join(c.get("title") or "" for c in validas)
    fuente_txt = cuerpo + "\n" + titulos

    # Modelos: literales en la fuente.
    modelos = []
    for crudo in extraido.get("modelos") or []:
        # La IA a veces devuelve el nombre completo ("LaserJet Pro P1102w"): el término
        # de búsqueda es solo el código de modelo.
        for m in tokens_modelo(str(crudo)):
            if _contiene_token(fuente_txt, m) and m.lower() not in [x.lower() for x in modelos]:
                modelos.append(m)
    if not modelos:
        return {"ok": False, "motivo": "modelo_no_aparece_en_fuente"}
    # La fuente debe ser sobre la impresora PEDIDA, no sobre otra parecida.
    pedido_ok = any(m.lower().startswith(t.lower()) or t.lower().startswith(m.lower())
                    for m in modelos for t in cliente_tokens)
    if not pedido_ok:
        return {"ok": False, "motivo": "fuente_de_otro_modelo", "detalle": modelos}

    # Códigos de cartucho: literales en la fuente.
    codigos = []
    for c in extraido.get("cartuchos") or []:
        c = str(c).strip()
        if c and _contiene_token(cuerpo, c) and c.upper() not in [x.upper() for x in codigos]:
            codigos.append(c)
    if not codigos:
        return {"ok": False, "motivo": "cartucho_no_aparece_en_fuente"}

    # El código y el modelo deben ir JUNTOS: en el mismo extracto, o en una página cuyo
    # título nombra el modelo. Que ambos aparezcan en extractos sueltos no prueba que el
    # cartucho sea de esa impresora (una página del L3210 que cita al L3110 daba frascos 544
    # para una impresora que usa 664).
    def _junto(codigo):
        for c in validas:
            if not _contiene_token(c["cited_text"], codigo):
                continue
            if any(_contiene_token(c["cited_text"], m) or _contiene_token(c.get("title") or "", m)
                   for m in modelos):
                return True
        return False
    codigos = [c for c in codigos if _junto(c)]
    if not codigos:
        return {"ok": False, "motivo": "cartucho_sin_relacion_con_modelo"}

    # Región: si la fuente reparte cartuchos por región, la mención de región MÁS CERCANA
    # al código (preferentemente la que lo antecede) debe ser Américas. Una ventana fija
    # no sirve: "Latinoamérica: 58A. Europa: 59A" deja a 59A cerca de las dos.
    if _RE_REGIONES.search(cuerpo):
        marcas = ([(m.start(), m.end(), True) for m in _RE_AMERICAS.finditer(cuerpo)]
                  + [(m.start(), m.end(), False) for m in _RE_REGIONES.finditer(cuerpo)])
        buenos = []
        for c in codigos:
            for mo in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(c) + r"(?![A-Za-z0-9])", cuerpo, re.I):
                antes = [(mo.start() - e, am) for (_s, e, am) in marcas if e <= mo.start()]
                despues = [(s_ - mo.end(), am) for (s_, _e, am) in marcas if s_ >= mo.end()]
                if antes:
                    cercana = min(antes)[1]
                elif despues:
                    cercana = min(despues)[1]
                else:
                    cercana = False
                if cercana:
                    buenos.append(c)
                    break
        if not buenos:
            return {"ok": False, "motivo": "region_no_confirmada"}
        codigos = buenos

    # Términos a registrar: modelos verificados + el token del cliente si es prefijo de uno.
    terminos = list(modelos)
    for t in cliente_tokens:
        if len(t) >= 4 and any(m.lower().startswith(t.lower()) for m in modelos) \
                and t.lower() not in [x.lower() for x in terminos]:
            terminos.append(t)
    terminos = [t for t in terminos if len(t) >= 4][:8]

    mejor = max(validas, key=lambda c: sum(1 for k in codigos if _contiene_token(c["cited_text"], k)))
    return {
        "ok": True, "marca": marca_ok, "modelos": modelos, "terminos": terminos,
        "codigos": codigos, "url": mejor["url"], "extracto": cuerpo[:2000],
        # nombre de la impresora PEDIDA, no de un modelo hermano que la fuente también cita
        "impresora": next((m for m in modelos for t in cliente_tokens
                           if m.lower().startswith(t.lower())), modelos[0]),
    }


# ───────────────────────── llamadas a la API ─────────────────────────

def _cliente_anthropic():
    import anthropic
    from app.config import get_settings
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key)


_ENFOQUES = {
    "estandar": ("Busca en el sitio oficial del fabricante qué cartuchos de tóner o tinta usa "
                 "(para Colombia / Latinoamérica)."),
    "alto_rendimiento": ("Busca en el sitio oficial del fabricante el cartucho de ALTO RENDIMIENTO "
                         "(alta capacidad, XL, o el que termina en X) y los cartuchos de CADA color "
                         "que acepta (para Colombia / Latinoamérica)."),
}


def _buscar_citas(texto: str, marca: Optional[str], enfoque: str = "estandar") -> List[Dict[str, str]]:
    dominios = DOMINIOS.get(marca) if marca else sorted({d for v in DOMINIOS.values() for d in v})
    r = _cliente_anthropic().messages.create(
        model=MODELO_IA, max_tokens=2500, thinking={"type": "disabled"},
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3,
                "allowed_domains": dominios}],
        messages=[{"role": "user", "content": (
            f"Un cliente en Colombia pide la impresora «{texto}». {_ENFOQUES[enfoque]} "
            "Responde con los códigos exactos de cartucho y el nombre exacto del modelo tal "
            "como aparece en la página.")}])
    citas = []
    for b in r.content:
        d = b.model_dump()
        if d.get("type") == "text":
            for ci in d.get("citations") or []:
                if ci.get("url") and ci.get("cited_text"):
                    citas.append({"url": ci["url"], "title": ci.get("title") or "",
                                  "cited_text": ci["cited_text"]})
    return citas


_PROMPT_EXTRACCION = (
    "Extraes datos de EXTRACTOS de páginas de un fabricante de impresoras. Usa SOLO lo que está "
    "escrito literalmente en los extractos; si algo no está, no lo pongas. Responde únicamente "
    "con JSON: {\"es_impresora\": bool, \"marca\": str, \"modelos\": [str], \"cartuchos\": [str]}. "
    "es_impresora=false si lo pedido no es una impresora (por ejemplo es un cartucho). "
    "modelos: nombres de modelo de la impresora pedida tal como aparecen (M406dn). "
    "cartuchos: números de pieza del cartucho (CF258A, CF258X), sin la marca delante; si el "
    "extracto da dos (58A y CF258A), incluye ambos."
)


def _extraer(texto: str, citas: List[Dict[str, str]]) -> Dict[str, Any]:
    extractos = "\n\n".join(f"[{c['url']}] {c['title']}\n{c['cited_text']}" for c in citas)[:12000]
    r = _cliente_anthropic().messages.create(
        model=MODELO_IA, max_tokens=1500, thinking={"type": "disabled"}, system=_PROMPT_EXTRACCION,
        messages=[{"role": "user", "content": f"Impresora pedida: {texto}\n\nEXTRACTOS:\n{extractos}"}])
    raw = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else {}


def consultar_fabricante(texto: str) -> Dict[str, Any]:
    """Búsqueda restringida al fabricante + verificación. No escribe nada."""
    marca = detectar_marca(texto)
    citas: List[Dict[str, str]] = []
    # Dos búsquedas con enfoque distinto (cartucho estándar / alto rendimiento): una sola
    # suele traer solo 58A y dejar fuera 58X, que también le sirve a la impresora.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        futuros = [pool.submit(_buscar_citas, texto, marca, e) for e in ("estandar", "alto_rendimiento")]
        for f in futuros:
            try:
                citas += f.result()
            except Exception as e:
                logger.warning("consultar_fabricante búsqueda: %s", e)
    if not citas:
        return {"ok": False, "motivo": "sin_resultados"}
    return verificar(texto, marca or "", citas, _extraer(texto, citas))


# ───────────────────────── orquestación ─────────────────────────

def _permitido_por_canal(canal) -> bool:
    if not canal:  # corrida por lote (backlog), sin chat de por medio
        return True
    ahora = time.time()
    with _LOCK:
        usos = [t for t in _USOS_POR_CANAL.get(canal, []) if ahora - t < 3600]
        if len(usos) >= _MAX_POR_CANAL_HORA:
            _USOS_POR_CANAL[canal] = usos
            return False
        usos.append(ahora)
        _USOS_POR_CANAL[canal] = usos
        return True


def aprender_impresora(odoo, texto: str, canal_id=None, bot_id: int = 0) -> Dict[str, Any]:
    """Punto de entrada. Devuelve {ok, aprendido, motivo, ...}; NUNCA lanza.

    `aprendido=True` significa que el catálogo ya conoce el modelo: el llamador repite la
    búsqueda normal.
    """
    try:
        texto = (texto or "").strip()
        if not parece_impresora(texto):
            return {"ok": False, "aprendido": False, "motivo": "no_parece_impresora"}
        clave = _n(texto)
        with _LOCK:
            previo = _CACHE.get(clave)
        if previo and time.time() - previo[0] < _TTL_NEGATIVO:
            return {**previo[1], "cache": True}
        if not _permitido_por_canal(canal_id):
            return {"ok": False, "aprendido": False, "motivo": "limite_por_canal"}

        # Aviso al cliente: la búsqueda tarda unos segundos.
        if canal_id and bot_id:
            try:
                odoo.execute_kw(
                    "jpc.whatsapp.bot.config", "enviar_resumen_productos",
                    [[bot_id], canal_id,
                     "🔎 Esa impresora no la tengo registrada todavía. Estoy haciendo una "
                     "búsqueda ampliada en el sitio del fabricante para ubicar su cartucho. "
                     "Dame un momento, por favor 🙏"])
            except Exception as e:
                logger.warning("aprender_impresora aviso: %s", e)

        v = consultar_fabricante(texto)
        if not v.get("ok"):
            res = {"ok": False, "aprendido": False, "motivo": v.get("motivo", "sin_verificar")}
            logger.info("aprender_impresora '%s': NO se escribe (%s %s)", texto, res["motivo"],
                        v.get("detalle", ""))
        else:
            r = odoo.execute_kw(
                "jpc.whatsapp.bot.aprendizaje.impresora", "aplicar", [],
                {"marca": v["marca"], "modelos": v["terminos"], "codigos": v["codigos"],
                 "url": v["url"], "extracto": v["extracto"], "canal_id": canal_id or False,
                 "impresora_nombre": v["impresora"]})
            if r.get("ok"):
                res = {"ok": True, "aprendido": True, "terminos": r.get("terminos"),
                       "productos": r.get("productos"), "codigos": v["codigos"], "url": v["url"]}
                logger.info("aprender_impresora '%s': APRENDIDO %s -> %s", texto,
                            r.get("terminos"), [p["name"] for p in r.get("productos", [])])
            else:
                res = {"ok": False, "aprendido": False, "motivo": r.get("motivo"),
                       "codigos": v["codigos"], "url": v["url"]}
                logger.info("aprender_impresora '%s': el servidor no lo aplicó (%s)", texto, r)
        with _LOCK:
            _CACHE[clave] = (time.time(), res)
        return res
    except Exception as e:  # nunca rompe la búsqueda normal
        logger.warning("aprender_impresora '%s': error %s", texto, e)
        return {"ok": False, "aprendido": False, "motivo": "error: %s" % e}
