"""JPC Odoo tools for the AI agent server."""
from langchain.tools import tool
from typing import List, Dict, Any, Optional
from app.odoo.client import OdooClient, get_odoo_client
import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _fmt_currency(amount) -> str:
    try:
        return f"${float(amount):,.0f} COP"
    except Exception:
        return str(amount)


def _fmt_date(date_str: str) -> str:
    if not date_str or date_str == "False":
        return "N/A"
    try:
        from datetime import datetime
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        months = ["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"]
        return f"{d.day} {months[d.month-1]} {d.year}"
    except Exception:
        return str(date_str)[:10]


def _m2o(field) -> str:
    if isinstance(field, (list, tuple)) and len(field) > 1:
        return str(field[1])
    return str(field) if field else "N/A"


# ── Repuestos de impresora (cilindro, almohadilla, fusor...) ────────────────
# Si el cliente pide un repuesto, la referencia NO debe resolverse como
# cartucho/toner/tinta. Ej: "cilindros 105" no es el toner HP 105A.
_PART_TYPES = {
    'cilindro': ['cilindro', 'cilindros', 'drum', 'drums', 'tambor',
                 'tambores', 'unidad de imagen'],
    'almohadilla': ['almohadilla', 'almohadillas', 'pad', 'pads'],
    'fusor': ['fusor', 'fusores', 'fuser'],
    'rodillo': ['rodillo', 'rodillos', 'roller', 'rollers'],
    'cuchilla': ['cuchilla', 'cuchillas', 'blade', 'wiper'],
    'cabezal': ['cabezal', 'cabezales'],
    'banda': ['banda de transferencia', 'transfer belt'],
    'repuesto': ['repuesto', 'repuestos'],
}
_PART_ALIAS_ALL = sorted({a for _als in _PART_TYPES.values() for a in _als})
_PART_ALIAS_RE = re.compile(
    r'(?<![a-zA-Z])(?:' + '|'.join(re.escape(a) for a in _PART_ALIAS_ALL) + r')(?![a-zA-Z])',
    re.IGNORECASE,
)
# Palabras que indican consumible normal — resetean la asociacion de repuesto
_CARTUCHO_RE = re.compile(r'\b(toner|t[oó]ner|cartuchos?|tintas?|botellas?|cintas?)\b', re.IGNORECASE)
_NOMBRES_REPUESTO = {
    'cilindro': 'cilindro/drum',
    'almohadilla': 'almohadilla',
    'fusor': 'fusor',
    'rodillo': 'rodillo',
    'cuchilla': 'cuchilla/blade',
    'cabezal': 'cabezal',
    'banda': 'banda de transferencia',
    'repuesto': 'repuesto',
}


# ── Resumen de variantes (colores/chip) ────────────────────────────────────
_COLOR_EMOJI = {
    'negro': '⚫', 'negra': '⚫', 'black': '⚫',
    'cian': '🔵', 'cyan': '🔵', 'azul': '🔵',
    'magenta': '🔴',
    'amarillo': '🟡', 'amarilla': '🟡', 'yellow': '🟡',
    'rojo': '🔴', 'red': '🔴',
    'verde': '🟢', 'green': '🟢',
    'gris': '⚪', 'gray': '⚪', 'grey': '⚪',
    'tricolor': '🌈', 'color': '🌈',
    'con chip': '🔧', 'chip integrado': '🔧',
    'sin chip': '🔩',
}
_COLOR_ORDEN = [
    'con chip', 'chip integrado', 'sin chip',
    'negro', 'negra', 'black',
    'cian', 'cyan', 'azul', 'magenta',
    'amarillo', 'amarilla', 'yellow',
    'rojo', 'red', 'verde', 'green',
    'gris', 'gray', 'grey', 'tricolor', 'color',
]


def _extraer_distinctor(nombre):
    """Extrae el diferenciador (color, chip) del nombre del producto.

    Retorna (etiqueta_capitalizada, emoji).
    """
    n = (nombre or '').lower()
    for kw in _COLOR_ORDEN:
        if kw in n:
            label = 'Con chip' if kw in ('con chip', 'chip integrado') else kw.capitalize()
            return label, _COLOR_EMOJI.get(kw, '•')
    return None, '•'


def _es_megatoner_u_original(prod):
    categ = (prod.get('categ_name') or '').lower()
    return 'megatoner' in categ or 'original' in categ


def _es_megatoner(prod):
    categ = (prod.get('categ_name') or '').lower()
    return 'megatoner' in categ or 'compatible' in categ


def _trigger_resumen(prods):
    """Decide si mostrar resumen de texto en lugar de tarjetas.

    Trigger A: 4+ productos de cualquier tipo -> siempre resumen, nunca tarjetas.
    Trigger B: 2+ productos con el mismo jpc_pos_name (ej: con chip / sin chip).
    Trigger C: 2 productos donde al menos uno tiene chip_attr definido (evita
               que el LLM asuma chip status por su conocimiento previo).
    """
    if len(prods) < 2:
        return False
    if len(prods) >= 4:
        return True  # Siempre resumen para 4+ productos
    names = [p.get('name') or '' for p in prods]
    if len(set(names)) == 1:
        return True
    # Trigger C: cualquier producto con chip_attr → generar resumen estructurado
    if len(prods) == 2 and any(p.get('chip_attr') for p in prods):
        return True
    return False


def _detectar_repuesto(texto):
    """Retorna el tipo de repuesto mencionado en el texto, o None."""
    t = (texto or '').lower()
    for tipo, aliases in _PART_TYPES.items():
        pat = '|'.join(re.escape(a) for a in aliases)
        if re.search(r'(?<![a-zA-Z])(?:' + pat + r')(?![a-zA-Z])', t):
            return tipo
    return None


def _msg_repuesto_no_encontrado(busqueda_tipo, referencia):
    """Mensaje guia para el LLM cuando un repuesto pedido no esta en catalogo.

    busqueda_tipo viene empaquetado: 'repuesto_no_encontrado:<tipo>[:<parciales>]'.
    """
    partes = busqueda_tipo.split(':', 2)
    tipo = partes[1] if len(partes) > 1 else 'repuesto'
    parciales = partes[2] if len(partes) > 2 else ''
    nombre = _NOMBRES_REPUESTO.get(tipo, tipo)
    lines = [
        f"⚠️ El cliente pidio un REPUESTO ({nombre}): '{referencia}'.",
        f"NO hay {nombre} en el catalogo para esa referencia.",
        "REGLAS OBLIGATORIAS:",
        f"1. NO ofrezcas toners, cartuchos ni tintas de esa referencia como si fueran el {nombre} pedido.",
        "2. Informa al cliente que ese repuesto no esta en el catalogo en linea y que un asesor "
        "verificara disponibilidad y precio. Luego usa escalar_a_asesor con el motivo.",
        "3. Si en el mismo mensaje el cliente pidio ADEMAS cartuchos/toners/tintas, "
        "busca cada uno con una llamada separada.",
    ]
    if parciales:
        lines.append(
            "Coincidencias parciales en catalogo (pueden NO ser compatibles): " + parciales
            + ". Solo mencionalas indicando el modelo exacto al que corresponden "
            "y pregunta al cliente si le sirve."
        )
    return "\n".join(lines)


def create_odoo_tools(odoo_context: Optional[Dict[str, Any]] = None):
    """Factory that creates all JPC Odoo tools."""
    odoo = get_odoo_client()

    ch_id = (odoo_context or {}).get("channel_id")
    _phone = (odoo_context or {}).get("phone", "")
    _partner_id = int((odoo_context or {}).get("partner_id") or 0)
    _session_id = int((odoo_context or {}).get("session_id") or 0)
    # Carrito de estado persistido en jwb_bot_context
    _cart = (odoo_context or {}).get("cart") or {}
    # Almacén del bot para filtro de stock (0 = sin restricción)
    _warehouse_id = int((odoo_context or {}).get("bot_warehouse_id") or 0)
    _wh_ctx = {"warehouse": _warehouse_id} if _warehouse_id else {}
    # Lista de precios del bot (se aplica a nuevas cotizaciones)
    _bot_pricelist_id = int((odoo_context or {}).get("bot_pricelist_id") or 0)
    # Política cuando un producto NO tiene regla en la pricelist del bot:
    #   'list_price' (default) → Odoo cae al precio de lista de la ficha
    #   'hide'                 → el producto no se muestra al cliente
    _price_fallback = str((odoo_context or {}).get("price_fallback") or "list_price").strip()
    # ID del bot de WhatsApp (para aviso de búsqueda similar)
    _bot_id = int((odoo_context or {}).get("bot_id") or 0)

    _commercial_cache = {}

    def _commercial_id(pid=None):
        """ID de la empresa (commercial_partner_id) del partner dado o del cliente del chat.

        Las cotizaciones/facturas quedan a nombre de la EMPRESA aunque escriba un
        contacto individual — toda validación de pertenencia y búsqueda por cliente
        debe usar este ID como raíz del child_of. Retorna el mismo id si el partner
        no tiene empresa padre; 0 si no hay partner.
        """
        base = int(pid or _partner_id or 0)
        if not base:
            return 0
        if base in _commercial_cache:
            return _commercial_cache[base]
        cid = base
        try:
            rows = odoo.read("res.partner", [base], ["commercial_partner_id"])
            cp = rows[0].get("commercial_partner_id") if rows else None
            cp_id = cp[0] if isinstance(cp, (list, tuple)) else int(cp or 0)
            cid = cp_id or base
        except Exception:
            pass
        _commercial_cache[base] = cid
        return cid

    def _es_del_cliente(order_partner):
        """True si order_partner (m2o de sale.order.partner_id) es el cliente actual,
        su empresa, o un contacto de ella."""
        if not _partner_id:
            return True
        opid = order_partner[0] if isinstance(order_partner, (list, tuple)) else int(order_partner or 0)
        _cid = _commercial_id()
        if opid in (_partner_id, _cid):
            return True
        try:
            return bool(odoo.search_read(
                "res.partner",
                [("id", "child_of", _cid), ("id", "=", opid)], ["id"], 1,
            ))
        except Exception:
            return False

    def _resolver_order_id_cliente(order_id: int, estados=("draft", "sent", "sale")):
        """Valida que order_id pertenezca al cliente actual (o su empresa); si no,
        intenta resolverlo por nombre dentro de la familia del cliente (cubre el caso
        de que el LLM haya confundido el order_id numérico con los dígitos del NOMBRE
        de la cotización, ej. 'S53654' -> 53654, que puede coincidir por azar con el
        ID real de una orden de OTRO cliente).

        Retorna (order_id_resuelto, record) o (None, None) si no se pudo resolver de
        forma segura para este cliente — en ese caso el llamador NO debe operar sobre
        el order_id original.
        """
        recs = odoo.search_read(
            "sale.order",
            [("id", "=", order_id), ("state", "in", list(estados))],
            ["id", "name", "partner_id"], limit=1,
        )
        if recs and _es_del_cliente(recs[0].get("partner_id")):
            return order_id, recs[0]

        if not _partner_id:
            # Sin cliente en contexto no hay forma segura de validar — usar tal cual.
            return (order_id, recs[0]) if recs else (None, None)

        alt = odoo.search_read(
            "sale.order",
            [
                ("partner_id", "child_of", _commercial_id()),
                ("name", "ilike", str(order_id)),
                ("state", "in", list(estados)),
            ],
            ["id", "name", "partner_id"], limit=1,
        )
        if alt:
            if recs:
                logger.warning(
                    "_resolver_order_id_cliente: order_id=%s no pertenece al partner %s "                    "(es de partner=%s) — resuelto como %s (ID:%s) por nombre",
                    order_id, _partner_id, recs[0].get("partner_id"), alt[0]["name"], alt[0]["id"],
                )
            return alt[0]["id"], alt[0]

        return None, None

    def _verificar_stock_o_sugerir(product_id: int, cantidad: float = 1.0):
        """Bloquea agregar a una cotización un producto sin stock suficiente en el
        almacén del bot. Si está agotado, busca variantes hermanas (ej. con
        chip/sin chip del mismo tóner — MISMO código entre paréntesis del
        jpc_pos_name, ej. '(M-W1500X)', aunque el jpc_pos_name completo difiera
        porque incluye la palabra 'con chip'/'sin chip') que SÍ tengan stock,
        para poder ofrecerlas de inmediato en vez de solo rechazar.

        Retorna None si hay stock suficiente (puede proceder). Si no, retorna un
        mensaje de error con instrucción para el agente — el llamador NO debe
        crear la línea.
        """
        try:
            prods = odoo.read(
                "product.product", [product_id],
                ["qty_available", "jpc_pos_name", "name", "product_tmpl_id"],
                **({"context": _wh_ctx} if _warehouse_id else {}),
            )
        except Exception as e:
            logger.warning("_verificar_stock_o_sugerir: error leyendo producto=%s: %s", product_id, e)
            return None  # no bloquear por un error de lectura — mejor dejar pasar
        if not prods:
            return None
        p = prods[0]
        disponible = p.get("qty_available") or 0
        if disponible >= cantidad:
            return None

        nombre = p.get("jpc_pos_name") or p.get("name") or f"producto {product_id}"
        msg = (
            f"❌ '{nombre}' está AGOTADO (disponible: {disponible:.0f}, solicitado: "
            f"{cantidad:.0f}). NO se agregó a la cotización.\n"
        )
        try:
            pos_name = p.get("jpc_pos_name") or ""
            tmpl = p.get("product_tmpl_id")
            tmpl_id = tmpl[0] if isinstance(tmpl, (list, tuple)) else tmpl
            m = re.search(r'\(([^)]+)\)', pos_name)
            codigo = m.group(1).strip() if m else ""
            codigo_base = re.sub(r'\s*sc$', '', codigo, flags=re.IGNORECASE).strip()
            if codigo_base:
                hermanos = odoo.search_read(
                    "product.template",
                    [("jpc_pos_name", "ilike", codigo_base), ("id", "!=", tmpl_id), ("active", "=", True)],
                    ["id"], limit=10,
                )
                tmpl_ids = [h["id"] for h in hermanos]
                if tmpl_ids:
                    variantes = odoo.search_read(
                        "product.product",
                        [("product_tmpl_id", "in", tmpl_ids), ("active", "=", True)],
                        ["id", "default_code", "display_name", "qty_available"],
                        limit=10,
                        **({"context": _wh_ctx} if _warehouse_id else {}),
                    )
                    alternativas = [v for v in variantes if (v.get("qty_available") or 0) >= cantidad]
                    if alternativas:
                        msg += "Alternativas disponibles (misma referencia, otra variante):\n"
                        for v in alternativas:
                            msg += (f"  • {v.get('display_name')} (product_id={v['id']}) — "
                                    f"stock: {v.get('qty_available'):.0f}\n")
                        msg += "Ofrécele estas opciones al cliente en vez de la agotada."
                    else:
                        msg += "No hay variantes alternativas con stock. Ofrece escalar a asesor."
                else:
                    msg += "No hay variantes alternativas con stock. Ofrece escalar a asesor."
        except Exception as e:
            logger.warning("_verificar_stock_o_sugerir: error buscando alternativas: %s", e)
            msg += "No pude buscar alternativas. Ofrece escalar a asesor."
        return msg

    def _merge_bot_context(updates: dict):
        """Fusiona updates en jwb_bot_context del canal sin sobrescribir otras claves."""
        if not ch_id:
            return
        try:
            odoo.execute_kw(
                "discuss.channel", "jwb_merge_bot_context",
                [[ch_id], json.dumps(updates, ensure_ascii=False)],
            )
        except Exception as e:
            logger.warning("_merge_bot_context: %s", e)

    def _get_carrito_id() -> int | None:
        """Retorna el ID del carrito abierto para este canal, creándolo si no existe."""
        if not ch_id:
            return None
        try:
            existing = odoo.search_read(
                "jpc.whatsapp.bot.carrito",
                [["channel_id", "=", ch_id], ["state", "=", "open"]],
                ["id"], limit=1,
            )
            if existing:
                return existing[0]["id"]
            vals = {"channel_id": ch_id, "state": "open"}
            if _session_id:
                vals["session_id"] = _session_id
            if _partner_id:
                vals["partner_id"] = _partner_id
            return odoo.create("jpc.whatsapp.bot.carrito", vals)
        except Exception as e:
            logger.warning("_get_carrito_id: %s", e)
            return None

    def _upsert_carrito_linea(
        product_id: int,
        product_name: str,
        search_term: str = "",
        tarjeta_mostrada: bool = False,
        precio_mostrado: bool = False,
        precio_unit: float = 0.0,
        precio_total: float = 0.0,
        qty_solicitada: float | None = None,
    ) -> int | None:
        """Crea o actualiza una línea de carrito para el producto dado."""
        carrito_id = _get_carrito_id()
        if not carrito_id:
            return None
        try:
            existing = odoo.search_read(
                "jpc.whatsapp.bot.carrito.linea",
                [["carrito_id", "=", carrito_id], ["product_id", "=", product_id]],
                ["id"], limit=1,
            )
            vals = {"product_name": product_name, "tarjeta_mostrada": tarjeta_mostrada}
            if search_term:
                vals["search_term"] = search_term
            if precio_mostrado:
                vals["precio_mostrado"] = True
                vals["precio_unit"] = precio_unit
                vals["precio_total"] = precio_total
            if qty_solicitada is not None:
                vals["qty_solicitada"] = qty_solicitada
            if existing:
                odoo.execute_kw(
                    "jpc.whatsapp.bot.carrito.linea", "write",
                    [[existing[0]["id"]], vals],
                )
                return existing[0]["id"]
            vals.update({
                "carrito_id": carrito_id,
                "product_id": product_id,
                "search_term": search_term or "",
                "qty_solicitada": 0.0,
                "seleccionado_cotizar": True,
            })
            return odoo.create("jpc.whatsapp.bot.carrito.linea", vals)
        except Exception as e:
            logger.warning("_upsert_carrito_linea product=%s: %s", product_id, e)
            return None

    def _actualizar_carrito_linea_cotizada(
        product_id: int,
        product_name: str,
        sale_order_line_id: int,
        qty: float,
        order_id: int,
    ):
        """Actualiza la línea del carrito cuando el producto ya fue agregado a la cotización."""
        try:
            carrito = odoo.search_read(
                "jpc.whatsapp.bot.carrito",
                [["channel_id", "=", ch_id], ["state", "in", ["open", "quoted"]]],
                ["id"], limit=1,
            )
            if not carrito:
                return
            carrito_id = carrito[0]["id"]
            linea = odoo.search_read(
                "jpc.whatsapp.bot.carrito.linea",
                [["carrito_id", "=", carrito_id], ["product_id", "=", product_id]],
                ["id"], limit=1,
            )
            if linea:
                odoo.execute_kw(
                    "jpc.whatsapp.bot.carrito.linea", "write",
                    [[linea[0]["id"]], {
                        "sale_order_line_id": sale_order_line_id,
                        "qty_solicitada": qty,
                        "product_name": product_name,
                    }],
                )
            odoo.execute_kw(
                "jpc.whatsapp.bot.carrito", "write",
                [[carrito_id], {"sale_order_id": order_id, "state": "quoted"}],
            )
        except Exception as e:
            logger.warning("_actualizar_carrito_linea_cotizada: %s", e)

    def _base_url():
        # bot_agent (least-privilege) no tiene acceso directo a ir.config_parameter
        # (endurecimiento de seguridad 2026-07-08) — se usa jwb_get_base_url, que
        # expone SOLO este valor puntual vía sudo() del lado Odoo.
        try:
            return odoo.execute_kw("jpc.whatsapp.bot.config", "jwb_get_base_url", []) or ""
        except Exception as e:
            logger.warning("_base_url: %s", e)
            return ""

    def _enviar_tarjetas_auto(ids: list, max_cards: int = 3) -> str:
        """Envía tarjetas de producto por WhatsApp. Retorna mensaje de estado."""
        if not ch_id:
            return "⚠️ Sin canal WhatsApp — no se pueden enviar tarjetas."
        ids_cut = ids[:max(1, min(3, int(max_cards)))]
        try:
            result = odoo.execute_kw(
                "discuss.channel", "jwb_enviar_tarjeta_v3",
                [[ch_id], ids_cut],
            )
            enviados = result.get("enviados", 0) if isinstance(result, dict) else 0
            errors = result.get("errors", []) if isinstance(result, dict) else []
            if enviados > 0:
                msg = f"✅ {enviados} tarjeta(s) enviada(s) al cliente."
                if errors:
                    msg += f" ({len(errors)} con error)"
                return msg
            err_str = "; ".join(errors[:2]) if errors else "sin detalles"
            return f"⚠️ No se pudo enviar tarjetas. Error: {err_str}"
        except Exception as e:
            logger.exception("_enviar_tarjetas_auto: error RPC")
            return f"❌ Error enviando tarjetas: {str(e)[:200]}"

    _precio_canal_cache = {}

    def _tiene_precio_canal(product_id):
        """True si el producto tiene regla aplicable (qty=1) en la pricelist del bot.

        Usado con price_fallback='hide': sin regla, Odoo caería al precio de
        lista de la ficha del producto — que puede no ser el precio del canal.
        """
        if not _bot_pricelist_id:
            return True
        if product_id in _precio_canal_cache:
            return _precio_canal_cache[product_id]
        ok = True
        try:
            prod = odoo.read("product.product", [product_id], ["product_tmpl_id", "categ_id"])
            if prod:
                tmpl = prod[0].get("product_tmpl_id")
                tmpl_id = tmpl[0] if isinstance(tmpl, (list, tuple)) else int(tmpl or 0)
                categ = prod[0].get("categ_id")
                categ_id = categ[0] if isinstance(categ, (list, tuple)) else int(categ or 0)
                domain = [
                    ("pricelist_id", "=", _bot_pricelist_id),
                    ("min_quantity", "<=", 1),
                    "|", "|", "|",
                    ("product_id", "=", product_id),
                    ("product_tmpl_id", "=", tmpl_id),
                    ("applied_on", "=", "3_global"),
                    "&", ("applied_on", "=", "2_product_category"),
                    ("categ_id", "parent_of", categ_id),
                ]
                ok = bool(odoo.search_read("product.pricelist.item", domain, ["id"], 1))
            else:
                ok = False
        except Exception as e:
            logger.warning("_tiene_precio_canal product=%s: %s", product_id, e)
            ok = True  # ante la duda, no ocultar
        _precio_canal_cache[product_id] = ok
        return ok

    def _get_product_price_for_client(product_id, partner_id):
        """Precio unitario a qty=1 con la pricelist del bot (default_pricelist_id). Retorna float o None."""
        if not partner_id:
            return None
        try:
            order_vals = {
                "partner_id": partner_id,
                "state": "draft",
                "payment_method_id": 215,
            }
            if _bot_pricelist_id:
                order_vals["pricelist_id"] = _bot_pricelist_id
            order_id = odoo.create("sale.order", order_vals)
            line_id = odoo.create("sale.order.line", {
                "order_id": order_id,
                "product_id": product_id,
                "product_uom_qty": 1.0,
            })
            lines = odoo.read("sale.order.line", [line_id], ["price_unit"])
            odoo.execute_kw("sale.order", "unlink", [[order_id]])
            if lines:
                return float(lines[0].get("price_unit") or 0)
        except Exception as e:
            logger.warning("_get_product_price_for_client product=%s: %s", product_id, e)
        return None

    # ── CLIENTES ──────────────────────────────────────────────────────────

    @tool
    def obtener_cliente_whatsapp() -> str:
        """Identificar automáticamente al cliente del chat de WhatsApp actual.

        USA ESTA HERRAMIENTA PRIMERO si necesitas el partner_id del cliente.
        Si partner_id > 0 en odoo_context, el cliente ya está identificado y
        NO necesitas llamar esta herramienta — usa directamente ese partner_id.
        """
        ctx = odoo_context or {}
        _partner_id = ctx.get("partner_id")
        _partner_name = ctx.get("partner_name", "")
        _partner_vat = ctx.get("partner_vat", "")
        _partner_city = ctx.get("partner_city", "")

        if _partner_id and int(_partner_id) > 0 and _partner_name:
            return (
                f"✅ Cliente identificado:\n"
                f"ID: {_partner_id}\n"
                f"Nombre: {_partner_name}\n"
                f"NIT/CC: {_partner_vat or 'N/A'}\n"
                f"Teléfono: {_phone or 'N/A'}\n"
                f"Ciudad: {_partner_city or 'N/A'}"
            )

        if _partner_id and int(_partner_id) > 0:
            try:
                recs = odoo.read(
                    "res.partner", [int(_partner_id)],
                    ["id","name","vat","phone","email","city","credit","debit"]
                )
                if recs:
                    p = recs[0]
                    return (
                        f"✅ Cliente identificado:\n"
                        f"ID: {p['id']}\n"
                        f"Nombre: {p.get('name','N/A')}\n"
                        f"NIT/CC: {p.get('vat','N/A')}\n"
                        f"Teléfono: {p.get('phone','N/A')}\n"
                        f"Ciudad: {p.get('city','N/A')}\n"
                        f"Deuda pendiente: {_fmt_currency(p.get('debit',0))}"
                    )
            except Exception as e:
                logger.warning("obtener_cliente_whatsapp: error leyendo partner_id=%s: %s", _partner_id, e)

        if _phone:
            clean = "".join(c for c in _phone if c.isdigit())[-8:]
            try:
                partners = odoo.search_read(
                    "res.partner",
                    [("phone", "ilike", clean)],
                    ["id","name","vat","phone","city","credit","debit"],
                    1,
                )
                if partners:
                    p = partners[0]
                    return (
                        f"✅ Cliente identificado por teléfono:\n"
                        f"ID: {p['id']}\n"
                        f"Nombre: {p.get('name','N/A')}\n"
                        f"NIT/CC: {p.get('vat','N/A')}\n"
                        f"Teléfono: {p.get('phone','N/A')}\n"
                        f"Ciudad: {p.get('city','N/A')}\n"
                        f"Deuda pendiente: {_fmt_currency(p.get('debit',0))}"
                    )
            except Exception as e:
                logger.warning("obtener_cliente_whatsapp: error buscando por teléfono: %s", e)

        return (
            "⚠️ Cliente no identificado en el sistema.\n"
            "Teléfono del canal: " + (_phone or "no disponible") + "\n"
            "Solicita al cliente su nombre o empresa para registrarlo."
        )

    @tool
    def consultar_faq(tema: str) -> str:
        """Consultar preguntas frecuentes de la empresa: métodos de pago, garantía, envíos,
        horario, devoluciones, documentos corporativos (RUT, cámara de comercio,
        certificado bancario, etc). USAR para políticas/procesos generales, no para
        productos específicos.

        Si la respuesta indica un documento disponible, usa enviar_documento_faq(faq_id)
        para enviarlo — respeta la instrucción de confirmación si la trae.
        """
        bot_id = (odoo_context or {}).get("bot_id")
        campos = ["id", "pregunta", "respuesta", "categoria", "bot_id",
                  "documento_filename", "requiere_confirmacion"]
        try:
            # Buscar FAQs que coincidan: primero específicas del bot, luego globales
            domain_bot = [
                ("active", "=", True),
                ("pregunta", "ilike", tema),
            ]
            if bot_id:
                domain_bot.append(("bot_id", "=", bot_id))
            faqs = odoo.search_read("jpc.whatsapp.bot.faq", domain_bot, campos, limit=3)
            # Si no encontró del bot específico, buscar globales
            if not faqs and bot_id:
                faqs = odoo.search_read(
                    "jpc.whatsapp.bot.faq",
                    [("active", "=", True), ("pregunta", "ilike", tema), ("bot_id", "=", False)],
                    campos, limit=3,
                )
            if not faqs:
                return f"No encontré información sobre '{tema}'. Escala al asesor si es urgente."

            partes = []
            for f in faqs:
                texto = f.get("respuesta", "") or ""
                nombre_doc = f.get("documento_filename")
                if nombre_doc:
                    if f.get("requiere_confirmacion"):
                        texto += (
                            f"\n[Tengo el documento '{nombre_doc}' listo, pero esta FAQ "
                            f"requiere confirmación previa — NO lo envíes todavía. Pregúntale "
                            f"al cliente si esto es exactamente lo que necesita. Solo si "
                            f"confirma explícitamente, llama "
                            f"enviar_documento_faq(faq_id={f['id']}).]"
                        )
                    else:
                        texto += (
                            f"\n[Documento disponible: '{nombre_doc}'. Usa "
                            f"enviar_documento_faq(faq_id={f['id']}) para enviarlo.]"
                        )
                if texto:
                    partes.append(texto)
            return "\n\n".join(partes)
        except Exception as e:
            logger.warning("consultar_faq: %s", e)
            return f"No pude consultar la información sobre '{tema}'."

    @tool
    def enviar_documento_faq(faq_id: int) -> str:
        """Enviar por WhatsApp el documento adjunto de una FAQ (ej: certificado bancario,
        RUT, cámara de comercio). Usa el faq_id retornado por consultar_faq.

        Si consultar_faq indicó que esa FAQ requiere confirmación previa, NO llames esta
        tool hasta que el cliente haya confirmado explícitamente que es lo que necesita.
        """
        if not ch_id:
            return "No hay canal activo para enviar el documento."
        try:
            result = odoo.execute_kw(
                "jpc.whatsapp.bot.funciones.negocio",
                "action_enviar_documento_faq_agente",
                [[]],
                {"faq_id": faq_id, "channel_id": ch_id},
            )
            return result or "✅ Documento enviado."
        except Exception as e:
            return f"Error enviando el documento: {str(e)}"

    @tool
    def buscar_cliente(query: str = "", phone: str = "", limit: int = 5) -> str:
        """Buscar un cliente en Odoo por nombre, NIT, email o teléfono."""
        if phone:
            clean = "".join(c for c in phone if c.isdigit())[-8:]
            domain = [("phone", "ilike", clean)]
        elif query:
            domain = ["|", "|", ("name", "ilike", query),
                      ("vat", "ilike", query), ("email", "ilike", query)]
        else:
            domain = [("customer_rank", ">", 0)]
        partners = odoo.search_read(
            "res.partner", domain,
            ["id","name","vat","phone","email","city","credit","debit"], limit)
        if not partners:
            return "No se encontraron clientes con ese criterio."
        lines = []
        for p in partners:
            saldo = (p.get("debit") or 0)
            lines.append(f"ID:{p['id']} | {p.get('name','')} | NIT:{p.get('vat','N/A')} | "
                         f"Tel:{p.get('phone','N/A')} | "
                         f"Ciudad:{p.get('city','N/A')} | Deuda:{_fmt_currency(saldo)}")
        return "\n".join(lines)

    @tool
    def registrar_cliente(nombre: str, telefono: str = "", email: str = "",
                          nit: str = "", ciudad: str = "") -> str:
        """Registrar un nuevo cliente en Odoo. Retorna el ID creado."""
        vals = {"name": nombre, "customer_rank": 1}
        if telefono: vals["phone"] = telefono
        if email:    vals["email"] = email
        if nit:      vals["vat"] = nit
        if ciudad:   vals["city"] = ciudad
        new_id = odoo.create("res.partner", vals)
        return f"Cliente registrado — ID:{new_id} | {nombre}"

    @tool
    def obtener_perfil_cliente(client_id: int) -> str:
        """Obtener perfil completo de un cliente: datos, saldo, deuda."""
        recs = odoo.read("res.partner", [client_id],
                         ["name","vat","phone","email","city","street",
                          "credit","debit","customer_rank"])
        if not recs:
            return f"Cliente ID {client_id} no encontrado."
        p = recs[0]
        return (f"Cliente: {p.get('name')}\nNIT: {p.get('vat','N/A')}\n"
                f"Tel: {p.get('phone','N/A')}\n"
                f"Email: {p.get('email','N/A')}\nCiudad: {p.get('city','N/A')}\n"
                f"Deuda pendiente: {_fmt_currency(p.get('debit',0))}\n"
                f"A favor (crédito): {_fmt_currency(p.get('credit',0))}")

    @tool
    def consultar_datos_facturacion() -> str:
        """Verifica si el cliente ya tiene los datos para facturar y despachar: NIT/cédula,
        nombre, correo, teléfono y dirección de envío. Llamar ANTES de pedirle cualquier
        dato (justo cuando confirma domicilio o recogida) — así solo se pregunta lo que falta.
        """
        if not ch_id:
            return "Sin canal activo, no se puede consultar."
        try:
            res = odoo.execute_kw(
                "discuss.channel", "jwb_consultar_datos_facturacion", [[ch_id]]
            )
        except Exception as e:
            logger.warning("consultar_datos_facturacion: %s", e)
            return "No se pudo consultar el estado de los datos del cliente."
        if not res or not res.get("ok"):
            return (res or {}).get("error") or "No se pudo consultar los datos del cliente."

        lineas = [f"¿Completo para facturar?: {'SÍ' if res['completo'] else 'NO'}"]
        if res["tiene_documento"]:
            lineas.append(f"Documento: {res['tipo_documento']} {res['numero_documento']}")
        else:
            lineas.append("Documento (NIT/cédula): NO tiene — hay que pedirlo.")
        lineas.append(f"Nombre: {res.get('nombre') or 'N/A'}")
        lineas.append(f"Correo: {res['correo']}" if res["tiene_correo"] else "Correo: NO tiene — hay que pedirlo.")
        lineas.append(f"Teléfono: {res['telefono']}" if res["tiene_telefono"] else "Teléfono: NO tiene — hay que pedirlo.")
        _term = res.get("payment_term") or "Pago de Contado"
        if res.get("es_credito"):
            lineas.append(f"Términos de pago: {_term} (CRÉDITO — NO preguntes método de pago, solo confirma el pedido)")
        else:
            lineas.append(f"Términos de pago: {_term} (CONTADO — pregunta método de pago antes de cerrar, ver sección 7b)")
        direcciones = res.get("direcciones") or []
        if not direcciones:
            lineas.append("Direcciones de envío: NINGUNA guardada — hay que pedirla.")
        elif len(direcciones) == 1:
            d = direcciones[0]
            lineas.append(
                f"Dirección de envío (1 sola, confirmar con el cliente si es esta): "
                f"{d['etiqueta']} — {d['calle']}, {d['ciudad']}"
            )
        else:
            lineas.append(f"Direcciones de envío guardadas ({len(direcciones)}, preguntar cuál usar):")
            for d in direcciones:
                lineas.append(f"  - ID:{d['id']} | {d['etiqueta']} — {d['calle']}, {d['ciudad']}")
        return "\n".join(lineas)

    def _estado_datos_facturacion():
        """Chequeo server-side de datos de facturación, independiente de si el LLM
        llamó consultar_datos_facturacion() en este turno. Se usa como guard antes
        de cerrar una venta (generar_link_cotizacion / confirmar_cotizacion_y_link_pago)
        porque confiar solo en que el prompt lo pida no es suficiente — se observó un
        caso real donde el agente saltó el chequeo y mandó el link sin NIT ni correo.

        Retorna (completo: bool, faltantes: str). Si la consulta falla, retorna
        completo=True (no bloquear la venta por un problema de infraestructura —
        mismo principio de "no bloquear" del resto del flujo de datos_facturacion)."""
        if not ch_id:
            return True, ""
        try:
            res = odoo.execute_kw(
                "discuss.channel", "jwb_consultar_datos_facturacion", [[ch_id]]
            )
        except Exception as e:
            logger.warning("_estado_datos_facturacion: %s", e)
            return True, ""
        if not res or not res.get("ok"):
            return True, ""
        if res.get("completo"):
            return True, ""
        faltantes = []
        if not res.get("tiene_documento"):
            faltantes.append("NIT/cédula")
        if not res.get("tiene_correo"):
            faltantes.append("correo")
        if not (res.get("direcciones") or []):
            faltantes.append("dirección de envío")
        return False, ", ".join(faltantes)

    @tool
    def completar_datos_facturacion(
        tipo_documento: str = "",
        numero_documento: str = "",
        razon_social: str = "",
        correo: str = "",
        telefono: str = "",
        via_principal: str = "",
        numero_1: str = "",
        complemento_1: str = "",
        direccional_1: str = "",
        numero_2: str = "",
        complemento_2: str = "",
        direccional_2: str = "",
        numero_puerta: str = "",
        interior: str = "",
        interior_numero: str = "",
        interior_2: str = "",
        interior_numero_2: str = "",
        barrio: str = "",
        ciudad: str = "",
        departamento: str = "",
        indicaciones: str = "",
        nombre_sede: str = "",
    ) -> str:
        """Guarda en Odoo los datos de facturación/envío que el cliente dio: documento,
        correo, teléfono y/o una dirección de envío nueva (se agrega como dirección
        ADICIONAL, nunca reemplaza una existente). Llenar SOLO lo que el cliente
        respondió; no inventar valores. tipo_documento: 'nit' o 'cedula'. Si mandó
        foto del RUT, usar el NIT y la razón social de ese documento.
        Dirección (nomenclatura vial colombiana): via_principal código ('CR','CL',
        'AV','DG','TV'), numero_1 (ej '74'), complemento_1 (letra), numero_2/
        complemento_2 (tras el '#'), numero_puerta (tras el '-'), direccional_1/2
        (Norte/Sur/Oriente/Occidente), interior/interior_numero (ej 'Apartamento'/'301').
        ciudad es OBLIGATORIA para el código postal — si falta, preguntarla antes de llamar.
        """
        if not ch_id:
            return "Sin canal activo, no se puede guardar."

        vals = {}
        if tipo_documento:
            vals["tipo_documento"] = tipo_documento.strip().lower()
        if numero_documento:
            vals["numero_documento"] = numero_documento.strip()
        if razon_social:
            vals["razon_social"] = razon_social.strip()
        if correo:
            vals["correo"] = correo.strip()
        if telefono:
            vals["telefono"] = telefono.strip()
        if nombre_sede:
            vals["nombre_sede"] = nombre_sede.strip()

        _direccion_map = {
            "via_principal_code": via_principal, "numero_1": numero_1,
            "complemento_1": complemento_1, "direccional_1_code": direccional_1,
            "numero_2": numero_2, "complemento_2": complemento_2,
            "direccional_2_code": direccional_2, "numero_puerta": numero_puerta,
            "interior_code": interior, "interior_numero": interior_numero,
            "interior_code_2": interior_2, "interior_numero_2": interior_numero_2,
            "barrio": barrio, "ciudad": ciudad, "departamento": departamento,
            "indicaciones": indicaciones,
        }
        direccion = {k: v.strip() for k, v in _direccion_map.items() if v}
        if direccion:
            vals["direccion"] = direccion

        if not vals:
            return "No se recibió ningún dato para guardar."

        try:
            res = odoo.execute_kw(
                "discuss.channel", "jwb_completar_datos_facturacion", [[ch_id], vals]
            )
        except Exception as e:
            logger.warning("completar_datos_facturacion: %s", e)
            return "No se pudieron guardar los datos, intenta de nuevo."

        if not res or not res.get("ok"):
            return (res or {}).get("error") or "No se pudieron guardar los datos."

        resumen = res.get("resumen", {})
        partes = []
        if resumen.get("documento"):
            d = resumen["documento"]
            partes.append(
                f"Documento guardado: {d['tipo'].upper()} {d['numero']} — {d['nombre']}"
                + (" (ese documento ya existía, se vinculó a ese contacto)" if d.get("vinculado_a_existente") else "")
            )
        if resumen.get("documento_error"):
            partes.append(f"⚠️ {resumen['documento_error']}")
        if resumen.get("direccion"):
            dd = resumen["direccion"]
            cp_txt = dd["codigo_postal"] if dd.get("geocodificado") else "PENDIENTE (un asesor la completará)"
            partes.append(
                f"Dirección guardada: {dd['etiqueta']} — {dd['calle']}, {dd['ciudad']} — Código postal: {cp_txt}"
            )
        if resumen.get("direccion_error"):
            partes.append(f"⚠️ {resumen['direccion_error']}")
        if not partes:
            partes.append("Datos de contacto (correo/teléfono) actualizados.")
        return "\n".join(partes)

    # ── PRODUCTOS ─────────────────────────────────────────────────────────

    # IDs de categorías raíz de consumibles
    _CONSUMABLE_CATS = [15, 16, 17, 18]
    # ID del atributo "Impresoras Compatibles" en Odoo
    _ATTR_COMPAT_IMPRESORA = 14
    # ID del atributo "Sku OEM" (referencia cartucho)
    _ATTR_SKU_OEM = 13

    _CONSUMABLE_STOP_DEFAULT = {
        "toner", "tóner", "cartucho", "tinta", "drum", "chip", "impresora",
        "printer", "laser", "láser", "inkjet", "generico", "genérico",
        "original", "compatible", "mfp", "mpf",
    }

    def _get_stop_words() -> set:
        bot_id = (odoo_context or {}).get("bot_id")
        if bot_id:
            try:
                rec = odoo.read("jpc.whatsapp.bot.config", [bot_id], ["printer_stop_words"])
                raw = (rec or [{}])[0].get("printer_stop_words") or ""
                if raw.strip():
                    return {w.strip().lower() for w in raw.splitlines() if len(w.strip()) > 1}
            except Exception:
                pass
        return set(_CONSUMABLE_STOP_DEFAULT)

    def _buscar_por_atributo_impresora(termino: str) -> list:
        """Busca IDs de product.product cuyo atributo 'Impresoras Compatibles' coincide."""
        try:
            attr_vals = odoo.search_read(
                "product.attribute.value",
                [("attribute_id", "=", _ATTR_COMPAT_IMPRESORA),
                 ("name", "ilike", termino)],
                ["id", "name"], 30
            )
            # Límite de palabra: "85x" no debe matchear embebido en un nombre de
            # impresora sin separador como "M2885Xpress" (ilike es substring puro
            # y no distingue eso de una impresora real "85x").
            _boundary = re.compile(
                r'(?<![a-zA-Z0-9])' + re.escape(termino) + r'(?![a-zA-Z0-9])',
                re.IGNORECASE,
            )
            attr_vals = [v for v in attr_vals if _boundary.search(v.get("name") or "")]
            if not attr_vals:
                return []
            av_ids = [v["id"] for v in attr_vals]
            ptavs = odoo.search_read(
                "product.template.attribute.value",
                [("product_attribute_value_id", "in", av_ids)],
                ["product_tmpl_id"], 50
            )
            if not ptavs:
                return []
            # Filtrar templates no publicados
            pub_tmpls = odoo.search_read(
                "product.template",
                [("id", "in", list({p["product_tmpl_id"][0] for p in ptavs if p.get("product_tmpl_id")})),
                 ("active", "=", True)],
                ["id"], 20,
            )
            tmpl_ids = [t["id"] for t in pub_tmpls]
            if not tmpl_ids:
                return []
            prods = odoo.search_read(
                "product.product",
                [("product_tmpl_id", "in", tmpl_ids),
                 ("sale_ok", "=", True),
                 ("active", "=", True)],
                ["id", "name", "default_code", "qty_available", "product_tmpl_id"], 12,
                context=_wh_ctx if _warehouse_id else None,
            )
            if _warehouse_id:
                prods = [p for p in prods if (p.get("qty_available") or 0) > 0]
            return prods[:6]
        except Exception as e:
            logger.warning("_buscar_por_atributo_impresora: %s", e)
            return []

    def _buscar_por_sku_oem(referencia: str) -> list:
        """Busca productos por atributo 'Sku OEM' (referencia de cartucho)."""
        try:
            attr_vals = odoo.search_read(
                "product.attribute.value",
                [("attribute_id", "=", _ATTR_SKU_OEM),
                 ("name", "ilike", referencia)],
                ["id"], 10
            )
            if not attr_vals:
                return []
            av_ids = [v["id"] for v in attr_vals]
            ptavs = odoo.search_read(
                "product.template.attribute.value",
                [("product_attribute_value_id", "in", av_ids)],
                ["product_tmpl_id"], 20
            )
            if not ptavs:
                return []
            # Filtrar templates no publicados
            pub_tmpls = odoo.search_read(
                "product.template",
                [("id", "in", list({p["product_tmpl_id"][0] for p in ptavs if p.get("product_tmpl_id")})),
                 ("active", "=", True)],
                ["id"], 20,
            )
            tmpl_ids = [t["id"] for t in pub_tmpls]
            if not tmpl_ids:
                return []
            prods = odoo.search_read(
                "product.product",
                [("product_tmpl_id", "in", tmpl_ids),
                 ("sale_ok", "=", True),
                 ("active", "=", True)],
                ["id", "name", "default_code", "qty_available", "product_tmpl_id"], 12,
                context=_wh_ctx if _warehouse_id else None,
            )
            if _warehouse_id:
                prods = [p for p in prods if (p.get("qty_available") or 0) > 0]
            return prods[:6]
        except Exception as e:
            logger.warning("_buscar_por_sku_oem: %s", e)
            return []

    def _enrich_prods(prods):
        """Agrega jpc_pos_name y URL del sitio web (megatoner.co) a cada product.product."""
        if not prods:
            return prods
        tmpl_ids = []
        for p in prods:
            tmpl_raw = p.get("product_tmpl_id")
            if isinstance(tmpl_raw, (list, tuple)) and tmpl_raw:
                tmpl_ids.append(int(tmpl_raw[0]))
            elif tmpl_raw:
                tmpl_ids.append(int(tmpl_raw))
        if not tmpl_ids:
            for p in prods:
                p.setdefault("product_url", "")
            return prods
        try:
            tmpls = odoo.search_read(
                "product.template",
                [("id", "in", tmpl_ids)],
                ["id", "jpc_pos_name", "name", "website_url"],
                limit=10,
            )
            pos_name_map = {t["id"]: (t.get("jpc_pos_name") or t.get("name") or "") for t in tmpls}
            url_map = {
                t["id"]: ("https://www.megatoner.co" + t["website_url"])
                if t.get("website_url") else ""
                for t in tmpls
            }
            for p in prods:
                tmpl_raw = p.get("product_tmpl_id")
                tid = int(tmpl_raw[0]) if isinstance(tmpl_raw, (list, tuple)) else (int(tmpl_raw) if tmpl_raw else None)
                if tid:
                    p["name"] = pos_name_map.get(tid, p.get("name", p.get("default_code", "")))
                    p["product_url"] = url_map.get(tid, "")
                else:
                    p["product_url"] = ""
        except Exception as e:
            logger.warning("_enrich_prods: %s", e)
            for p in prods:
                p.setdefault("product_url", "")
        return prods

    def _build_resumen(ref, prods):
        """Resumen de variantes: emoji-primero, ref+color, sku-oem, precio por linea.

        Formato:
          Tenemos el *HP 206A* en 4 colores, todos sin chip:

          ⚫ 206A Negro (W2110A) — $64,800
          🔵 206A Cian (W2111A) — $64,800
          🔴 206A Magenta (W2112A) — $64,800
          🟡 206A Amarillo (W2113A) — $64,800

          ⚠️ Estos son *sin chip* — el chip se reutiliza del toner original.
          ¿Cuáles colores necesitas y en qué cantidad? 😊
        """
        _COLOR_KWS = [kw for kw in _COLOR_ORDEN if 'chip' not in kw]

        def _map_color(color_val):
            n = (color_val or '').lower()
            for kw in _COLOR_KWS:
                if kw in n:
                    return kw.capitalize(), _COLOR_EMOJI.get(kw, '\u2022')
            label = (color_val or '').split(' -')[0].strip().split('/')[0].strip()
            return label, '\u2022'

        # Enriquecer items con atributos
        items = []
        for p in prods:
            color_label, color_emoji = _map_color(p.get('color_attr', ''))
            chip_lower = p.get('chip_attr', '').lower()
            sku_oem = p.get('sku_oem', '')
            referencia = p.get('referencia_attr', '')

            # Determinar chip_label desde el atributo (no desde el nombre del producto)
            if 'chip integrado' in chip_lower or 'con chip' in chip_lower:
                chip_label = 'Con chip'
                chip_emoji = '🔧'
            elif 'sin chip' in chip_lower:
                chip_label = 'Sin chip'
                chip_emoji = '🔩'
            else:
                chip_label = ''
                chip_emoji = color_emoji  # mantener emoji de color si no hay chip

            # Si no hay referencia del atributo, extraerla del nombre (ej: "206A" de jpc_pos_name)
            if not referencia:
                m = re.search(r'\b([A-Z0-9]{3,6}[A-Z]|[0-9]{3,4})\b', p.get('name') or '')
                referencia = m.group(1) if m else ''

            # Si no hay sku_oem del atributo, intentar extraer del nombre
            if not sku_oem:
                m = re.search(r'\b([A-Z]{1,2}\d{3,5}[A-Z])\b', p.get('name') or '')
                sku_oem = m.group(1) if m else ''

            price = None
            if _partner_id:
                price = _get_product_price_for_client(p['id'], _partner_id)

            items.append({
                'product_id': p['id'],
                'referencia': referencia,
                'color_label': color_label,
                'color_emoji': color_emoji,
                'chip_lower': chip_lower,
                'chip_label': chip_label,
                'chip_emoji': chip_emoji,
                'sku_oem': sku_oem,
                'price': price,
                'en_stock': p.get('en_stock', True),
                'categ_name': (p.get('categ_name') or '').lower(),
                'jpc_marca': (p.get('jpc_marca') or '').strip(),
            })

        # Chip uniforme
        todos_sin_chip = bool(items) and all('sin chip' in i['chip_lower'] for i in items)
        todos_con_chip = bool(items) and all(
            ('con chip' in i['chip_lower'] or 'chip integrado' in i['chip_lower'])
            for i in items
        )

        # Detectar si es mixto (Original + Megatoner según jpc_marca)
        _es_meg = lambda i: 'megatoner' in (i.get('jpc_marca') or '').lower()
        _n_meg = sum(1 for i in items if _es_meg(i))
        _es_mixto = bool(items) and 0 < _n_meg < len(items)

        # Es escenario de colores: todos con color reconocido; no aplica si es mixto
        _COLOR_SET = {
            'negro', 'cian', 'magenta', 'amarillo', 'azul', 'rojo',
            'violeta', 'verde', 'gris', 'photo', 'cyan', 'yellow', 'black', 'blanco', 'color',
        }
        es_colores = (not _es_mixto) and bool(items) and all(
            any(ck in (i['color_label'] or '').lower() for ck in _COLOR_SET)
            for i in items
        )
        # Si hay colores repetidos (ej: 2 variantes "Negro"), el color NO es el
        # diferenciador — no es escenario de colores (será chip u opciones).
        _colores_unicos = {(i['color_label'] or '').strip().lower() for i in items}
        if es_colores and len(_colores_unicos) < len(items):
            es_colores = False

        # Familia de referencia (usar referencia del primer item si todos la tienen)
        refs = list({i['referencia'] for i in items if i['referencia']})
        familia = refs[0] if len(refs) == 1 else ref

        # Encabezado
        chip_str = 'sin chip' if todos_sin_chip else ('con chip' if todos_con_chip else '')
        # Detectar si el chip es el único diferenciador (colores iguales, chip distinto o todos con chip)
        _color_labels = [i['color_label'] for i in items]
        _chip_labels = [i['chip_label'] for i in items]
        # Chips distintos entre items (ej: uno con chip y otro sin chip) →
        # el chip es el diferenciador real cuando el color se repite.
        _chips_distintos = len(set(_chip_labels)) > 1 and any(_chip_labels)
        _use_chip_per_line = (
            (bool(chip_str) or _chips_distintos) and
            not es_colores and
            (len(set(_color_labels)) <= 1 or not all(_color_labels))
        )
        if es_colores:
            header = f'Tenemos el *{familia}* en {len(items)} colores'
        elif len(items) == 2 and (chip_str or _chips_distintos):
            header = f'Tenemos el *{familia}* en 2 versiones'
        else:
            header = f'Tenemos *{familia}* en {len(items)} opciones'
        if chip_str and not _use_chip_per_line:
            header += f', todos {chip_str}'
        header += ':'

        def _fmt_line(i, tipo_label='', use_chip=False):
            ref_part = f' ({i["sku_oem"]})' if i['sku_oem'] else ''
            # Cuando chip es el diferenciador, usar chip_label como etiqueta principal
            if use_chip and i.get('chip_label'):
                nombre = i['chip_label']
                if i['referencia'] and i['referencia'] != familia:
                    nombre = f'{nombre} — {i["referencia"]}'
                emoji = i.get('chip_emoji', i['color_emoji'])
            else:
                nombre = f'{i["referencia"]} {i["color_label"]}' if i['referencia'] else i['color_label']
                emoji = i['color_emoji']
            if tipo_label:
                nombre = f'{nombre} {tipo_label}'
            line = f'* {emoji} *{nombre}*{ref_part}'
            if i['price'] and i['price'] > 0:
                line += f' — {_fmt_currency(i["price"])}'
            if not i['en_stock']:
                line += ' ❌ agotado'
            return line

        if _es_mixto:
            meg_items = [i for i in items if _es_meg(i)]
            orig_items = [i for i in items if not _es_meg(i)]
            lines = [header, '']
            if meg_items:
                lines.append('Compatible Megatoner')
                for i in meg_items:
                    lines.append(_fmt_line(i, 'Megatoner'))
            if orig_items:
                if meg_items:
                    lines.append('')
                lines.append('Originales')
                for i in orig_items:
                    lines.append(_fmt_line(i, 'Original'))
            lines.append('')
            lines.append('¿Cuál versión y color necesitas y en qué cantidad? \U0001f60a')
        else:
            lines = [header, '']
            for i in items:
                lines.append(_fmt_line(i, use_chip=_use_chip_per_line))
            lines.append('')
            if todos_sin_chip:
                lines.append(
                    '⚠️ Estos son *sin chip* — el chip se reutiliza del tóner original.'
                )
                lines.append('')
            elif _chips_distintos and any('sin chip' in i['chip_lower'] for i in items):
                lines.append(
                    '⚠️ El *sin chip* requiere reutilizar el chip del tóner original.'
                )
                lines.append('')
            lines.append(
                '¿Cuáles colores necesitas y en qué cantidad? \U0001f60a'
                if es_colores else
                '¿Cuál opción necesitas y en qué cantidad? \U0001f60a'
            )

        texto = '\n'.join(lines)
        logger.info('_build_resumen: formateado ref=%r prods=%d', ref, len(prods))
        # Mapa nombre→product_id para que el LLM no re-busque al agregar al carrito
        _pid_lines = []
        for i in items:
            _tipo = 'Megatoner' if _es_meg(i) else ('Original' if _es_mixto else '')
            _lbl = f'{i["color_label"]} {_tipo}'.strip() if _tipo else i['color_label']
            # Si el chip es el diferenciador, incluirlo en la etiqueta para que
            # el LLM no confunda dos items con el mismo color (ej: 2x "Negro").
            if _use_chip_per_line and i['chip_label']:
                _lbl = f'{i["chip_label"]} {_lbl}'.strip()
            _pid_lines.append(f'  {_lbl} → product_id={i["product_id"]}')
        _pid_map = '\n'.join(_pid_lines)
        return (
            texto +
            f'\n\n[INSTRUCCION: Envía este texto EXACTAMENTE al cliente sin modificar.\n'
            f'NO envíes tarjetas individuales (enviar_tarjeta_producto) ni llames buscar_referencias_mensaje para estos productos — ya fue presentado el resumen.\n'
            f'Para agregar al carrito cuando el cliente elija, usa estos product_id (NO vuelvas a buscar):\n'
            f'{_pid_map}\n'
            f'Si el cliente vuelve a preguntar por catálogo, llama buscar_producto de nuevo.]'
        )


    def _buscar_repuesto_directo(tipo, texto_ref):
        """Busca repuestos por nombre de producto (no por search terms de cartuchos).

        Retorna (tmpl_ids, parciales). tmpl_ids = matches confiables por
        referencia/modelo; parciales = nombres de candidatos que solo
        coinciden por marca u otra palabra (pueden no ser compatibles).
        """
        import re as _re
        aliases = _PART_ALIAS_ALL if tipo == 'repuesto' else _PART_TYPES[tipo]
        # Tokens de referencia: el texto sin las palabras de repuesto
        ref_rep = _PART_ALIAS_RE.sub(' ', texto_ref or '')
        rep_tokens = [t for t in _re.findall(r'[a-zA-Z0-9]+', ref_rep) if len(t) >= 3]
        # Candidatos: nombre contiene un alias del tipo; el tipo generico
        # ademas incluye la categoria de repuestos.
        name_dom = [('name', 'ilike', a) for a in aliases]
        dom = ['|'] * (len(name_dom) - 1) + name_dom
        if tipo == 'repuesto':
            dom = ['|', ('categ_id.complete_name', 'ilike', 'repuesto')] + dom
        try:
            cands = odoo.search_read(
                'product.template', [('active', '=', True)] + dom,
                ['id', 'name'], limit=80,
            )
        except Exception as e:
            logger.warning('_buscar_repuesto_directo(%s): %s', tipo, e)
            return [], []

        def _tok_in_name(tok, nm):
            nm = nm or ''
            if _re.fullmatch(r'\d+', tok):
                return bool(_re.search(r'(?<!\d)' + _re.escape(tok) + r'(?!\d)', nm))
            return tok.lower() in nm.lower()

        # Token con digitos = referencia/modelo (fuerte); sin digitos = marca (debil)
        strong = [t for t in rep_tokens if _re.search(r'\d', t)]
        weak = [t for t in rep_tokens if not _re.search(r'\d', t)]
        hits = [c for c in cands if any(_tok_in_name(t, c['name']) for t in strong)]
        if not strong and not weak:
            # Pregunta generica ("¿venden almohadillas?") → mostrar lo que hay
            hits = cands[:5]
        if hits:
            return [c['id'] for c in hits[:5]], []
        parciales = [c['name'] for c in cands
                     if any(_tok_in_name(t, c['name']) for t in weak)][:3]
        return [], parciales

    def _buscar_producto_core(referencia: str):
        """Busca productos y retorna (prods, ids, ref_display, busqueda_tipo).

        busqueda_tipo: 'exacta' | 'similar' | 'atributo' | 'ninguna'
                       | 'repuesto_no_encontrado:<tipo>[:parciales]'.
        Cuando busqueda_tipo != 'exacta', se envia aviso previo al cliente.
        """
        import re as _re

        ref = referencia.strip()
        # NOTA: 'tambor' y 'drum' NO van aqui \u2014 son tipos de repuesto, no ruido.
        _MODIFIERS = _re.compile(
            r'\b(toner|t[o\u00f3]ner|cartucho|tinta|ink|cartridge|'
            r'sin|con|chip|negro|color|original|compatible|nuevo|nueva|'
            r'para|de|del|la|el|los|las|y|o|a|en|un|una)\b',
            _re.IGNORECASE,
        )
        ref_clean = _MODIFIERS.sub(' ', ref).strip()
        ref_clean = _re.sub(r' {2,}', ' ', ref_clean)
        if ref_clean:
            ref = ref_clean
        tokens = sorted(_re.findall(r'[a-zA-Z0-9]+', ref), key=len, reverse=True)

        def _tmpl_to_prods(tmpl_ids):
            if not tmpl_ids:
                return []
            tmpls = odoo.search_read(
                "product.template",
                [("id", "in", tmpl_ids)],
                ["id", "jpc_pos_name", "name", "website_url", "categ_id", "jpc_marca"],
                limit=20,
            )
            pos_name_map = {t["id"]: (t["jpc_pos_name"] or t["name"]) for t in tmpls}
            url_map = {
                t["id"]: ("https://www.megatoner.co" + t["website_url"])
                if t.get("website_url") else ""
                for t in tmpls
            }
            # categ_id viene como [id, "Categoria / Subcategoria"]
            categ_map = {
                t["id"]: (t["categ_id"][1] if isinstance(t["categ_id"], (list, tuple)) and len(t["categ_id"]) > 1 else "")
                for t in tmpls
            }
            marca_map = {t["id"]: (t.get("jpc_marca") or "").strip() for t in tmpls}
            # Obtener TODAS las variantes (incluyendo agotados) para mostrar resumen de colores.
            # qty_available en domain no respeta warehouse context en Odoo 19 — filtrar en Python.
            all_variants = odoo.search_read(
                "product.product",
                [("product_tmpl_id", "in", tmpl_ids), ("active", "=", True)],
                ["id", "default_code", "qty_available", "product_tmpl_id", "display_name"],
                limit=30,
                context=_wh_ctx if _warehouse_id else None,
            )

            # Agrupar por template — una entrada por template con representante e info de variantes
            _by_tmpl = {}
            for p in all_variants:
                tid = p["product_tmpl_id"][0] if isinstance(p["product_tmpl_id"], list) else p["product_tmpl_id"]
                _by_tmpl.setdefault(tid, []).append(p)

            def _var_label(p, tmpl_name):
                dn = p.get("display_name", "") or ""
                # "Template Name (Atributo)" → extraer el atributo entre paréntesis
                if "(" in dn and dn.endswith(")"):
                    return dn[dn.rfind("(")+1:-1].strip()
                code = p.get("default_code", "")
                return code if code else tmpl_name

            # ── Atributos Color, Chip y Sku OEM por template ────────────────
            _tmpl_attr_map = {}
            try:
                _ptav = odoo.search_read(
                    "product.template.attribute.value",
                    [("product_tmpl_id", "in", tmpl_ids), ("ptav_active", "=", True)],
                    ["product_tmpl_id", "attribute_id", "product_attribute_value_id"],
                    limit=500,
                )
                for _r in _ptav:
                    _tid = _r["product_tmpl_id"][0] if isinstance(_r["product_tmpl_id"], list) else _r["product_tmpl_id"]
                    _aname = (_r["attribute_id"][1] if isinstance(_r["attribute_id"], list) else "").strip().lower()
                    _vname = (_r["product_attribute_value_id"][1] if isinstance(_r["product_attribute_value_id"], list) else "").strip()
                    _tmpl_attr_map.setdefault(_tid, {})[_aname] = _vname
            except Exception as _ae:
                logger.warning("_tmpl_to_prods: attrs error: %s", _ae)

            result = []
            for tmpl_id in tmpl_ids:
                variants = _by_tmpl.get(tmpl_id, [])
                if not variants:
                    continue
                tmpl_name = pos_name_map.get(tmpl_id, "")
                tmpl_url = url_map.get(tmpl_id, "")
                in_stock = [p for p in variants if (p.get("qty_available") or 0) > 0]
                agotados = [p for p in variants if (p.get("qty_available") or 0) <= 0]
                rep = dict(in_stock[0] if in_stock else agotados[0])
                rep["name"] = tmpl_name
                rep["product_url"] = tmpl_url
                rep["en_stock"] = bool(in_stock)
                rep["categ_name"] = categ_map.get(tmpl_id, "")
                rep["jpc_marca"] = marca_map.get(tmpl_id, "")
                # Atributos clave para resumen de colores
                _tattrs = _tmpl_attr_map.get(tmpl_id, {})
                rep["color_attr"] = _tattrs.get("color", "")
                rep["chip_attr"] = _tattrs.get("chip", "")
                _raw_sku = _tattrs.get("sku oem", "")
                rep["sku_oem"] = _raw_sku.split(":", 1)[-1].strip() if ":" in _raw_sku else _raw_sku
                _raw_ref = _tattrs.get("referencia", "")
                rep["referencia_attr"] = _raw_ref.split(":", 1)[-1].strip() if ":" in _raw_ref else _raw_ref
                # Resumen de variantes solo cuando hay más de 1 variante
                if len(variants) > 1:
                    rep["variantes_resumen"] = [
                        {"nombre": _var_label(v, tmpl_name), "en_stock": (v.get("qty_available") or 0) > 0}
                        for v in (in_stock + agotados)
                    ]
                else:
                    rep["variantes_resumen"] = []
                result.append(rep)
            return result[:20]

        def _exact(term, tmpl_filter=None):
            # Dos pasos: primero encontrar IDs de terminos exactos, luego templates.
            term_recs = odoo.search_read(
                "jpc.product.search.term",
                [("name", "=ilike", term)],
                ["id"], limit=20,
            )
            if not term_recs:
                return []
            term_ids = [t["id"] for t in term_recs]
            domain = [("jpc_search_exact_ids", "in", term_ids), ("active", "=", True)]
            if tmpl_filter is not None:
                domain.append(("id", "in", tmpl_filter))
            recs = odoo.search_read("product.template", domain, ["id"], limit=10)
            return [r["id"] for r in recs]

        def _similar(term, tmpl_filter=None):
            domain = [("jpc_search_similar_ids.name", "ilike", term), ("active", "=", True)]
            if tmpl_filter is not None:
                domain.append(("id", "in", tmpl_filter))
            recs = odoo.search_read("product.template", domain, ["id"], limit=10)
            return [r["id"] for r in recs]

        def _similar_numero_completo(token, tmpl_filter=None):
            """Similar con limite de numero completo: '141' no devuelve '1410' ni '5141'."""
            nums = _re.findall(r'\d+', token)
            if not nums:
                return _similar(token, tmpl_filter)
            num_str = nums[0]
            boundary = _re.compile(r'(?<!\d)' + _re.escape(num_str) + r'(?!\d)', _re.IGNORECASE)
            term_recs = odoo.search_read(
                "jpc.product.search.term",
                [("name", "ilike", num_str), ("search_type", "=", "similar")],
                ["id", "name"], limit=100,
            )
            matching_ids = [r["id"] for r in term_recs if boundary.search(r["name"])]
            if not matching_ids:
                return []
            domain = [("jpc_search_similar_ids", "in", matching_ids), ("active", "=", True)]
            if tmpl_filter is not None:
                domain.append(("id", "in", tmpl_filter))
            recs = odoo.search_read("product.template", domain, ["id"], limit=10)
            return [r["id"] for r in recs]

        # ── Detección de marca OEM ────────────────────────────────────────────
        def _get_marca_filter(ref_text):
            """Detecta marca OEM en el texto. Retorna (tmpl_filter, ref_sin_marca).
            tmpl_filter es None si no hay marca o no hay productos con esa marca.

            Reconoce la marca como palabra separada ("HP 106A") y también
            pegada al inicio de un token seguido de dígitos ("HP4103"). En
            este segundo caso SOLO dispara si el prefijo coincide con una
            marca/alias real de jpc.product.brand.oem — nunca con una letra
            genérica. Esto evita falsos positivos: "M400"/"P1102" son
            nomenclatura propia del fabricante (MFP/Printer), no marcas, y
            deben quedar intactos.
            """
            try:
                marcas = odoo.search_read(
                    "jpc.product.brand.oem",
                    [("active", "=", True)],
                    ["id", "name", "aliases"],
                    limit=100,
                )
            except Exception:
                return None, ref_text
            if not marcas:
                return None, ref_text
            lookup = {}
            for m in marcas:
                lookup[m["name"].strip().lower()] = m["id"]
                for alias in (m.get("aliases") or "").split(","):
                    a = alias.strip().lower()
                    if a:
                        lookup[a] = m["id"]
            ref_tokens = _re.findall(r'[a-zA-Z0-9]+', ref_text.lower())

            brand_id = None
            brand_token = None      # texto exacto a reemplazar en ref_text
            brand_replacement = ''  # por qué reemplazarlo (vacío o el resto pegado)

            # 1) Marca como palabra separada (comportamiento original).
            for tok in ref_tokens:
                if tok in lookup:
                    brand_id = lookup[tok]
                    brand_token = tok
                    break

            # 2) Marca pegada al inicio de un token, seguida de dígito
            #    (ej. "HP4103" -> marca "hp" + resto "4103"). Se prueban las
            #    claves de marca más largas primero (ej. "hpe" antes que
            #    "hp") para no cortar de más.
            if not brand_id:
                marca_keys_sorted = sorted(lookup.keys(), key=len, reverse=True)
                for tok in ref_tokens:
                    for key in marca_keys_sorted:
                        if len(tok) > len(key) and tok.startswith(key) and tok[len(key)].isdigit():
                            brand_id = lookup[key]
                            brand_token = tok
                            brand_replacement = tok[len(key):]
                            break
                    if brand_id:
                        break

            if not brand_id:
                return None, ref_text
            try:
                # SIN límite bajo: con limit=500 y 519 templates HP, los últimos
                # en orden alfabético (ej: "Tóner HP U4...") quedaban fuera del
                # filtro y eran invisibles en toda búsqueda que mencionara "hp"
                tmpls = odoo.search_read(
                    "product.template",
                    [("jpc_marca_oem_id", "=", brand_id), ("active", "=", True)],
                    ["id"], limit=5000,
                )
            except Exception:
                return None, ref_text
            if not tmpls:
                return None, ref_text
            tmpl_ids_marca = [t["id"] for t in tmpls]
            ref_sin = _re.sub(
                r'(?<![a-zA-Z0-9])' + _re.escape(brand_token) + r'(?![a-zA-Z0-9])',
                brand_replacement, ref_text, flags=_re.IGNORECASE,
            ).strip()
            ref_sin = _re.sub(r' {2,}', ' ', ref_sin).strip()
            logger.debug("_buscar_producto_core: marca OEM detectada id=%s token=%r, ref_sin=%r", brand_id, brand_token, ref_sin)
            return tmpl_ids_marca, ref_sin or ref_text

        _tmpl_filter, ref_busq = _get_marca_filter(ref)
        tokens_busq = sorted(_re.findall(r'[a-zA-Z0-9]+', ref_busq), key=len, reverse=True) if ref_busq != ref else tokens

        # ── Ruta dedicada para repuestos: NUNCA responder con cartuchos ─────
        _repuesto_tipo = _detectar_repuesto(referencia)
        if _repuesto_tipo:
            rep_tmpl_ids, rep_parciales = _buscar_repuesto_directo(_repuesto_tipo, ref)
            if rep_tmpl_ids:
                prods = _tmpl_to_prods(rep_tmpl_ids)
                if prods:
                    ids = [p["id"] for p in prods if p.get("en_stock")]
                    logger.info(
                        "_buscar_producto_core: repuesto '%s' tipo=%s -> tmpl=%s",
                        ref, _repuesto_tipo, rep_tmpl_ids,
                    )
                    return prods, ids, ref, 'exacta'
            bt = 'repuesto_no_encontrado:%s' % _repuesto_tipo
            if rep_parciales:
                bt += ':' + ' | '.join(rep_parciales)
            logger.info(
                "_buscar_producto_core: repuesto '%s' tipo=%s sin match (parciales=%s)",
                ref, _repuesto_tipo, rep_parciales,
            )
            return [], [], ref, bt

        prods = []
        busqueda_tipo = 'ninguna'

        # Fase 1: busqueda exacta en jpc_search_exact_ids
        tmpl_ids = _exact(ref_busq, _tmpl_filter)
        if not tmpl_ids:
            for tok in tokens_busq:
                if len(tok) < 3:
                    continue
                tmpl_ids = _exact(tok, _tmpl_filter)
                if tmpl_ids:
                    break
        if tmpl_ids:
            busqueda_tipo = 'exacta'
            prods = _tmpl_to_prods(tmpl_ids)

        # Fase 2: busqueda similar (silenciosa — el aviso se lanza solo si llega a Fase 3)
        # Refs "cortas" (<2 digitos, ej. U4, M6) NUNCA entran aqui: el ilike
        # parcial de "similar" es demasiado propenso a falsos positivos con
        # un solo digito. Puro-digito sigue exigiendo 3+ (ambiguo con cantidad).
        def _valido_para_similar(t):
            if _re.fullmatch(r'\d+', t):
                return len(t) >= 3
            return len(_re.findall(r'\d', t)) >= 2

        if not prods and _valido_para_similar(ref_busq):
            busqueda_tipo = 'similar'
            valid_tokens = [t for t in tokens_busq if _valido_para_similar(t)]

            tmpl_ids = _similar(ref_busq, _tmpl_filter)
            if not tmpl_ids:
                if len(valid_tokens) >= 2:
                    # Interseccion de tokens: evita que un token generico devuelva demasiados
                    token_results = []
                    for tok in valid_tokens:
                        # Numero puro: respetar limite (141 != 1410)
                        if _re.fullmatch(r'\d+', tok):
                            tok_ids = set(_similar_numero_completo(tok, _tmpl_filter))
                        else:
                            tok_ids = set(_similar(tok, _tmpl_filter))
                        if not tok_ids:
                            base = _re.sub(r'[a-zA-Z]+$', '', tok)
                            if base and base != tok and len(base) >= 3:
                                if _re.fullmatch(r'\d+', base):
                                    tok_ids = set(_similar_numero_completo(base, _tmpl_filter))
                                else:
                                    tok_ids = set(_similar(base, _tmpl_filter))
                        if tok_ids:
                            token_results.append(tok_ids)
                    if token_results:
                        token_results.sort(key=lambda s: len(s))
                        intersection = token_results[0]
                        for other in token_results[1:]:
                            narrowed = intersection & other
                            if narrowed:
                                intersection = narrowed
                        tmpl_ids = list(intersection)
                    else:
                        tmpl_ids = []
                else:
                    # Un solo token
                    for tok in valid_tokens:
                        if _re.fullmatch(r'\d+', tok):
                            tmpl_ids = _similar_numero_completo(tok, _tmpl_filter)
                        else:
                            tmpl_ids = _similar(tok, _tmpl_filter)
                        if tmpl_ids:
                            break
                        base = _re.sub(r'[a-zA-Z]+$', '', tok)
                        if base and base != tok and len(base) >= 3:
                            if _re.fullmatch(r'\d+', base):
                                tmpl_ids = _similar_numero_completo(base, _tmpl_filter)
                            else:
                                tmpl_ids = _similar(base, _tmpl_filter)
                            if tmpl_ids:
                                break
            if tmpl_ids:
                # Ambigüedad de marca: el cliente no dio marca (_tmpl_filter is None,
                # _get_marca_filter no detectó HP/Samsung/etc en el mensaje) y el match
                # por número suelto cae en templates de MARCAS OEM distintas (ej: HP
                # LaserJet 2015 vs Samsung ML-2015, mismo "2015" por coincidencia).
                # Si el cliente ya dio la marca, _tmpl_filter ya viene acotado y esto
                # nunca dispara.
                if _tmpl_filter is None and len(tmpl_ids) > 1:
                    try:
                        _marca_check = odoo.read(
                            "product.template", tmpl_ids, ["jpc_marca_oem_id"],
                        )
                        _marcas_presentes = {
                            m["jpc_marca_oem_id"][1] for m in _marca_check
                            if m.get("jpc_marca_oem_id")
                        }
                    except Exception as _e:
                        logger.warning("_buscar_producto_core: error chequeando marca ambigua: %s", _e)
                        _marcas_presentes = set()
                    if len(_marcas_presentes) > 1:
                        _marcas_txt = ", ".join(sorted(_marcas_presentes))
                        logger.info(
                            "_buscar_producto_core: '%s' ambiguo entre marcas %s (tmpl=%s)",
                            ref, _marcas_txt, tmpl_ids,
                        )
                        return [], [], ref, f'ambigua_marca:{_marcas_txt}'
                prods = _tmpl_to_prods(tmpl_ids)

        # Fase 3: fallback por atributos e impresoras (con aviso al cliente)
        if not prods:
            if ch_id and _bot_id:
                try:
                    odoo.execute_kw(
                        'jpc.whatsapp.bot.config', 'enviar_aviso_busqueda_similar',
                        [[_bot_id], ch_id, ref],
                    )
                except Exception as _e:
                    logger.warning("_buscar_producto_core: aviso atributo error: %s", _e)
            prods = _enrich_prods(_buscar_por_atributo_impresora(ref))
            if prods:
                busqueda_tipo = 'atributo'
        if not prods:
            prods = _enrich_prods(_buscar_por_sku_oem(ref))
            if prods:
                busqueda_tipo = 'atributo'

        # Asegurar flag en_stock para resultados de fallback (atributo/sku que no pasan por _tmpl_to_prods)
        for p in prods:
            if "en_stock" not in p:
                p["en_stock"] = (p.get("qty_available") or 0) > 0
        # Política de precio del bot: ocultar productos sin regla en la pricelist
        if _price_fallback == 'hide' and prods:
            _con_precio = [p for p in prods if _tiene_precio_canal(p["id"])]
            if len(_con_precio) != len(prods):
                logger.info(
                    "_buscar_producto_core: %d/%d producto(s) oculto(s) sin regla en pricelist=%s ref=%r",
                    len(prods) - len(_con_precio), len(prods), _bot_pricelist_id, ref,
                )
            if not _con_precio:
                return [], [], ref, 'sin_precio_canal'
            prods = _con_precio
        # ids solo incluye productos en stock (para envío de tarjetas)
        ids = [p["id"] for p in prods if p.get("en_stock")]
        return prods, ids, ref, busqueda_tipo

    def _chip_label(chip_attr: str) -> str:
        """'Con chip'/'Sin chip' a partir del valor crudo del atributo Chip, o ''
        si el producto no tiene ese atributo. Cuando hay un solo valor (caso
        normal fuera de _build_resumen), esto es un HECHO ya confirmado —
        no algo que el LLM deba preguntarle al cliente."""
        c = (chip_attr or '').lower()
        if 'sin chip' in c:
            return 'Sin chip'
        if 'con chip' in c or 'chip integrado' in c:
            return 'Con chip'
        return ''

    def _format_producto_output(ref: str, prods: list, tarjeta_result: str) -> str:
        lines = [f"Encontré {len(prods)} producto(s) para '{ref}'."]
        hay_agotados = any(not p.get("en_stock", True) for p in prods)
        hay_chip_info = False
        for p in prods:
            en_stock = p.get("en_stock", True)
            qty = p.get("qty_available", 0)
            url_txt = p.get("product_url", "")
            chip_txt = _chip_label(p.get("chip_attr", ""))
            if chip_txt:
                hay_chip_info = True

            if not en_stock:
                # Producto agotado — no tiene precio ni tarjeta
                parts = [
                    f"NOMBRE_EXACTO:{p.get('name', '')}",
                    f"ID:{p['id']}",
                    f"Ref:{p.get('default_code', 'N/A')}",
                ]
                if chip_txt:
                    parts.append(f"Chip:{chip_txt}")
                parts.append("❌ AGOTADO")
                lines.append("  - " + " | ".join(parts))
            else:
                stock_txt = f"Stock:{qty:.0f}" if qty is not None else ""
                precio_str = ""
                if _partner_id:
                    price = _get_product_price_for_client(p["id"], _partner_id)
                    if price is not None:
                        precio_str = _fmt_currency(price)
                parts = [
                    f"NOMBRE_EXACTO:{p.get('name', '')}",
                    f"ID:{p['id']}",
                    f"Ref:{p.get('default_code', 'N/A')}",
                ]
                if chip_txt:
                    parts.append(f"Chip:{chip_txt}")
                if stock_txt:
                    parts.append(stock_txt)
                if precio_str:
                    parts.append(f"PRECIO_x1:{precio_str}")
                if url_txt:
                    parts.append(f"URL_PRODUCTO:{url_txt}")
                lines.append("  - " + " | ".join(parts))

            # Resumen de variantes de color (cuando hay más de 1 variante bajo el mismo template)
            variantes = p.get("variantes_resumen", [])
            if variantes:
                partes_var = []
                for v in variantes:
                    icon = "✅" if v["en_stock"] else "❌ agotado"
                    partes_var.append(f"{v['nombre']} {icon}")
                lines.append(f"    🎨 Variantes: {' | '.join(partes_var)}")

        lines.append("")
        if tarjeta_result:
            lines.append("✅ Tarjetas enviadas al cliente con imagen, precio y enlace.")
            lines.append("⚠️ TU RESPUESTA DE TEXTO AL CLIENTE debe ser MUY CORTA:")
            lines.append("   - Lista los productos disponibles con viñeta (* NOMBRE_EXACTO)")
            if hay_agotados:
                lines.append("   - MENCIONA los productos agotados (❌ AGOTADO) e indica que no están disponibles")
            if any(p.get("variantes_resumen") for p in prods):
                lines.append("   - Si hay variantes de color, menciona cuáles están disponibles y cuáles agotadas")
            lines.append("   - NO incluyas precio, stock ni URLs — las tarjetas ya los tienen")
            if hay_chip_info:
                lines.append("   - Chip:X ya es un dato CONFIRMADO del producto — NO le preguntes al cliente si lo quiere con o sin chip")
            lines.append("   - Termina con la pregunta de intención")
            lines.append(tarjeta_result)
        else:
            lines.append("⚠️ REGLAS OBLIGATORIAS PARA TU RESPUESTA AL CLIENTE:")
            lines.append("1. Usa EXACTAMENTE el NOMBRE_EXACTO. No cambies, abrevies ni reformules el nombre del producto.")
            lines.append("2. El precio que menciones al cliente DEBE SER EXACTAMENTE el PRECIO_x1 (precio unitario a 1 unidad con la pricelist del cliente).")
            lines.append("3. Si incluyes un enlace, usa EXACTAMENTE la URL_PRODUCTO. NUNCA inventes ni construyas un enlace diferente.")
            if hay_agotados:
                lines.append("4. Para productos ❌ AGOTADO: infórmale al cliente que no está disponible. No des precio ni URL.")
            if hay_chip_info:
                lines.append("5. Chip:X ya es un dato CONFIRMADO del producto — NO le preguntes al cliente si lo quiere con o sin chip.")
        return "\n".join(lines)

    @tool
    def buscar_producto(referencia: str) -> str:
        """Buscar productos por referencia de cartucho o modelo de impresora.
        SOLO para consulta de precio SIN intención de compra: envía tarjetas WhatsApp
        con imagen y precio. Usa NOMBRE_EXACTO, PRECIO_x1 y URL_PRODUCTO retornados,
        textualmente.
        """
        prods, ids, ref, busqueda_tipo = _buscar_producto_core(referencia)
        if not prods:
            if busqueda_tipo == 'sin_precio_canal':
                return (
                    f"Encontré '{referencia}' en catálogo pero SIN precio configurado "
                    f"para la lista de precios de este canal.\n"
                    f"NO inventes ni informes precio de lista. Dile al cliente que vas a "
                    f"confirmar el precio con un asesor y usa escalar_a_asesor con motivo "
                    f"'Producto sin precio en lista del canal: {referencia}'."
                )
            if busqueda_tipo.startswith('repuesto_no_encontrado'):
                return _msg_repuesto_no_encontrado(busqueda_tipo, referencia)
            if busqueda_tipo.startswith('ambigua_marca'):
                _marcas = busqueda_tipo.split(':', 1)[1] if ':' in busqueda_tipo else ''
                return (
                    f"La referencia '{referencia}' coincide con productos de más de una "
                    f"marca ({_marcas}) — es un número de modelo que distintos fabricantes "
                    f"reutilizan. NO elijas ni cotices ninguno todavía. Pregúntale al "
                    f"cliente cuál marca/impresora tiene (ej: '¿tu impresora es {_marcas}?') "
                    f"y vuelve a llamar esta tool con la marca incluida en la referencia."
                )
            return (
                f"No encontre productos para '{referencia}'.\n"
                f"Dile al cliente algo como: 'No encuentro esta referencia en nuestro "
                f"catálogo actualmente, pero no te preocupes 😊 Voy a transferir tu "
                f"conversación a uno de nuestros asesores para que consulte la "
                f"disponibilidad con nuestros proveedores locales y pueda prepararte "
                f"una cotización ajustada a lo que necesitas.' Luego usa escalar_a_asesor "
                f"con motivo 'Producto no encontrado en catálogo: {referencia}'."
            )
        logger.info("buscar_producto: '%s' -> IDs=%s", ref, ids)
        # Resumen de variantes: mixto (Megatoner+Original) -> todo junto en secciones
        _meg_prods = [p for p in prods if _es_megatoner(p)]
        _es_mixto_pre = bool(_meg_prods) and len(_meg_prods) < len(prods)
        if _es_mixto_pre:
            if _trigger_resumen(prods):
                return _build_resumen(ref, prods)
        else:
            if _trigger_resumen(_meg_prods):
                return _build_resumen(ref, _meg_prods)
            if _trigger_resumen(prods):
                return _build_resumen(ref, prods)
        # Registrar en carrito — precio_mostrado=True porque la tarjeta incluye precio
        if ids and ch_id:
            _upsert_carrito_linea(
                product_id=ids[0],
                product_name=prods[0].get("name") or ref,
                search_term=ref,
                tarjeta_mostrada=True,
                precio_mostrado=True,
            )
        tarjeta_result = _enviar_tarjetas_auto(ids, max_cards=3)
        return _format_producto_output(ref, prods, tarjeta_result)

    @tool
    def buscar_producto_cotizacion(referencia: str) -> str:
        """Buscar productos para cotización: envía tarjetas WhatsApp y registra hasta 3
        resultados en el carrito con precio a qty=1. USAR con intención de compra.
        Si el cliente elige uno: seleccionar_linea_carrito(product_id).
        Si pide otra cantidad: obtener_precio(product_id, partner_id, qty).
        """
        prods, ids, ref, busqueda_tipo = _buscar_producto_core(referencia)
        if not prods:
            if busqueda_tipo == 'sin_precio_canal':
                return (
                    f"Encontré '{referencia}' en catálogo pero SIN precio configurado "
                    f"para la lista de precios de este canal.\n"
                    f"NO inventes ni informes precio de lista. Dile al cliente que vas a "
                    f"confirmar el precio con un asesor y usa escalar_a_asesor con motivo "
                    f"'Producto sin precio en lista del canal: {referencia}'."
                )
            if busqueda_tipo.startswith('repuesto_no_encontrado'):
                return _msg_repuesto_no_encontrado(busqueda_tipo, referencia)
            if busqueda_tipo.startswith('ambigua_marca'):
                _marcas = busqueda_tipo.split(':', 1)[1] if ':' in busqueda_tipo else ''
                return (
                    f"La referencia '{referencia}' coincide con productos de más de una "
                    f"marca ({_marcas}) — es un número de modelo que distintos fabricantes "
                    f"reutilizan. NO elijas ni cotices ninguno todavía. Pregúntale al "
                    f"cliente cuál marca/impresora tiene (ej: '¿tu impresora es {_marcas}?') "
                    f"y vuelve a llamar esta tool con la marca incluida en la referencia."
                )
            return (
                f"No encontre productos para '{referencia}'.\n"
                f"Dile al cliente algo como: 'No encuentro esta referencia en nuestro "
                f"catálogo actualmente, pero no te preocupes 😊 Voy a transferir tu "
                f"conversación a uno de nuestros asesores para que consulte la "
                f"disponibilidad con nuestros proveedores locales y pueda prepararte "
                f"una cotización ajustada a lo que necesitas.' Luego usa escalar_a_asesor "
                f"con motivo 'Producto no encontrado en catálogo: {referencia}'."
            )
        logger.info("buscar_producto_cotizacion: '%s' -> IDs=%s", ref, ids)
        # Resumen de variantes: mixto (Megatoner+Original) -> todo junto en secciones
        _meg_prods2 = [p for p in prods if _es_megatoner(p)]
        _es_mixto_pre2 = bool(_meg_prods2) and len(_meg_prods2) < len(prods)
        if _es_mixto_pre2:
            if _trigger_resumen(prods):
                return _build_resumen(ref, prods)
        else:
            if _trigger_resumen(_meg_prods2):
                return _build_resumen(ref, _meg_prods2)
            if _trigger_resumen(prods):
                return _build_resumen(ref, prods)
        # Enviar tarjetas WhatsApp para todos los productos encontrados
        tarjeta_result = _enviar_tarjetas_auto(ids, max_cards=3)
        # Registrar TODOS los productos en el carrito con precio a qty=1
        if ids and ch_id:
            for prod_data, prod_id in zip(prods[:3], ids[:3]):
                prod_name = prod_data.get("jpc_pos_name") or prod_data.get("name") or ref
                precio_unit = 0.0
                precio_total = 0.0
                precio_mostrado = False
                if _partner_id:
                    try:
                        order_id = odoo.create("sale.order", {
                            "partner_id": _partner_id,
                            "state": "draft",
                            "payment_method_id": 215,
                        })
                        line_id = odoo.create("sale.order.line", {
                            "order_id": order_id,
                            "product_id": prod_id,
                            "product_uom_qty": 1.0,
                        })
                        sol_lines = odoo.read("sale.order.line", [line_id],
                                              ["price_unit", "price_total"])
                        odoo.execute_kw("sale.order", "unlink", [[order_id]])
                        if sol_lines:
                            precio_unit = float(sol_lines[0].get("price_unit") or 0)
                            precio_total = float(sol_lines[0].get("price_total") or 0)
                            precio_mostrado = True
                    except Exception as e:
                        logger.warning("buscar_producto_cotizacion precio product=%s: %s", prod_id, e)
                _upsert_carrito_linea(
                    product_id=prod_id,
                    product_name=prod_name,
                    search_term=ref,
                    tarjeta_mostrada=True,
                    precio_mostrado=precio_mostrado,
                    precio_unit=precio_unit,
                    precio_total=precio_total,
                )
        return _format_producto_output(ref, prods, tarjeta_result)

    # ── PRECIOS ─────────────────────────────────────────────────────────────────

    @tool
    def obtener_precio(product_id: int, partner_id: int, qty: float = 1.0) -> str:
        """Obtener el precio real de un producto para un cliente con su lista de precios.

        USAR SIEMPRE antes de informar un precio al cliente.
        Aplica la pricelist real del cliente en Odoo (descuentos, condiciones especiales).
        """
        if _price_fallback == 'hide' and not _tiene_precio_canal(product_id):
            return (
                f"❌ El producto {product_id} NO tiene precio configurado en la lista "
                f"de precios de este canal. NO informes precio de lista ni lo inventes. "
                f"Dile al cliente que confirmas el precio con un asesor y usa "
                f"escalar_a_asesor si lo necesita."
            )
        try:
            order_vals = {
                "partner_id": partner_id,
                "state": "draft",
                "payment_method_id": 215,
            }
            if _bot_pricelist_id:
                order_vals["pricelist_id"] = _bot_pricelist_id
            order_id = odoo.create("sale.order", order_vals)
            line_id = odoo.create("sale.order.line", {
                "order_id": order_id,
                "product_id": product_id,
                "product_uom_qty": qty,
            })
            lines = odoo.read("sale.order.line", [line_id],
                              ["price_unit", "price_subtotal", "price_total", "product_id", "discount"])
            odoo.execute_kw("sale.order", "unlink", [[order_id]])
            if not lines:
                return f"No se pudo calcular el precio del producto {product_id}."
            li = lines[0]
            price    = li.get("price_unit", 0)
            total    = li.get("price_total", 0)
            discount = li.get("discount", 0)
            prod_name = (li.get("product_id") or [None, str(product_id)])[1]
            desc_txt = f" (descuento {discount:.0f}%)" if discount else ""
            # Actualizar carrito: precio y cantidad solicitada
            if ch_id:
                upd = dict(
                    product_id=product_id,
                    product_name=prod_name,
                    precio_mostrado=True,
                    precio_unit=float(price),
                    precio_total=float(total),
                )
                if qty != 1.0:
                    upd["qty_solicitada"] = qty
                _upsert_carrito_linea(**upd)
            return (
                f"💰 {prod_name}\n"
                f"Precio unitario: {_fmt_currency(price)}{desc_txt}\n"
                f"Total con IVA ({qty:.0f} uds): {_fmt_currency(total)}"
            )
        except Exception as e:
            logger.exception("obtener_precio: error")
            try:
                prods = odoo.read("product.product", [product_id], ["name", "lst_price"])
                if prods:
                    p = prods[0]
                    return (
                        f"💰 {p['name']}\n"
                        f"Precio base: {_fmt_currency(p['lst_price'])}\n"
                        f"(precio aproximado, pricelist no disponible)"
                    )
            except Exception:
                pass
            return f"❌ Error consultando precio: {str(e)[:200]}"

    @tool
    def crear_cotizacion(partner_id: int, nota: str = "") -> str:
        """Crear una cotización vacía para un cliente. Retorna el ORDER_ID numérico que
        debes usar en agregar_linea_cotizacion y generar_link_cotizacion — no confundir
        con invoice_id.
        """
        # Usar la empresa (commercial_partner_id) en lugar del contacto individual
        effective_partner_id = _commercial_id(partner_id) or partner_id
        vals = {
            "partner_id": effective_partner_id,
            "state": "draft",
            "payment_method_id": 215,
        }
        if _bot_pricelist_id:
            vals["pricelist_id"] = _bot_pricelist_id
        if nota:
            vals["note"] = nota
        new_id = odoo.create("sale.order", vals)
        recs = odoo.read("sale.order", [new_id], ["name"])
        name = recs[0]["name"] if recs else str(new_id)
        return (
            f"COTIZACION_CREADA: {name} | ORDER_ID={new_id}\n"
            f"Menciona el numero {name} en tu respuesta al cliente.\n"
            f"Para el link de pago usa: generar_link_cotizacion(order_id={new_id})\n"
            f"NUNCA uses generar_link_pago ni enviar_link_pago_whatsapp con este ID — son para facturas."
        )

    @tool
    def agregar_linea_cotizacion(order_id: int, product_id: int, cantidad: float = 1.0) -> str:
        """Agregar o actualizar un producto en una cotización (si la línea ya existe,
        actualiza la cantidad). Aplica la pricelist del cliente automáticamente.
        order_id = ID numérico de BD retornado por crear_cotizacion — NO el número
        del nombre de la cotización (S47836 tiene un ID interno distinto).
        """
        order_id, order_rec = _resolver_order_id_cliente(order_id, estados=("draft", "sent"))
        if not order_id:
            return (
                f"❌ Cotización no encontrada o no pertenece al cliente actual. "                f"Llama crear_cotizacion() para crear una nueva."
            )
        orders = [order_rec]

        _stock_error = _verificar_stock_o_sugerir(product_id, cantidad)
        if _stock_error:
            return _stock_error

        prods = odoo.read("product.product", [product_id], ["name", "jpc_pos_name", "default_code"])
        if not prods:
            return f"Producto {product_id} no encontrado."
        order_name = orders[0]["name"]
        ref = prods[0].get("default_code") or ""
        nombre = prods[0].get("jpc_pos_name") or prods[0]["name"]
        label = ref if ref else nombre

        # Si el producto ya está en la orden, actualizar cantidad en vez de agregar línea nueva
        existing = odoo.search_read(
            "sale.order.line",
            [("order_id", "=", order_id), ("product_id", "=", product_id)],
            ["id"], limit=1,
        )
        if existing:
            line_id = existing[0]["id"]
            odoo.execute_kw("sale.order.line", "write", [[line_id], {"product_uom_qty": cantidad}])
            line_data = odoo.read("sale.order.line", [line_id], ["price_unit", "price_total"])
            price_unit  = line_data[0]["price_unit"]  if line_data else 0
            price_total = line_data[0]["price_total"] if line_data else 0
            _actualizar_carrito_linea_cotizada(product_id, nombre, line_id, cantidad, order_id)
            return (f"✅ {order_name} — {label} × {cantidad:.0f} uds = "
                    f"{_fmt_currency(price_total)} (IVA incluido) [actualizado]")

        line_id = odoo.create("sale.order.line", {
            "order_id": order_id, "product_id": product_id,
            "product_uom_qty": cantidad,
        })
        line_data = odoo.read("sale.order.line", [line_id], ["price_unit", "price_total"])
        price_unit  = line_data[0]["price_unit"]  if line_data else 0
        price_total = line_data[0]["price_total"] if line_data else 0
        _actualizar_carrito_linea_cotizada(product_id, nombre, line_id, cantidad, order_id)
        return (f"✅ {order_name} — {label} × {cantidad:.0f} uds = "
                f"{_fmt_currency(price_total)} (IVA incluido)")

    @tool
    def obtener_cotizacion(order_id: int) -> str:
        """Obtener detalles de una cotización: cliente, estado, líneas, subtotal, IVA y total."""
        recs = odoo.read("sale.order", [order_id],
                         ["name","partner_id","state","amount_untaxed","amount_tax",
                          "amount_total","date_order","order_line"])
        if not recs:
            return f"Cotización {order_id} no encontrada."
        o = recs[0]
        state_map = {"draft":"Borrador","sent":"Enviada","sale":"Confirmada",
                     "cancel":"Cancelada","done":"Finalizada"}
        lines_text = ""
        if o.get("order_line"):
            lines = odoo.read("sale.order.line", o["order_line"],
                              ["product_id","product_uom_qty","price_unit","price_subtotal"])
            prod_ids = [l["product_id"][0] for l in lines if l.get("product_id")]
            prod_names = {}
            if prod_ids:
                pp = odoo.read("product.product", prod_ids, ["jpc_pos_name", "name", "default_code"])
                prod_names = {p["id"]: (p.get("jpc_pos_name") or p.get("name") or p.get("default_code")) for p in pp}
            items = [f"  • {prod_names.get(l['product_id'][0], _m2o(l.get('product_id')))} "
                     f"{_fmt_currency(l.get('price_unit',0))} × {l.get('product_uom_qty',0):.0f} = "
                     f"{_fmt_currency(l.get('price_subtotal',0))}" for l in lines]
            lines_text = "\nProductos:\n" + "\n".join(items)
        return (f"Cotización: {o.get('name')}\nCliente: {_m2o(o.get('partner_id'))}\n"
                f"Estado: {state_map.get(o.get('state',''), o.get('state',''))}\n"
                f"Fecha: {_fmt_date(str(o.get('date_order','') or ''))}\n"
                f"Subtotal (antes de IVA): {_fmt_currency(o.get('amount_untaxed',0))}\n"
                f"IVA: {_fmt_currency(o.get('amount_tax',0))}\n"
                f"Total (con IVA): {_fmt_currency(o.get('amount_total',0))}{lines_text}\n\n"
                f"IMPORTANTE: la diferencia entre el subtotal y el total es SIEMPRE el IVA. "
                f"NO hay cargo de envío/domicilio salvo que aparezca como línea de producto "
                f"explícita arriba — nunca inventes ni asumas un costo de envío.")

    @tool
    def registrar_espera_respuesta(mensaje_followup: str = "") -> str:
        """Registra que el bot quedó esperando respuesta del cliente. Llamar al final de
        cada respuesta que espera decisión (tarjetas de precio, resumen de cotización,
        pregunta). Si no responde en 5 min, Odoo envía mensaje_followup automáticamente —
        nunca lo dejes vacío; usa un texto acorde al contexto
        (ej: "¿Te interesa que te prepare una cotización? 😊").
        """
        if not _session_id:
            return "ok (sin sesión)"
        try:
            odoo.execute_kw(
                "jpc.ai.agent.session", "action_set_followup",
                [[_session_id], mensaje_followup or "", 5],
            )
            return "⏳ Follow-up programado en 5 min si no hay respuesta."
        except Exception as e:
            logger.warning("registrar_espera_respuesta: %s", e)
            return "ok"

    @tool
    def confirmar_orden(order_id: int) -> str:
        """Confirmar una cotización y convertirla en pedido de venta, SIN facturar.

        USAR para contra entrega, y también para clientes de CRÉDITO (ver
        "Términos de pago" en consultar_datos_facturacion): en ambos casos
        se confirma el pedido pero la factura la genera después un asesor
        manualmente — no factures tú automáticamente, reduce errores y deja
        el control de facturación al equipo de cartera.
        """
        order_id, order_rec = _resolver_order_id_cliente(order_id, estados=("draft", "sent"))
        if not order_id:
            return (
                f"❌ Cotización no encontrada o no pertenece al cliente actual — "                f"NO se confirmó nada. Verifica el ORDER_ID (usa el numérico de "                f"crear_cotizacion/agregar_linea_cotizacion, no los dígitos del nombre)."
            )
        try:
            odoo.execute_kw("sale.order", "action_confirm", [[order_id]])
            recs = odoo.read("sale.order", [order_id], ["name", "amount_total"])
            if not recs:
                return f"✅ Orden {order_id} confirmada."
            name = recs[0]["name"]
            total = recs[0].get("amount_total", 0)
            return (f"✅ Orden {name} confirmada — Total: {_fmt_currency(total)}. "
                    f"Usa este total exacto al confirmarle al cliente, no un valor calculado de memoria.")
        except Exception as e:
            return f"Error al confirmar: {str(e)}"

    @tool
    def enviar_cotizacion_whatsapp(order_id: int) -> str:
        """Obtener el enlace de portal de una cotización para compartir con el cliente.

        REGLA: Esta herramienta genera el ÚNICO link válido de cotización.
        NUNCA construyas ni inventes el link — siempre llama esta herramienta.
        Envía al cliente el texto completo que retorna esta herramienta sin modificarlo.
        """
        # Garantizar que el access_token exista antes de leerlo
        try:
            odoo.execute_kw("sale.order", "_portal_ensure_token", [[order_id]])
        except Exception:
            pass
        recs = odoo.read("sale.order", [order_id], ["name","amount_total","partner_id","access_token"])
        if not recs:
            return f"Cotización {order_id} no encontrada."
        o = recs[0]
        token = o.get("access_token") or ""
        url = f"{_base_url()}/my/orders/{order_id}"
        if token:
            url += f"?access_token={token}"
        return (f"📋 *{o.get('name')}* — {_fmt_currency(o.get('amount_total',0))}\n"
                f"🔗 {url}\n"
                f"(enlace para {_m2o(o.get('partner_id'))})")

    # ── FACTURAS ──────────────────────────────────────────────────────────

    @tool
    def listar_facturas(partner_id: int, limit: int = 5, nombre: str = "") -> str:
        """Listar las facturas de un cliente. Si el cliente pide una factura por número
        (ej: POS3247), pasa nombre='POS3247' para filtrar. Incluye ID interno para usar
        con enviar_factura_whatsapp o enviar_factura_pdf_whatsapp."""
        domain = [
            ("partner_id", "child_of", _commercial_id(partner_id) or partner_id),
            ("move_type", "in", ["out_invoice", "out_receipt"]),
            ("state", "=", "posted"),
        ]
        if nombre:
            domain.append(("name", "ilike", nombre))
        invs = odoo.search_read(
            "account.move", domain,
            ["id","name","invoice_date","amount_total","amount_residual","payment_state"],
            limit)
        if not invs:
            msg = f"No se encontró la factura '{nombre}' para este cliente." if nombre else \
                  "No se encontraron facturas para este cliente."
            return msg
        state_map = {"not_paid":"Pendiente","partial":"Parcial","paid":"Pagada",
                     "in_payment":"En pago","reversed":"Anulada"}
        lines = []
        for inv in invs:
            state = state_map.get(inv.get("payment_state",""), inv.get("payment_state",""))
            e = "✅" if inv.get("payment_state") == "paid" else "⏳"
            lines.append(f"{e} {inv.get('name')} (ID:{inv.get('id')}) | "
                         f"{_fmt_date(str(inv.get('invoice_date','') or ''))} | "
                         f"Total:{_fmt_currency(inv.get('amount_total',0))} | "
                         f"Saldo:{_fmt_currency(inv.get('amount_residual',0))} | {state}")
        return "\n".join(lines)

    @tool
    def obtener_factura(invoice_id: int) -> str:
        """Obtener detalles de una factura. SOLO usar con IDs obtenidos de listar_facturas
        para el partner del cliente actual — nunca con IDs inventados o de sesiones anteriores."""
        if not _partner_id:
            return "Error: no hay partner_id del cliente en el contexto."
        recs = odoo.read("account.move", [invoice_id],
                         ["name","partner_id","invoice_date","invoice_date_due",
                          "amount_total","amount_residual","payment_state"])
        if not recs:
            return f"Factura {invoice_id} no encontrada."
        inv = recs[0]
        # Validar que la factura pertenece al cliente
        inv_partner = inv.get("partner_id")
        inv_partner_id = inv_partner[0] if isinstance(inv_partner, (list, tuple)) else inv_partner
        allowed = odoo.search_read(
            "res.partner", [("id", "child_of", _commercial_id() or _partner_id), ("id", "=", inv_partner_id)], ["id"], 1)
        if not allowed:
            return f"Error: la factura {inv.get('name')} no pertenece a este cliente."
        state_map = {"not_paid":"Pendiente","partial":"Pago parcial","paid":"Pagada",
                     "in_payment":"En pago","reversed":"Anulada"}
        return (f"Factura: {inv.get('name')}\nCliente: {_m2o(inv.get('partner_id'))}\n"
                f"Fecha: {_fmt_date(str(inv.get('invoice_date','') or ''))}\n"
                f"Vencimiento: {_fmt_date(str(inv.get('invoice_date_due','') or ''))}\n"
                f"Total: {_fmt_currency(inv.get('amount_total',0))}\n"
                f"Saldo: {_fmt_currency(inv.get('amount_residual',0))}\n"
                f"Estado: {state_map.get(inv.get('payment_state',''), inv.get('payment_state',''))}")

    @tool
    def enviar_factura_whatsapp(invoice_id: int) -> str:
        """Generar link de portal de una factura. SOLO usar con IDs obtenidos de listar_facturas
        para el cliente actual. NUNCA usar IDs de sesiones anteriores ni inventados.
        NO usar cuando el cliente pide 'copia de factura' — usar enviar_factura_pdf_whatsapp.
        """
        if not _partner_id:
            return "Error: no hay partner_id del cliente en el contexto."
        recs = odoo.read("account.move", [invoice_id],
                         ["name","amount_total","amount_residual","partner_id"])
        if not recs:
            return f"Factura {invoice_id} no encontrada."
        inv = recs[0]
        # Validar que la factura pertenece al cliente
        inv_partner = inv.get("partner_id")
        inv_partner_id = inv_partner[0] if isinstance(inv_partner, (list, tuple)) else inv_partner
        allowed = odoo.search_read(
            "res.partner", [("id", "child_of", _commercial_id() or _partner_id), ("id", "=", inv_partner_id)], ["id"], 1)
        if not allowed:
            return f"Error: la factura {inv.get('name')} no pertenece a este cliente. Usa listar_facturas para obtener IDs válidos."
        try:
            odoo.execute_kw("account.move", "_portal_ensure_token", [[invoice_id]])
        except Exception:
            pass
        _efw_token = (odoo.read("account.move", [invoice_id], ["access_token"]) or [{}])[0].get("access_token") or ""
        url = f"{_base_url()}/my/invoices/{invoice_id}"
        if _efw_token:
            url += f"?access_token={_efw_token}"
        return (f"📄 *{inv.get('name')}*\n"
                f"Total:{_fmt_currency(inv.get('amount_total',0))} | "
                f"Pendiente:{_fmt_currency(inv.get('amount_residual',0))}\n"
                f"🔗 {url}")

    @tool
    def estado_de_cuenta(partner_id: int) -> str:
        """Obtener el estado de cuenta del cliente: deuda total, deuda vencida y el
        detalle de facturas pendientes. Cada línea ya trae la marca "⚠️ VENCIDA"
        cuando corresponde — es un cálculo exacto por fecha, NO la recalcules ni la
        infieras tú (ej. asumiendo que solo la más antigua está vencida): repite
        tal cual qué facturas están marcadas."""
        from datetime import date as _date

        invs = odoo.search_read(
            "account.move",
            [("partner_id","child_of",_commercial_id(partner_id) or partner_id),("move_type","=","out_invoice"),
             ("payment_state","in",["not_paid","partial"])],
            ["name","invoice_date_due","amount_residual"], 20)
        if not invs:
            return "✅ El cliente no tiene facturas pendientes."

        invs.sort(key=lambda i: i.get("invoice_date_due") or "")
        hoy = _date.today()
        total = sum(i.get("amount_residual",0) for i in invs)
        total_vencido = 0.0
        detalle = []
        for inv in invs:
            due_str = str(inv.get("invoice_date_due","") or "")
            vencida = False
            if due_str:
                try:
                    vencida = _date.fromisoformat(due_str[:10]) < hoy
                except ValueError:
                    pass
            if vencida:
                total_vencido += inv.get("amount_residual",0)
            marca = " ⚠️ VENCIDA" if vencida else ""
            detalle.append(f"  • {inv.get('name')} | Vence:{_fmt_date(due_str)} | "
                            f"{_fmt_currency(inv.get('amount_residual',0))}{marca}")

        header = [f"💳 Deuda total: {_fmt_currency(total)}"]
        if total_vencido:
            header.append(f"⚠️ Deuda vencida: {_fmt_currency(total_vencido)}")
        header.append("")
        return "\n".join(header + detalle)

    @tool
    def generar_link_pago(invoice_id: int) -> str:
        """Generar link de pago para una FACTURA ya emitida (account.move).

        SOLO para facturas emitidas — NO para cotizaciones (sale.order).
        Si el cliente quiere pagar una COTIZACION usa generar_link_cotizacion(order_id=...).
        Si confundes order_id con invoice_id enviaras el link de otro cliente.
        """
        try:
            odoo.execute_kw("account.move", "_portal_ensure_token", [[invoice_id]])
        except Exception:
            pass
        recs = odoo.read("account.move", [invoice_id], ["name","amount_residual","move_type","access_token"])
        if not recs:
            return f"Factura {invoice_id} no encontrada."
        inv = recs[0]
        if inv.get("move_type","") not in ("out_invoice","out_receipt",""):
            return f"El ID {invoice_id} no es una factura de venta. Para cotizaciones usa generar_link_cotizacion."
        _glp_token = inv.get("access_token") or ""
        url = f"{_base_url()}/my/invoices/{invoice_id}"
        if _glp_token:
            url += f"?access_token={_glp_token}"
        return (f"🔗 Enlace de pago — {inv.get('name')}\n"
                f"Saldo: {_fmt_currency(inv.get('amount_residual',0))}\n{url}")

    @tool
    def enviar_link_pago_whatsapp(invoice_id: int) -> str:
        """Texto con link de pago de una FACTURA emitida (account.move) para WhatsApp.

        SOLO para facturas emitidas — NO para cotizaciones (sale.order).
        Si el cliente quiere pagar una COTIZACION usa generar_link_cotizacion(order_id=...).
        Si confundes order_id con invoice_id enviaras datos de otro cliente.
        """
        try:
            odoo.execute_kw("account.move", "_portal_ensure_token", [[invoice_id]])
        except Exception:
            pass
        recs = odoo.read("account.move", [invoice_id], ["name","amount_residual","access_token"])
        if not recs:
            return f"Factura {invoice_id} no encontrada."
        inv = recs[0]
        _elp_token = inv.get("access_token") or ""
        url = f"{_base_url()}/my/invoices/{invoice_id}"
        if _elp_token:
            url += f"?access_token={_elp_token}"
        return (f"💳 *{inv.get('name')}*\n"
                f"Valor a pagar: {_fmt_currency(inv.get('amount_residual',0))}\n"
                f"🔗 {url}")

    @tool
    def generar_link_cotizacion(order_id: int) -> str:
        """Generar el link del portal para que el cliente revise y pague su cotización.
        USAR cuando pidan "link de pago", "link", "el pago", "cómo pago". El pago
        confirma la orden automáticamente; el bot NO confirma ni factura.
        NUNCA uses confirmar_cotizacion_y_link_pago.
        """
        _completo, _faltan = _estado_datos_facturacion()
        if not _completo:
            return (
                f"⚠️ NO generes el link todavía — faltan datos de facturación del "
                f"cliente: {_faltan}. Llama consultar_datos_facturacion() para ver "
                f"el detalle completo, pídele SOLO lo que falta en un único mensaje "
                f"corto, y cuando responda llama completar_datos_facturacion con lo "
                f"que haya dado. Si el cliente se niega a darlos, usa "
                f"escalar_a_asesor con motivo 'Cliente no quiere dar datos de "
                f"facturación'. Recién con los datos completos vuelve a llamar "
                f"generar_link_cotizacion."
            )
        try:
            # Garantizar que el access_token exista (órdenes nuevas lo tienen vacío)
            try:
                odoo.execute_kw("sale.order", "_portal_ensure_token", [[order_id]])
            except Exception:
                pass
            orders = odoo.read("sale.order", [order_id],
                               ["name", "amount_total", "access_token", "state"])
            if not orders:
                return f"❌ Cotización ID:{order_id} no encontrada."
            order = orders[0]
            token = order.get("access_token") or ""
            # Si _portal_ensure_token no genero el token, escribirlo directamente
            if not token:
                import uuid as _uuid
                token = str(_uuid.uuid4())
                try:
                    odoo.write("sale.order", [order_id], {"access_token": token})
                except Exception:
                    token = ""
            url = f"{_base_url()}/my/orders/{order_id}"
            if token:
                url += f"?access_token={token}"
            return (
                f"💳 *{order['name']}*\n"
                f"Total: {_fmt_currency(order.get('amount_total', 0))}\n"
                f"🔗 {url}"
            )
        except Exception as e:
            logger.exception("generar_link_cotizacion: error")
            return f"❌ Error generando link: {str(e)[:200]}"

    @tool
    def confirmar_cotizacion_y_link_pago(order_id: int) -> str:
        """Confirmar una cotización y generar el link de pago para el cliente.

        USAR cuando el cliente acepta pagar. Confirma la orden → crea factura → retorna link.
        NUNCA uses generar_link_pago ni enviar_link_pago_whatsapp para cotizaciones — usa esta.
        """
        _completo, _faltan = _estado_datos_facturacion()
        if not _completo:
            return (
                f"⚠️ NO confirmes la orden todavía — faltan datos de facturación del "
                f"cliente: {_faltan}. Llama consultar_datos_facturacion() para ver "
                f"el detalle completo, pídele SOLO lo que falta en un único mensaje "
                f"corto, y cuando responda llama completar_datos_facturacion con lo "
                f"que haya dado. Si el cliente se niega a darlos, usa "
                f"escalar_a_asesor con motivo 'Cliente no quiere dar datos de "
                f"facturación'. Recién con los datos completos vuelve a llamar "
                f"confirmar_cotizacion_y_link_pago."
            )
        try:
            orders = odoo.read("sale.order", [order_id], ["name", "state"])
            if not orders:
                return f"❌ Cotización ID:{order_id} no encontrada."
            order = orders[0]
            order_name = order["name"]

            # Confirmar si sigue en borrador
            if order["state"] in ("draft", "sent"):
                odoo.execute_kw("sale.order", "action_confirm", [[order_id]])

            # Buscar factura existente ligada a esta orden
            existing = odoo.search_read(
                "account.move",
                [("invoice_origin", "=", order_name),
                 ("move_type", "=", "out_invoice"),
                 ("state", "!=", "cancel")],
                ["id", "name", "amount_residual", "state"], limit=1,
            )
            if existing:
                inv_rec = existing[0]
                invoice_id = inv_rec["id"]
                if inv_rec["state"] == "draft":
                    odoo.execute_kw("account.move", "action_post", [[invoice_id]])
            else:
                # Crear factura usando el wizard público (Odoo 19: _create_invoices es privado)
                wizard_id = odoo.create("sale.advance.payment.inv", {
                    "advance_payment_method": "delivered",
                    "sale_order_ids": [[6, 0, [order_id]]],
                })
                odoo.execute_kw("sale.advance.payment.inv", "create_invoices", [[wizard_id]])
                # Buscar la factura recién creada
                new_invs = odoo.search_read(
                    "account.move",
                    [("invoice_origin", "=", order_name),
                     ("move_type", "=", "out_invoice"),
                     ("state", "!=", "cancel")],
                    ["id", "state"], limit=1,
                )
                if not new_invs:
                    return f"❌ No se pudo crear la factura para {order_name}."
                invoice_id = new_invs[0]["id"]
                if new_invs[0]["state"] == "draft":
                    odoo.execute_kw("account.move", "action_post", [[invoice_id]])

            try:
                odoo.execute_kw("account.move", "_portal_ensure_token", [[invoice_id]])
            except Exception:
                pass
            inv_data = odoo.read("account.move", [invoice_id], ["name", "amount_residual", "access_token"])
            if not inv_data:
                return "❌ Error al leer la factura generada."
            inv = inv_data[0]
            _inv_token = inv.get("access_token") or ""
            url = f"{_base_url()}/my/invoices/{invoice_id}"
            if _inv_token:
                url += f"?access_token={_inv_token}"
            # Marcar carrito como confirmado
            try:
                if ch_id:
                    carriots = odoo.search_read(
                        "jpc.whatsapp.bot.carrito",
                        [["channel_id", "=", ch_id], ["sale_order_id", "=", order_id]],
                        ["id"], limit=1,
                    )
                    if carriots:
                        odoo.execute_kw(
                            "jpc.whatsapp.bot.carrito", "write",
                            [[carriots[0]["id"]], {"state": "confirmed"}],
                        )
            except Exception:
                pass
            return (
                f"✅ {order_name} confirmada.\n"
                f"💳 *{inv['name']}*\n"
                f"Valor a pagar: {_fmt_currency(inv.get('amount_residual', 0))}\n"
                f"🔗 {url}"
            )
        except Exception as e:
            logger.exception("confirmar_cotizacion_y_link_pago: error")
            return f"❌ Error al confirmar cotización: {str(e)[:200]}"

    # ── PEDIDOS ───────────────────────────────────────────────────────────

    @tool
    def listar_pedidos_cliente(partner_id: int, limit: int = 5, estado: str = "sale") -> str:
        """Listar pedidos/cotizaciones de un cliente, de la más reciente a la más antigua.
        estado: draft=borradores, sent=enviadas, sale=confirmados, done=entregados, all=todos.
        Incluye el ID interno para agregar_linea_cotizacion o estado_entrega.
        USAR PROACTIVAMENTE con estado="sale" cuando el cliente hable de compra recurrente
        ("lo de siempre", "el pedido habitual", "lo que compramos cada mes") para deducir
        qué necesita sin pedirle la referencia.
        """
        import datetime as _dt
        estado_filter = {
            "draft": ["draft"],
            "sent": ["sent"],
            "sale": ["sale"],
            "done": ["done"],
            "all": ["draft", "sent", "sale", "done"],
        }.get(estado, ["sale"])
        # child_of sobre la empresa: los pedidos quedan a nombre del commercial_partner_id
        # aunque el LLM pase el id del contacto que escribe
        domain = [
            ("partner_id", "child_of", _commercial_id(partner_id) or partner_id),
            ("state", "in", estado_filter),
        ]
        # Borradores: solo los creados hoy — evitar retomar cotizaciones de dias anteriores
        if estado == "draft":
            _hoy = _dt.datetime.utcnow().strftime("%Y-%m-%d")
            domain.append(("create_date", ">=", _hoy + " 00:00:00"))
        orders = odoo.search_read(
            "sale.order",
            domain,
            ["id","name","date_order","amount_total","state"], limit)
        if not orders:
            return f"No se encontraron pedidos con estado={estado} para este cliente."
        state_map = {"draft":"⏳ Borrador","sent":"Enviada","sale":"Confirmado","done":"Entregado","cancel":"Cancelado"}
        return "\n".join(
            f"📦 {o.get('name')} (ID:{o.get('id')}) | {_fmt_date(str(o.get('date_order','') or ''))} | "
            f"{_fmt_currency(o.get('amount_total',0))} | {state_map.get(o.get('state',''),o.get('state',''))}"
            for o in orders)
    _PICKING_FIELDS_ENTREGA = [
        "name", "state", "scheduled_date", "date_done",
        "x_entregado", "x_entregado_at", "carrier_id", "carrier_tracking_ref",
    ]

    def _carrier_map(picks):
        """delivery.carrier.id -> {'name','in_store'} para los pickings dados.

        in_store distingue 'recoger en tienda' (no hay transportista real,
        el cliente viene por el pedido) de un envío real (ruta propia o
        transportadora tercera)."""
        ids = {p["carrier_id"][0] for p in picks if p.get("carrier_id")}
        if not ids:
            return {}
        carriers = odoo.read("delivery.carrier", list(ids), ["name", "delivery_type", "tracking_url"])
        return {
            c["id"]: {
                "name": c.get("name") or "",
                "in_store": c.get("delivery_type") == "in_store",
                "tracking_url": (c.get("tracking_url") or "").strip(),
            }
            for c in carriers
        }

    def _tracking_link(url: str, guia: str) -> tuple:
        """(url_lista, lleva_guia) a partir del campo 'Enlace de seguimiento'
        (delivery.carrier.tracking_url). ('', False) si no hay nada configurado.

        Odoo documenta el placeholder `<shipmenttrackingnumber>` en ese campo
        (delivery_carrier.py: "Use <shipmenttrackingnumber> as a placeholder in
        your URL"). Se aceptan además otros marcadores comunes por si alguien
        los configuró a mano.

        Si la URL no trae placeholder, se entrega tal cual: es la página de
        rastreo genérica y el cliente debe teclear la guía ahí. `lleva_guia`
        distingue los dos casos para poder decírselo bien.

        Muchas se cargan sin protocolo ('www.x.com/rastreo'); WhatsApp no las
        vuelve enlace tocable, así que se les antepone https://.
        """
        url = (url or "").strip()
        if not url:
            return "", False
        guia = (guia or "").strip()
        lleva = False
        for marcador in ("<shipmenttrackingnumber>", "{tracking}", "{guia}", "{ref}", "%s"):
            if marcador in url:
                url = url.replace(marcador, guia)
                lleva = True
                break
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        return url, lleva

    def _fmt_estado_envio(pick, carriers):
        """(icono, estado, fecha, lineas_extra) para un stock.picking.

        x_entregado es la confirmación REAL de entrega al cliente (toggle manual
        del repartidor/ruta, ver jpc_custom stock_picking.py) — state=='done' solo
        significa que el picking se validó (mercancía alistada/despachada), NO que
        el cliente ya la recibió. No tratar 'done' como entregado.
        """
        carrier = pick.get("carrier_id")
        carrier_id = carrier[0] if isinstance(carrier, list) else carrier
        info = carriers.get(carrier_id, {})
        es_recoge_tienda = info.get("in_store", False)

        if pick.get("x_entregado"):
            icon, estado = "✅", "Entregado"
            fecha = _fmt_date(str(pick.get("x_entregado_at") or pick.get("date_done") or ""))
        elif pick.get("state") == "done":
            if es_recoge_tienda:
                icon, estado = "🏪", "Listo para recoger en tienda"
            else:
                icon, estado = "🚚", "Despachado (en camino — entrega aún no confirmada)"
            fecha = _fmt_date(str(pick.get("date_done") or pick.get("scheduled_date") or ""))
        else:
            pick_state_map = {"draft": "Borrador", "waiting": "Esperando",
                               "confirmed": "Confirmado", "assigned": "Listo para despachar"}
            icon, estado = "🚚", pick_state_map.get(pick.get("state", ""), pick.get("state", ""))
            fecha = _fmt_date(str(pick.get("scheduled_date") or ""))

        extra = []
        if not es_recoge_tienda and info.get("name"):
            extra.append(f"Transportista: {info['name']}")
        # Con guía de rastreo el envío NO va con nuestros mensajeros sino con una
        # transportadora externa: el cliente rastrea en el sitio de ella, y no hay
        # ETA de ruta ni firma nuestra que ofrecerle.
        guia = (pick.get("carrier_tracking_ref") or "").strip()
        if guia:
            extra.append(f"📦 Envío con transportadora externa | Guía de rastreo: {guia}")
            link, lleva_guia = _tracking_link(info.get("tracking_url", ""), guia)
            if link and lleva_guia:
                extra.append(f"    Link de rastreo directo: {link}")
                extra.append(
                    "    Ese link ya lleva la guía incrustada: dáselo al cliente "
                    "TEXTUALMENTE, tal cual, sin modificarlo ni acortarlo."
                )
            elif link:
                extra.append(f"    Página de rastreo: {link}")
                extra.append(
                    "    Esa página NO lleva la guía incrustada: dale el link Y la "
                    "guía, y aclárale que debe ingresar la guía ahí para ver el estado."
                )
            else:
                extra.append(
                    f"    No hay link de rastreo configurado para {info.get('name') or 'esta transportadora'}. "
                    "Dale la guía y el nombre de la transportadora para que rastree en el sitio de ella. "
                    "NO inventes una URL."
                )
        return icon, estado, fecha, extra

    # ── Módulo de ruta (jpc_ruta): hora real, novedad y firma ────────────────
    # Ventanas horarias por jornada. Salen de 983 entregas reales de 60 días
    # (percentiles 10-90 de la hora de finalización). NO usar eta_plan para
    # prometerle una hora al cliente: sobre 878 entregas, la mediana llega 73
    # min después del plan y solo 1 de cada 3 cae dentro de la hora siguiente.
    _VENTANA_JORNADA = {
        "manana": "entre las 9:00 a. m. y la 1:00 p. m.",
        "tarde": "entre las 12:00 m. y las 5:00 p. m.",
    }
    _NOVEDAD_LABEL = {
        "cerrado": "el local estaba cerrado",
        "no_recibieron": "no quisieron recibir el pedido",
        "direccion_errada": "la dirección estaba errada",
        "cliente_ausente": "el cliente no se encontraba",
        "sin_dinero": "no tenían el dinero para el pago contra entrega",
        "otro": "hubo una novedad",
    }
    _RUTA_FIELDS = [
        "picking_id", "ruta_id", "sequence", "is_delivered", "estado_parada",
        "novedad", "comentarios", "fecha_hora_finalizacion", "parent_jornada",
        "parent_date", "parent_state",
    ]

    def _hora_local(dt_utc: str) -> str:
        """'2026-07-31 20:36:14' (UTC) -> '3:36 p. m.' (Bogotá, UTC-5 fijo).

        Colombia no tiene horario de verano, así que el offset es constante.
        Se calcula aquí en vez de leer el campo *_display del modelo porque
        ese compute depende del timezone del usuario RPC (bot_agent), que no
        necesariamente es America/Bogota.
        """
        import datetime as _d
        try:
            dt = _d.datetime.strptime(str(dt_utc)[:19], "%Y-%m-%d %H:%M:%S") - _d.timedelta(hours=5)
            h = dt.hour % 12 or 12
            return f"{h}:{dt.minute:02d} {'a. m.' if dt.hour < 12 else 'p. m.'}"
        except Exception:
            return ""

    def _ruta_info(picking_ids: list) -> dict:
        """picking_id -> dict con estado de la parada en la ruta. {} si no aplica."""
        if not picking_ids:
            return {}
        try:
            lineas = odoo.search_read(
                "jpc.ruta.line",
                [("picking_id", "in", list(picking_ids))],
                _RUTA_FIELDS, limit=200, order="id desc",
            ) or []
        except Exception as e:
            logger.warning("_ruta_info: %s", e)
            return {}
        # Contar paradas por ruta para el "parada X de Y"
        ruta_ids = {l["ruta_id"][0] for l in lineas if l.get("ruta_id")}
        totales = {}
        for rid in ruta_ids:
            try:
                totales[rid] = odoo.execute_kw(
                    "jpc.ruta.line", "search_count", [[("ruta_id", "=", rid)]],
                )
            except Exception:
                pass
        # ¿Cuáles de esas paradas tienen firma REAL capturada? El Binary es
        # attachment=True, así que el archivo vive en ir.attachment. Hay que
        # comprobarlo: no todas las entregas quedan firmadas, y anunciarle al
        # modelo una firma inexistente lo lleva a prometérsela al cliente.
        con_firma = set()
        if lineas:
            try:
                atts = odoo.search_read(
                    "ir.attachment",
                    [("res_model", "=", "jpc.ruta.line"),
                     ("res_field", "=", "firma_cliente"),
                     ("res_id", "in", [l["id"] for l in lineas])],
                    ["res_id"], limit=200,
                ) or []
                con_firma = {a["res_id"] for a in atts}
            except Exception as e:
                logger.warning("_ruta_info firmas: %s", e)

        out = {}
        for l in lineas:
            pid = l["picking_id"][0] if l.get("picking_id") else None
            if not pid or pid in out:      # order=id desc -> nos quedamos con la más reciente
                continue
            rid = l["ruta_id"][0] if l.get("ruta_id") else None
            out[pid] = {
                "linea_id": l["id"],
                "tiene_firma": l["id"] in con_firma,
                "entregado": bool(l.get("is_delivered")),
                "estado": l.get("estado_parada") or "",
                "novedad": l.get("novedad") or "",
                "comentarios": (l.get("comentarios") or "").strip(),
                "hora_real": _hora_local(l.get("fecha_hora_finalizacion") or ""),
                "jornada": l.get("parent_jornada") or "",
                "fecha_ruta": l.get("parent_date") or "",
                "secuencia": l.get("sequence") or 0,
                "total_paradas": totales.get(rid, 0),
            }
        return out

    def _linea_ruta_texto(info: dict) -> list:
        """Líneas de texto para el agente a partir de la info de ruta."""
        if not info:
            return []
        out = []
        if info["entregado"]:
            if info["hora_real"]:
                out.append(f"🕒 Entregado a las {info['hora_real']} (dato real de la ruta)")
            if info["tiene_firma"]:
                out.append(
                    "📝 Hay firma de recibido: usa enviar_firma_entrega(picking_id=...) "
                    "SOLO si el cliente la pide o dice que no recibió el pedido."
                )
            else:
                out.append(
                    "📝 SIN firma digital de esta entrega — no le ofrezcas ni le prometas "
                    "una prueba de entrega firmada."
                )
        elif info["estado"] == "fallida" or info["novedad"]:
            motivo = _NOVEDAD_LABEL.get(info["novedad"], "hubo una novedad")
            out.append(f"⚠️ Entrega NO completada: {motivo}")
            if info["comentarios"]:
                out.append(f"    Nota del mensajero: {info['comentarios'][:200]}")
        else:
            pos = ""
            if info["secuencia"] and info["total_paradas"]:
                pos = f" (parada {info['secuencia']} de {info['total_paradas']})"
            ventana = _VENTANA_JORNADA.get(info["jornada"], "")
            if ventana:
                out.append(f"🚚 En ruta hoy{pos} — llega aproximadamente {ventana}")
            else:
                out.append(f"🚚 En ruta{pos}")
            out.append(
                "    IMPORTANTE: esa franja es un ESTIMADO de la ruta, no una hora exacta. "
                "Dísela como aproximada; NUNCA prometas una hora puntual."
            )
        return out

    @tool
    def estado_entrega(order_id: int) -> str:
        """Consultar el estado de entrega de un pedido: estado del pedido, de cada envío,
        y si está en ruta la posición y franja horaria aproximada. Si ya se entregó informa
        la hora real y si quedó firma de recibido."""
        recs = odoo.read("sale.order", [order_id], ["name","state","picking_ids"])
        if not recs:
            return f"Pedido {order_id} no encontrado."
        o = recs[0]
        order_state_map = {"sale":"Confirmado (activo)","done":"Cerrado","cancel":"Cancelado","draft":"Borrador"}
        order_state = order_state_map.get(o.get("state",""), o.get("state",""))
        lines = [f"Pedido: {o.get('name')} — Estado del pedido: {order_state}"]
        pids = o.get("picking_ids", [])
        if not pids:
            lines.append("  ⏳ Sin despacho generado aún")
        else:
            picks = odoo.read("stock.picking", pids, _PICKING_FIELDS_ENTREGA)
            active_picks = [p for p in picks if p.get("state") != "cancel"]
            cancelled_picks = [p for p in picks if p.get("state") == "cancel"]
            carriers = _carrier_map(active_picks)
            rutas = _ruta_info([p["id"] for p in active_picks])
            for pick in active_picks:
                icon, estado, fecha, extra = _fmt_estado_envio(pick, carriers)
                lines.append(
                    f"  {icon} {pick.get('name')} (picking_id={pick['id']}) | {estado} | {fecha}"
                )
                for ex in extra:
                    lines.append(f"      {ex}")
                # Datos del módulo de ruta (hora real, novedad, firma, posición).
                # Solo aplican a envíos con mensajero propio: si hay guía de
                # rastreo el paquete lo lleva una transportadora externa y no
                # existe parada de ruta ni firma nuestra.
                if not (pick.get("carrier_tracking_ref") or "").strip():
                    for rl in _linea_ruta_texto(rutas.get(pick["id"])):
                        lines.append(f"      {rl}")
            if not active_picks and cancelled_picks:
                lines.append("  ⚠️ El despacho fue cancelado pero el pedido sigue activo — puede estar pendiente de reprogramación")
        return "\n".join(lines)


    @tool
    def enviar_firma_entrega(picking_id: int) -> str:
        """Enviar al cliente por WhatsApp la firma que dio al recibir un despacho entregado.
        El picking_id sale de estado_entrega. Usar SOLO si el cliente pide la prueba de
        entrega o dice que no recibió el pedido. Si no hay firma capturada, díselo — no la
        inventes.
        """
        if not ch_id:
            return "Sin canal de WhatsApp activo — no se puede enviar la firma."
        try:
            return odoo.execute_kw(
                "jpc.whatsapp.bot.funciones.negocio",
                "action_enviar_firma_entrega_agente",
                [[]],
                {"picking_id": int(picking_id), "channel_id": ch_id},
            )
        except Exception as e:
            logger.exception("enviar_firma_entrega picking=%s", picking_id)
            return f"No se pudo enviar la firma: {str(e)[:200]}"

    @tool
    def consultar_pedidos_cliente(partner_id: int, limit: int = 5) -> str:
        """Consultar pedidos recientes del cliente con estado de pedido y estado de envío por separado (transportista y guía de rastreo si aplica)."""
        orders = odoo.search_read(
            "sale.order",
            [("partner_id", "=", partner_id), ("state", "in", ["sale", "done"])],
            ["name", "date_order", "amount_total", "state", "picking_ids"],
            limit,
        )
        if not orders:
            return "No se encontraron pedidos para este cliente."

        state_map_order = {"sale": "Confirmado (activo)", "done": "Cerrado", "cancel": "Cancelado"}

        lines = []
        for o in orders:
            fecha_pedido = _fmt_date(str(o.get("date_order", "") or ""))
            estado_pedido = state_map_order.get(o.get("state", ""), o.get("state", ""))
            total = _fmt_currency(o.get("amount_total", 0))
            lines.append(f"📦 *{o.get('name')}* — {fecha_pedido} | {total} | Pedido: {estado_pedido}")

            pids = o.get("picking_ids") or []
            if pids:
                picks = odoo.read("stock.picking", pids, _PICKING_FIELDS_ENTREGA)
                active = [p for p in picks if p.get("state") != "cancel"]
                cancelled = [p for p in picks if p.get("state") == "cancel"]
                carriers = _carrier_map(active)
                for pick in active:
                    icon, estado, fecha, extra = _fmt_estado_envio(pick, carriers)
                    lines.append(f"  {icon} Envío {pick.get('name')} | {estado} | {fecha}")
                    for ex in extra:
                        lines.append(f"      {ex}")
                if not active and cancelled:
                    lines.append("  ⚠️ Envío cancelado — pedido activo, despacho pendiente de reprogramación")
            else:
                lines.append("  ⏳ Sin despacho generado aún")

        return "\n".join(lines)


    @tool
    def enviar_factura_pdf_whatsapp(invoice_id: int) -> str:
        """Enviar la copia de una factura en PDF por WhatsApp y correo al cliente.
        USAR SIEMPRE que el cliente pida: "copia de factura", "envíame la factura",
        "necesito mi factura", "me mandas la factura", "factura en PDF".
        Requiere el ID interno obtenido de listar_facturas (el número después de "ID:").
        """
        if not ch_id:
            return "No hay canal activo para enviar el PDF."
        try:
            result = odoo.execute_kw(
                "jpc.whatsapp.bot.funciones.negocio",
                "action_enviar_factura_pdf_agente",
                [[]],
                {"invoice_id": invoice_id, "channel_id": ch_id},
            )
            return result or "✅ PDF enviado."
        except Exception as e:
            return f"Error enviando PDF: {str(e)}"

    @tool
    def enviar_factura_completa_correo(invoice_id: int, email: str) -> str:
        """Reenviar una factura YA emitida por correo con el paquete legal completo
        (PDF + XML/CUDE aceptado por la DIAN) — el mismo adjunto que se envía al
        validar la factura, no un PDF suelto.

        USAR cuando el cliente pida la factura "completa", "con el XML", "la
        electrónica", o la pida a un correo específico de facturación (puede ser
        distinto al registrado). NO uses enviar_factura_pdf_whatsapp para esto —
        esa solo manda un PDF por WhatsApp, sin XML.
        Requiere el ID interno obtenido de listar_facturas (el número después de "ID:").
        """
        try:
            result = odoo.execute_kw(
                "account.move", "jwb_enviar_factura_completa_correo",
                [[invoice_id], email],
            )
        except Exception as e:
            return f"Error enviando la factura: {str(e)[:200]}"

        if not result or not result.get("ok"):
            return f"❌ {(result or {}).get('error', 'No se pudo enviar la factura.')}"

        return (
            f"✅ Factura {result['factura']} enviada a {result['email']} "
            f"con el PDF y el XML/CUDE de la DIAN."
        )

    # ── TICKETS / SOPORTE ─────────────────────────────────────────────────

    @tool
    def listar_tickets_cliente(partner_id: int, limit: int = 5) -> str:
        """Listar los tickets de soporte de un cliente."""
        try:
            tickets = odoo.search_read(
                "helpdesk.ticket", [("partner_id","=",partner_id)],
                ["name","create_date","stage_id"], limit)
        except Exception:
            return "El módulo de helpdesk no está disponible."
        if not tickets:
            return "No se encontraron tickets para este cliente."
        return "\n".join(
            f"🎫 {t.get('name')} | {_fmt_date(str(t.get('create_date',''))[:10])} | "
            f"Estado:{_m2o(t.get('stage_id'))}"
            for t in tickets)

    @tool
    def crear_ticket_garantia(partner_id: int, asunto: str, descripcion: str = "") -> str:
        """Crear un ticket de soporte o garantía para un cliente."""
        try:
            ticket_id = odoo.create("helpdesk.ticket", {
                "name": asunto, "partner_id": partner_id, "description": descripcion})
            return f"✅ Ticket creado — ID:{ticket_id} | {asunto}"
        except Exception as e:
            return f"Error creando ticket: {str(e)}"

    @tool
    def obtener_ticket(ticket_id: int) -> str:
        """Obtener detalles de un ticket de soporte por su ID."""
        try:
            recs = odoo.read("helpdesk.ticket", [ticket_id],
                             ["name","partner_id","stage_id","description","create_date"])
            if not recs:
                return f"Ticket {ticket_id} no encontrado."
            t = recs[0]
            return (f"Ticket: {t.get('name')}\nCliente: {_m2o(t.get('partner_id'))}\n"
                    f"Estado: {_m2o(t.get('stage_id'))}\n"
                    f"Fecha: {_fmt_date(str(t.get('create_date',''))[:10])}\n"
                    f"Descripción: {t.get('description','N/A')}")
        except Exception as e:
            return f"Error: {str(e)}"

    @tool
    def actualizar_ticket(ticket_id: int, nota: str = "") -> str:
        """Agregar una nota interna a un ticket de soporte."""
        try:
            if nota:
                odoo.execute_kw("helpdesk.ticket", "message_post", [[ticket_id]],
                                {"body": nota, "message_type": "comment",
                                 "subtype_xmlid": "mail.mt_note"})
            return f"✅ Ticket {ticket_id} actualizado."
        except Exception as e:
            return f"Error: {str(e)}"

    @tool
    def verificar_garantia(partner_id: int, product_name: str = "") -> str:
        """Verificar si un cliente tiene tickets de garantía activos."""
        try:
            domain = [("partner_id","=",partner_id)]
            if product_name:
                domain.append(("name","ilike",product_name))
            tickets = odoo.search_read("helpdesk.ticket", domain, ["name","stage_id"], 5)
            if not tickets:
                return "No se encontraron garantías activas."
            return "\n".join(f"🎫 {t.get('name')} | {_m2o(t.get('stage_id'))}" for t in tickets)
        except Exception as e:
            return f"Helpdesk no disponible: {str(e)}"

    @tool
    def registrar_solucion(ticket_id: int, solucion: str) -> str:
        """Registrar la solución aplicada a un ticket de soporte."""
        try:
            odoo.execute_kw("helpdesk.ticket", "message_post", [[ticket_id]],
                            {"body": f"✅ Solución: {solucion}", "message_type": "comment",
                             "subtype_xmlid": "mail.mt_note"})
            return f"✅ Solución registrada en ticket {ticket_id}."
        except Exception as e:
            return f"Error: {str(e)}"

    # ── CARTERA ───────────────────────────────────────────────────────────

    @tool
    def registrar_pago(partner_id: int, amount: float, invoice_id: int = 0, nota: str = "") -> str:
        """Anotar un pago de cliente (requiere confirmación posterior en Odoo)."""
        msg = f"💰 Pago anotado: {_fmt_currency(amount)}"
        if nota: msg += f" — {nota}"
        if invoice_id: msg += f" (factura ID:{invoice_id})"
        try:
            odoo.execute_kw("res.partner", "message_post", [[partner_id]],
                            {"body": msg, "message_type": "comment"})
        except Exception:
            pass
        return f"✅ {msg}\n⚠️ Requiere confirmación en Odoo."

    @tool
    def listar_pagos(partner_id: int, limit: int = 5) -> str:
        """Listar los pagos recientes de un cliente."""
        pays = odoo.search_read(
            "account.payment",
            [("partner_id","=",partner_id),("payment_type","=","inbound")],
            ["name","date","amount","state"], limit)
        if not pays:
            return "No se encontraron pagos para este cliente."
        state_map = {"draft":"Borrador","posted":"Confirmado","cancel":"Cancelado"}
        return "\n".join(
            f"💰 {p.get('name')} | {_fmt_date(str(p.get('date','') or ''))} | "
            f"{_fmt_currency(p.get('amount',0))} | {state_map.get(p.get('state',''),p.get('state',''))}"
            for p in pays)

    @tool
    def enviar_estado_cuenta_whatsapp(partner_id: int) -> str:
        """Generar el texto del estado de cuenta para enviar por WhatsApp. Cada línea
        ya trae la marca "⚠️ VENCIDA" cuando corresponde — es un cálculo exacto por
        fecha, NO la recalcules ni la infieras tú (ej. asumiendo que solo la más
        antigua está vencida): repite tal cual el texto que devuelve esta tool."""
        from datetime import date as _date

        invs = odoo.search_read(
            "account.move",
            [("partner_id","child_of",_commercial_id(partner_id) or partner_id),("move_type","=","out_invoice"),
             ("payment_state","in",["not_paid","partial"])],
            ["name","invoice_date_due","amount_residual"], 10)
        if not invs:
            return "✅ El cliente no tiene saldo pendiente."

        invs.sort(key=lambda i: i.get("invoice_date_due") or "")
        hoy = _date.today()
        total = sum(i.get("amount_residual",0) for i in invs)
        total_vencido = 0.0
        recs = odoo.read("res.partner", [partner_id], ["name"])
        pname = recs[0]["name"] if recs else "Cliente"

        detalle = []
        for inv in invs:
            due_str = str(inv.get("invoice_date_due","") or "")
            vencida = False
            if due_str:
                try:
                    vencida = _date.fromisoformat(due_str[:10]) < hoy
                except ValueError:
                    pass
            if vencida:
                total_vencido += inv.get("amount_residual",0)
            marca = " ⚠️ VENCIDA" if vencida else ""
            detalle.append(f"  • {inv.get('name')} | Vence:{_fmt_date(due_str)} | "
                            f"{_fmt_currency(inv.get('amount_residual',0))}{marca}")

        lines = [f"📊 *Estado de cuenta — {pname}*",
                 f"Saldo total: *{_fmt_currency(total)}*"]
        if total_vencido:
            lines.append(f"⚠️ Saldo vencido: *{_fmt_currency(total_vencido)}*")
        lines.append("")
        return "\n".join(lines + detalle)

    @tool
    def crear_acuerdo_pago(partner_id: int, plan_descripcion: str) -> str:
        """Registrar un acuerdo de pago con un cliente."""
        msg = f"🤝 Acuerdo de pago: {plan_descripcion}"
        try:
            odoo.execute_kw("res.partner", "message_post", [[partner_id]],
                            {"body": msg, "message_type": "comment"})
        except Exception:
            pass
        return f"✅ {msg}"

    # ── WHATSAPP / ESCALACIÓN ─────────────────────────────────────────────

    @tool
    def escalar_a_asesor(motivo: str, resumen: str = "") -> str:
        """Transfiere la conversación a un asesor humano y detiene las respuestas del bot.

        USAR cuando: el cliente pide una persona real, expresa molestia o reclamo formal,
        pide descuento especial / negociación / crédito, o no puedes resolver con tus herramientas.

        Antes de llamar: envía UN mensaje cálido genérico ("Un asesor de nuestro equipo te
        escribirá pronto. 🙏") — sin nombre de asesor ni hora prometida. Después de llamar,
        no respondas nada más: el asesor toma el control.

        motivo: razón específica. resumen: contexto clave para el asesor (productos, cotización).
        """
        if ch_id:
            _esc_at = datetime.now(timezone.utc).isoformat()
            _merge_bot_context({"v3_escalate": True, "v3_escalate_motivo": motivo, "v3_escalate_at": _esc_at})
            try:
                partes = ["🤖 Bot escaló la conversación"]
                if motivo:
                    partes.append("Motivo: " + motivo)
                if resumen:
                    partes.append("Contexto: " + resumen)
                odoo.execute_kw(
                    "discuss.channel", "jwb_postear_nota_interna",
                    [[ch_id], " — ".join(partes)],
                )
            except Exception as _e:
                logger.warning("escalar_a_asesor: no pudo postear nota canal=%s: %s", ch_id, _e)
        return "ESCALADO_OK: asesor humano tomara el control. No respondas mas mensajes."


    @tool
    def limpiar_carrito() -> str:
        """Limpiar el carrito de estado (producto y cotización pendiente).
        Usar cuando el cliente empieza una consulta completamente nueva o cancela el pedido actual."""
        if ch_id:
            try:
                carriots = odoo.search_read(
                    "jpc.whatsapp.bot.carrito",
                    [["channel_id", "=", ch_id], ["state", "in", ["open", "quoted"]]],
                    ["id"], limit=5,
                )
                for c in carriots:
                    odoo.execute_kw(
                        "jpc.whatsapp.bot.carrito", "write",
                        [[c["id"]], {"state": "cancelled"}],
                    )
            except Exception as e:
                logger.warning("limpiar_carrito: %s", e)
        return "🗑️ Carrito limpiado. Puedes empezar una nueva consulta."

    # ── CARRITO ───────────────────────────────────────────────────────────

    @tool
    def ver_carrito() -> str:
        """Ver el estado actual del carrito de compras: productos buscados, precios mostrados y líneas cotizadas.
        Llama esta herramienta cuando necesites saber qué productos ya están en el carrito antes de actuar."""
        if not ch_id:
            return "No hay canal activo."
        try:
            carriots = odoo.search_read(
                "jpc.whatsapp.bot.carrito",
                [["channel_id", "=", ch_id], ["state", "in", ["open", "quoted"]]],
                ["id", "name", "sale_order_id", "line_ids"], limit=1,
            )
            if not carriots:
                return "No hay carrito activo. El cliente no ha buscado productos aún."
            c = carriots[0]
            if not c.get("line_ids"):
                return f"Carrito {c['name']} vacío."
            lines = odoo.read(
                "jpc.whatsapp.bot.carrito.linea", c["line_ids"],
                ["product_id", "product_name", "search_term", "qty_solicitada",
                 "tarjeta_mostrada", "precio_mostrado", "precio_unit", "precio_total",
                 "seleccionado_cotizar", "sale_order_line_id"],
            )
            sale_ref = c.get("sale_order_id")
            result = [f"🛒 Carrito: {c['name']}"]
            if sale_ref:
                oid = sale_ref[0] if isinstance(sale_ref, list) else sale_ref
                result.append(f"Cotización: order_id={oid}")
            result.append("")
            for line in lines:
                pid = line["product_id"][0] if isinstance(line["product_id"], list) else line.get("product_id")
                pname = line.get("product_name") or (
                    line["product_id"][1] if isinstance(line["product_id"], list) else "")
                qty = line.get("qty_solicitada", 0)
                tarjeta = "✅" if line.get("tarjeta_mostrada") else "⬜"
                precio_ok = line.get("precio_mostrado", False)
                total = line.get("precio_total", 0)
                precio_str = f"✅ ${total:,.0f}" if precio_ok else "⚠️PENDIENTE"
                cotizado = "✅" if line.get("sale_order_line_id") else "⬜"
                result.append(
                    f"  • ID:{pid} {pname} | qty={qty:.0f}"
                    f" | tarjeta:{tarjeta} | precio:{precio_str} | cotizado:{cotizado}"
                )
            needs = [l for l in lines if not l.get("precio_mostrado") and not l.get("sale_order_line_id")]
            if needs:
                result.append(f"\n⚠️ Pendiente mostrar precio: {', '.join(l.get('product_name','') for l in needs)}")
            return "\n".join(result)
        except Exception as e:
            return f"Error leyendo carrito: {str(e)[:200]}"

    @tool
    def seleccionar_linea_carrito(product_id: int) -> str:
        """Marcar UN producto del carrito para cotizar (deselecciona los demás).
        USAR cuando el cliente elige uno de varios mostrados. Para cotizar TODOS,
        usa directamente crear_cotizacion_desde_carrito sin llamar esta.
        """
        carrito_id = _get_carrito_id()
        if not carrito_id:
            return "No hay carrito activo."
        try:
            lines = odoo.search_read(
                "jpc.whatsapp.bot.carrito.linea",
                [["carrito_id", "=", carrito_id]],
                ["id", "product_id", "product_name"],
            )
            selected_name = None
            for line in lines:
                pid = line["product_id"][0] if isinstance(line["product_id"], list) else line.get("product_id")
                is_sel = (pid == product_id)
                odoo.execute_kw(
                    "jpc.whatsapp.bot.carrito.linea", "write",
                    [[line["id"]], {"seleccionado_cotizar": is_sel}],
                )
                if is_sel:
                    selected_name = line.get("product_name", f"ID:{product_id}")
            if selected_name:
                return f"✅ Seleccionado para cotizar: {selected_name}"
            return f"⚠️ Producto {product_id} no encontrado en el carrito."
        except Exception as e:
            logger.warning("seleccionar_linea_carrito product=%s: %s", product_id, e)
            return f"Error: {e}"

    @tool
    def crear_cotizacion_desde_carrito(partner_id: int) -> str:
        """Crear una cotización con TODOS los productos del carrito que tienen precio mostrado y están seleccionados.
        Usar cuando el cliente confirma que quiere cotizar todos los ítems del carrito.
        Más eficiente que llamar crear_cotizacion + agregar_linea_cotizacion línea por línea."""
        if not ch_id:
            return "No hay canal activo."
        try:
            carriots = odoo.search_read(
                "jpc.whatsapp.bot.carrito",
                [["channel_id", "=", ch_id], ["state", "in", ["open", "quoted"]]],
                ["id", "name", "sale_order_id", "line_ids"], limit=1,
            )
            if not carriots:
                return "No hay carrito activo. Busca los productos primero."
            c = carriots[0]
            if not c.get("line_ids"):
                return "El carrito está vacío."
            lines = odoo.read(
                "jpc.whatsapp.bot.carrito.linea", c["line_ids"],
                ["id", "product_id", "product_name", "qty_solicitada",
                 "precio_mostrado", "seleccionado_cotizar", "sale_order_line_id"],
            )
            to_quote = [
                l for l in lines
                if l.get("seleccionado_cotizar") and not l.get("sale_order_line_id")
            ]
            if not to_quote:
                return "Todos los productos ya están cotizados o ninguno está seleccionado."

            # Obtener o crear la cotización
            sale_ref = c.get("sale_order_id")
            order_id = (sale_ref[0] if isinstance(sale_ref, list) else sale_ref) if sale_ref else None
            if not order_id:
                _eff_partner = _commercial_id(partner_id) or partner_id
                order_id = odoo.create("sale.order", {
                    "partner_id": _eff_partner,
                    "state": "draft",
                    "payment_method_id": 215,
                    **({"pricelist_id": _bot_pricelist_id} if _bot_pricelist_id else {}),
                })
            orders = odoo.read("sale.order", [order_id], ["name", "amount_total"])
            order_name = orders[0]["name"] if orders else str(order_id)

            added = []
            agotados = []
            for line in to_quote:
                pid = line["product_id"][0] if isinstance(line["product_id"], list) else line["product_id"]
                pname = line.get("product_name", "")
                qty = line.get("qty_solicitada") or 1.0
                _stock_error = _verificar_stock_o_sugerir(pid, qty)
                if _stock_error:
                    agotados.append(_stock_error)
                    continue
                sol_id = odoo.create("sale.order.line", {
                    "order_id": order_id,
                    "product_id": pid,
                    "product_uom_qty": qty,
                })
                sol_data = odoo.read("sale.order.line", [sol_id], ["price_total"])
                price_total = sol_data[0]["price_total"] if sol_data else 0
                odoo.execute_kw(
                    "jpc.whatsapp.bot.carrito.linea", "write",
                    [[line["id"]], {"sale_order_line_id": sol_id, "qty_solicitada": qty}],
                )
                added.append(f"  • {pname} × {qty:.0f} = {_fmt_currency(price_total)}")

            if not added:
                return "\n\n".join(agotados) if agotados else "No se pudo agregar ningún producto."

            odoo.execute_kw(
                "jpc.whatsapp.bot.carrito", "write",
                [[c["id"]], {"sale_order_id": order_id, "state": "quoted"}],
            )
            order_upd = odoo.read("sale.order", [order_id], ["name", "amount_total"])
            total = order_upd[0]["amount_total"] if order_upd else 0
            result = [f"✅ {order_name} (ID:{order_id})"]
            result.extend(added)
            result.append(f"Total: {_fmt_currency(total)} (IVA incluido)")
            if agotados:
                result.append("")
                result.extend(agotados)
            return "\n".join(result)
        except Exception as e:
            logger.exception("crear_cotizacion_desde_carrito: error")
            return f"❌ Error: {str(e)[:200]}"

    # ── GENÉRICOS ─────────────────────────────────────────────────────────

    @tool
    def search_contacts(name: str = "", email: str = "", limit: int = 10) -> str:
        """Buscar contactos en Odoo por nombre o email."""
        domain = []
        if name: domain.append(("name","ilike",name))
        if email: domain.append(("email","ilike",email))
        partners = odoo.search_read("res.partner", domain,
                                    ["name","email","phone","id"], limit)
        if not partners:
            return "No se encontraron contactos."
        return "\n".join(
            f"ID:{p['id']} | {p.get('name')} | {p.get('email')} | {p.get('phone')}"
            for p in partners)

    @tool
    def search_odoo_model(model: str, domain_json: str = "[]",
                          fields_json: str = "[]", limit: int = 20) -> str:
        """Buscar registros en cualquier modelo de Odoo (herramienta genérica).
        domain_json: '[["field","op","val"]]'. fields_json: '["field1","field2"]'."""
        domain = json.loads(domain_json)
        fields = json.loads(fields_json)
        records = odoo.search_read(model, domain, fields or None, limit)
        if not records:
            return "No se encontraron registros."
        return json.dumps(records, ensure_ascii=False, default=str)


    @tool
    def buscar_producto_ficha(query: str) -> str:
        """Busca un producto sin enviar tarjeta. Usa cuando el cliente ya dio la cantidad.
        Reutiliza _buscar_producto_core para tokenizar igual que buscar_producto_cotizacion."""
        if not query.strip():
            return "Por favor proporciona un término de búsqueda."

        # Reutilizar la misma lógica de búsqueda tokenizada
        prods, ids, ref, busqueda_tipo = _buscar_producto_core(query)
        if not prods and busqueda_tipo.startswith('repuesto_no_encontrado'):
            return _msg_repuesto_no_encontrado(busqueda_tipo, query)

        template_ids = []
        if prods:
            tmpl_ids_set = set()
            for p in prods:
                tmpl_raw = p.get("product_tmpl_id")
                tid = tmpl_raw[0] if isinstance(tmpl_raw, (list, tuple)) else tmpl_raw
                if tid:
                    tmpl_ids_set.add(int(tid))
            template_ids = list(tmpl_ids_set)

        if not template_ids:
            return f"No se encontró el producto '{query}' en nuestra base de datos."

        # 3. Obtener datos de los productos encontrados
        try:
            templates = odoo.search_read(
                'product.template',
                [['id', 'in', template_ids]],
                ['id', 'name', 'default_code'],
                limit=3
            )
        except Exception as e:
            return f"Error consultando productos: {e}"

        result_parts = []
        for tmpl in templates:
            tmpl_id = tmpl['id']
            lines = []
            lines.append(f"Producto: {tmpl['name']}")
            # Obtener product.product ID (necesario para agregar_linea_cotizacion)
            try:
                prods_pp = odoo.search_read(
                    'product.product',
                    [['product_tmpl_id', '=', tmpl_id], ['active', '=', True]],
                    ['id'], limit=1,
                )
                if prods_pp:
                    lines.append(f"PRODUCT_ID:{prods_pp[0]['id']}")
            except Exception as _e:
                logger.warning("buscar_producto_ficha product.product: %s", _e)
            if tmpl.get('default_code'):
                lines.append(f"Referencia: {tmpl['default_code']}")
            # No exponer precio base — el precio real viene de obtener_precio(product_id, partner_id)

            # Obtener atributos relevantes
            try:
                attr_lines = odoo.search_read(
                    'product.template.attribute.line',
                    [['product_tmpl_id', '=', tmpl_id]],
                    ['attribute_id', 'product_template_value_ids'],
                    limit=30
                )
                attrs = {}
                for al in attr_lines:
                    attr_name = al['attribute_id'][1] if isinstance(al['attribute_id'], list) else str(al['attribute_id'])
                    val_ids = al.get('product_template_value_ids') or []
                    if val_ids:
                        vals = odoo.search_read(
                            'product.template.attribute.value',
                            [['id', 'in', val_ids]],
                            ['name'], limit=20
                        )
                        attrs[attr_name] = ', '.join(v['name'] for v in vals)

                for key in ['Modelo', 'Rendimiento', 'Color', 'Chip', 'Impresoras Compatibles',
                            'Tipo Impresión', 'Fabricante', 'Marca Compatible']:
                    if key in attrs:
                        lines.append(f"{key}: {attrs[key]}")
            except Exception as e:
                logger.warning("buscar_producto_ficha attrs: %s", e)

            result_parts.append("\n".join(lines))

        return "\n\n---\n\n".join(result_parts)

    @tool
    def web_search(query: str, max_results: int = 5) -> str:
        """Busca información técnica en internet. Retorna datos CON advertencia de verificación y URL fuente."""
        import requests as _req
        import re
        WARN = 'ADVERTENCIA: datos de internet no verificados. Confirmar con fuente oficial antes de informar al cliente.\n'
        try:
            resp = _req.get(
                'https://api.duckduckgo.com/',
                params={'q': query, 'format': 'json', 'no_redirect': '1', 'no_html': '1', 'skip_disambig': '1'},
                timeout=10, headers={'User-Agent': 'Mozilla/5.0'}
            )
            data = resp.json()
            results = []
            abstract_url = data.get('AbstractURL', '')
            if data.get('AbstractText'):
                src = f' (fuente: {abstract_url})' if abstract_url else ''
                results.append(f'Resumen: {data["AbstractText"]}{src}')
            for topic in (data.get('RelatedTopics') or [])[:max_results]:
                if isinstance(topic, dict) and topic.get('Text'):
                    url = topic.get('FirstURL', '')
                    results.append(f'- {topic["Text"]}' + (f' [{url}]' if url else ''))
            if results:
                return WARN + '\n'.join(results)
            html_resp = _req.get(
                'https://html.duckduckgo.com/html/', params={'q': query},
                headers={'User-Agent': 'Mozilla/5.0 (compatible; JPC-Agent/1.0)'}, timeout=15
            )
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html_resp.text, re.DOTALL)
            clean = [re.sub(r'<[^>]+>', '', s).strip() for s in snippets[:max_results] if s.strip()]
            if clean:
                return WARN + '\n'.join(f'- {t}' for t in clean if t)
            return 'No se encontraron resultados en internet.'
        except Exception as e:
            return f'Error en búsqueda web: {e}'


    @tool
    def consultar_estado_pedido_cliente(partner_id: int) -> str:
        """Consulta el estado del ultimo pedido activo del cliente usando el campo de etiqueta de entrega.
        Informa si esta en preparacion, en ruta con hora estimada, entregado o fallido.
        Usar cuando el cliente pregunte por su pedido, domicilio, entrega o envio."""
        try:
            from datetime import datetime, timezone, timedelta
            orders = odoo.search_read(
                "sale.order",
                [("partner_id", "=", partner_id), ("state", "in", ["sale", "done"])],
                ["name", "state", "delivery_status", "jpc_entrega_label", "date_order"],
                limit=1,
                order="date_order desc",
            )
            if not orders:
                return "No encontre pedidos activos para este cliente."

            order = orders[0]
            label = (order.get("jpc_entrega_label") or "").strip()
            name = order["name"]
            date_str = str(order.get("date_order") or "")
            age_days = 999
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str.replace(" ", "T"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age_days = (datetime.now(timezone.utc) - dt).days
                except Exception:
                    pass

            if not label:
                return (
                    f"Su pedido *{name}* esta en preparacion y aun no ha sido despachado. "
                    "En cuanto salga a domicilio te avisamos. Puedes comunicarte con tu asesor si necesitas urgencia."
                )

            label_lower = label.lower()
            hora = label.replace("(", "").replace(")", "").strip()
            # Extraer solo la hora del formato "Entrega (12:00)"
            import re as _re2
            m = _re2.search(r"\((\d{1,2}:\d{2})\)", label)
            hora_str = m.group(1) if m else hora

            if label_lower.startswith("entregada"):
                if age_days > 5:
                    return (
                        f"No tienes pedidos recientes activos. "
                        f"El ultimo pedido ({name}) fue entregado hace {age_days} dias."
                    )
                return f"Checkmark *Su pedido {name} fue entregado* a las {hora_str}. Esperamos que todo este perfecto! Si tienes algun inconveniente cuename."

            if label_lower.startswith("fallida"):
                return (
                    f"El domiciliario intento entregar tu pedido *{name}* a las {hora_str} "
                    "pero no fue posible la entrega. Tu asesor te contactara para reprogramar. "
                    "Si necesitas urgencia puedes comunicarte directamente."
                )

            if label_lower.startswith("entrega"):
                return (
                    f"Su pedido *{name}* esta *en ruta* y llegara aproximadamente a las *{hora_str}*. "
                    "Asegurate de tener alguien disponible para recibirlo."
                )

            return f"Estado de tu pedido *{name}*: {label}"

        except Exception as e:
            logger.error("consultar_estado_pedido_cliente: %s", e)
            return "No pude consultar el estado del pedido en este momento. Comunicate con tu asesor."


    @tool
    def calcular_envio_orden(order_id: int) -> str:
        """Calcular el costo de envio para una orden SIN aplicarlo todavia.
        Usar para mostrar al cliente el precio del envio antes de que confirme.
        Si el cliente confirma el envio, despues llama agregar_envio_orden(order_id).
        Si el cliente prefiere recoger en tienda, NO llames agregar_envio_orden."""
        try:
            orders = odoo.read("sale.order", [order_id], ["name", "state", "carrier_id"])
            if not orders:
                return f"Orden {order_id} no encontrada."
            order = orders[0]
            # Ignorar carrier por defecto (Recoleccion en tienda id=2) asignado automaticamente por Odoo
            # Solo saltar si hay un carrier de ENVIO real ya confirmado (no pickup)
            existing_carrier = order.get("carrier_id")
            existing_carrier_id = existing_carrier[0] if isinstance(existing_carrier, list) else existing_carrier
            if existing_carrier_id and existing_carrier_id not in (1, 2):  # 1=Envio estandar, 2=Recoleccion tienda
                carrier_name = existing_carrier[1] if isinstance(existing_carrier, list) else str(existing_carrier)
                return f"La orden {order['name']} ya tiene envio asignado: *{carrier_name}*. No se necesita calcular de nuevo."

            wizard_id = odoo.execute_kw(
                "choose.delivery.carrier", "create",
                [{"order_id": order_id}],
            )
            if not wizard_id:
                return "No pude calcular el envio. Consulta con un asesor."

            wizard_data = odoo.read(
                "choose.delivery.carrier",
                [wizard_id],
                ["carrier_id", "delivery_price"],
            )
            if not wizard_data:
                return "No pude leer el costo de envio."

            w = wizard_data[0]
            carrier_name = w["carrier_id"][1] if isinstance(w.get("carrier_id"), list) else "Envio"
            price = float(w.get("delivery_price") or 0)

            try:
                odoo.execute_kw("choose.delivery.carrier", "unlink", [[wizard_id]])
            except Exception:
                pass

            logger.info("calcular_envio_orden: order_id=%s carrier=%s price=%.0f", order_id, carrier_name, price)
            return f"El envio con *{carrier_name}* tiene un costo de *{_fmt_currency(price)}*."

        except Exception as e:
            logger.error("calcular_envio_orden: order_id=%s error=%s", order_id, e)
            return f"No pude calcular el envio: {str(e)[:100]}. Consulta con un asesor."

    @tool
    def agregar_envio_orden(order_id: int) -> str:
        """Agrega el método de envío a la orden (Odoo elige el carrier según la dirección
        y calcula el costo por peso). Llamar antes de enviar la cotización si el cliente
        pidió domicilio; NO llamar si el envío ya está (verificar con obtener_cotizacion).
        """
        order_id, _rec = _resolver_order_id_cliente(order_id, estados=("draft", "sent"))
        if not order_id:
            return (
                "❌ Cotización no encontrada o no pertenece al cliente actual — "                "NO se agregó envío. Verifica el ORDER_ID (usa el numérico de "                "crear_cotizacion/agregar_linea_cotizacion, no los dígitos del nombre)."
            )
        try:
            # Verificar que la orden existe y no tiene envio ya agregado
            orders = odoo.read("sale.order", [order_id], ["name", "state", "carrier_id", "amount_total"])
            if not orders:
                return f"Orden {order_id} no encontrada."
            order = orders[0]
            if order.get("carrier_id"):
                carrier_name = order["carrier_id"][1] if isinstance(order["carrier_id"], list) else str(order["carrier_id"])
                # Si ya tiene envio real (no recogida en tienda), no modificar
                is_pickup = any(kw in carrier_name.lower() for kw in ['pick up', 'pickup', 'recog', 'store', 'tienda'])
                if not is_pickup:
                    return f"La orden {order['name']} ya tiene envio asignado: {carrier_name}. No se modifico."
                # Es Pick up in store: eliminar linea y reemplazar con envio a domicilio
                delivery_lines = odoo.search_read(
                    "sale.order.line",
                    [["order_id", "=", order_id], ["is_delivery", "=", True]],
                    ["id"]
                )
                if delivery_lines:
                    odoo.execute_kw("sale.order.line", "unlink", [[l["id"] for l in delivery_lines]])
                odoo.execute_kw("sale.order", "write", [[order_id], {"carrier_id": False}])

            # Crear wizard de envio - Odoo calcula carriers disponibles y precio segun peso/destino
            wizard_id = odoo.execute_kw(
                "choose.delivery.carrier", "create",
                [{"order_id": order_id}],
            )
            if not wizard_id:
                return "No se pudo inicializar el wizard de envio."

            # Leer carrier seleccionado por defecto y precio calculado
            wizard_data = odoo.read(
                "choose.delivery.carrier",
                [wizard_id],
                ["carrier_id", "delivery_price"],
            )
            if not wizard_data:
                return "No se pudo leer el wizard de envio."

            w = wizard_data[0]
            carrier_name = w["carrier_id"][1] if isinstance(w.get("carrier_id"), list) else "Envio"
            price = float(w.get("delivery_price") or 0)

            # Confirmar: agrega linea de envio a la orden
            odoo.execute_kw(
                "choose.delivery.carrier", "button_confirm",
                [[wizard_id]],
            )

            logger.info("agregar_envio_orden: order_id=%s carrier=%s price=%.0f", order_id, carrier_name, price)
            return f"Envio agregado a la orden: *{carrier_name}* — ${price:,.0f} COP"

        except Exception as e:
            logger.error("agregar_envio_orden: order_id=%s error=%s", order_id, e)
            return f"No pude agregar el envio automaticamente: {str(e)}. Solicita a tu asesor que lo gestione."


    @tool
    def verificar_historial_envio(partner_id: int) -> str:
        """Verifica el método de entrega preferido del cliente (carrier configurado en el
        partner, o inferido de su historial de pedidos). USAR antes de crear cotización
        cuando confirma compra; si retorna carrier, usa agregar_envio_orden.
        """
        if not partner_id:
            return "No hay cliente activo para revisar."
        try:
            # Prioridad: metodo de entrega configurado en el partner
            partner_data = odoo.read(
                "res.partner", [partner_id],
                ["name", "property_delivery_carrier_id"],
            )
            if partner_data:
                carrier = partner_data[0].get("property_delivery_carrier_id")
                if carrier:
                    carrier_name = carrier[1] if isinstance(carrier, list) else str(carrier)
                    msg_carrier = (
                        "El cliente tiene metodo de entrega configurado: *" + carrier_name + "*.\n"
                        "Accion: despues de crear la cotizacion, llama agregar_envio_orden(order_id) automaticamente.\n"
                        "NO preguntes al cliente -- ya esta configurado en su cuenta."
                    )
                    return msg_carrier
        except Exception as e:
            logger.warning("verificar_historial_envio: error leyendo carrier: %s", e)

        # Fallback: revisar historial de pedidos si no tiene carrier configurado
        if not partner_id:
            return "No hay cliente activo para revisar historial."
        try:
            orders = odoo.search_read(
                "sale.order",
                [["partner_id", "child_of", _commercial_id(partner_id) or partner_id], ["state", "in", ["sale", "done"]]],
                ["id", "name", "date_order", "amount_total"],
                limit=10,
            )
            # Ordenar por fecha descendente y tomar los 3 más recientes
            orders = sorted(orders, key=lambda o: str(o.get("date_order") or ""), reverse=True)[:3]
            if not orders:
                return (
                    "El cliente no tiene pedidos anteriores confirmados.\n"
                    "Recomendacion: pregunta si desea envio a domicilio o recoge en tienda."
                )

            pedidos_con_envio = []
            pedidos_sin_envio = []

            for order in orders:
                delivery_lines = odoo.search_read(
                    "sale.order.line",
                    [
                        ["order_id", "=", order["id"]],
                        ["product_id.categ_id.name", "=", "Deliveries"],
                    ],
                    ["product_id", "price_total"],
                    limit=3,
                )
                fecha = str(order.get("date_order", ""))[:10]
                total = order.get("amount_total", 0)
                nombre = order.get("name", str(order["id"]))
                if delivery_lines:
                    envio_total = sum(float(l.get("price_total") or 0) for l in delivery_lines)
                    tipo_envio = delivery_lines[0].get("product_id")
                    tipo_str = tipo_envio[1] if isinstance(tipo_envio, list) else "Envio"
                    pedidos_con_envio.append(f"  - {nombre} ({fecha}) | Pedido: {_fmt_currency(total)} | Envio: {_fmt_currency(envio_total)} ({tipo_str})")
                else:
                    pedidos_sin_envio.append(f"  - {nombre} ({fecha}) | Pedido: {_fmt_currency(total)} | Sin envio")

            total_revisados = len(orders)
            tiene_historial_envio = len(pedidos_con_envio) > 0
            mayoria_con_envio = len(pedidos_con_envio) >= len(pedidos_sin_envio)

            lines = [f"Historial de envio ({total_revisados} pedido(s) revisado(s)):"]
            if pedidos_con_envio:
                lines.append("Con envio:")
                lines.extend(pedidos_con_envio)
            if pedidos_sin_envio:
                lines.append("Sin envio:")
                lines.extend(pedidos_sin_envio)

            if tiene_historial_envio and mayoria_con_envio:
                lines.append(
                    "\nRecomendacion: el cliente normalmente recibe con envio. "
                    "Pregunta: *\u00bfDesea que incluyamos el servicio de envio a domicilio?* "
                    "Si responde Si, agregar linea de envio antes de confirmar."
                )
            elif tiene_historial_envio:
                lines.append(
                    "\nRecomendacion: el cliente ha tenido envio en algunos pedidos. "
                    "Pregunta: *\u00bfDesea envio a domicilio o recoge en tienda?*"
                )
            else:
                lines.append(
                    "\nRecomendacion: el cliente normalmente recoge en tienda. "
                    "Pregunta: *\u00bfDesea recibir a domicilio o recoge en tienda?*"
                )

            return "\n".join(lines)
        except Exception as e:
            logger.exception("verificar_historial_envio: error")
            return f"No se pudo revisar el historial de envio: {str(e)[:100]}"


    @tool
    def buscar_referencias_mensaje(mensaje: str) -> str:
        """Busca referencias de productos mencionadas en el mensaje del cliente.

        Herramienta PRINCIPAL para INTENCIÓN DE COMPRA ("necesito", "cotízame", "quiero",
        "factúrame"): extrae todas las referencias y cantidades, maneja la degradación
        exacto → similar → dígitos, y registra lo encontrado en el carrito.
        Para consulta de precio/información usa buscar_producto (envía tarjetas).

        Retorna PRODUCT_ID, nombre, cantidad y nivel de confianza por cada referencia.
        """
        import re as _re

        if not mensaje or not mensaje.strip():
            return "Indica la referencia del producto que necesitas (ej: 85A, 664, CE285A)."

        # Limpiar modificadores genericos del mensaje
        # NOTA: 'tambor' y 'drum' NO van aqui \u2014 son tipos de repuesto, no ruido.
        _MOD = _re.compile(
            r'\b(toner|t[o\u00f3]ner|cartucho|tinta|ink|cartridge|'
            r'necesito|quiero|cotizame|facturame|pide|dame|busco|'
            r'sin|con|chip|negro|color|original|compatible|nuevo|nueva|'
            r'para|de|del|la|el|los|las|y|o|a|en|un|una|por|favor)\b',
            _re.IGNORECASE,
        )
        msg_clean = _MOD.sub(' ', mensaje)
        msg_clean = _re.sub(r' {2,}', ' ', msg_clean).strip()

        # Token = referencia si mezcla letra+digito (cualquier cantidad de
        # digitos: nunca se confunde con cantidad porque QTY_PAT exige digitos
        # puros sin letra pegada) o si es puro digito con 3+.
        # Ejemplo: 85A, 664, CE285A, 255X, 1020nw, U4, M6  ->  referencias
        # Ejemplo: 2, 4, 10  ->  cantidades (1-2 digitos solos, sin letra)
        REF_PAT = _re.compile(
            r'\b('
            r'[A-Za-z]{1,6}\d{1,}[A-Za-z0-9]{0,8}'
            r'|\d{1,}[A-Za-z][A-Za-z0-9]{0,8}'
            r'|\d{3,}'
            r')\b'
        )
        QTY_PAT = _re.compile(r'(?<![A-Za-z0-9])(\d{1,2})(?![A-Za-z0-9])')

        # Referencia "corta" (ej. U4, M6, con <2 digitos): solo se busca en
        # terminos EXACTOS (=ilike sobre el string completo, sin riesgo de
        # falso positivo). Nunca cae a "similar" (ilike parcial), donde 1
        # solo digito genera demasiados falsos positivos.
        def _ref_es_corta(r):
            return len(_re.findall(r'\d', r)) < 2

        ref_matches = list(REF_PAT.finditer(msg_clean))
        if not ref_matches:
            return (
                "No detecte referencias de productos (minimo 3 digitos). "
                "Indica la referencia exacta (ej: 85A, 664, CE285A)."
            )

        # Asociar cantidad a cada referencia: buscar qty entre esta ref y la siguiente.
        # Ademas clasificar cada referencia como repuesto o consumible segun el
        # texto previo en el MENSAJE ORIGINAL (alli sobreviven 'toner', 'cilindro'...).
        # El tipo de repuesto es pegajoso ("almohadilla L3210 y L220" → ambas)
        # hasta que aparezca una palabra de consumible (toner/cartucho/tinta).
        pairs = []
        msg_low = mensaje.lower()
        search_pos = 0
        prev_end_orig = 0
        sticky_tipo = None
        for i, m in enumerate(ref_matches):
            ref = m.group(1).upper()
            seg_end = ref_matches[i + 1].start() if i + 1 < len(ref_matches) else len(msg_clean)
            segment = msg_clean[m.end():seg_end]
            qty_m = QTY_PAT.search(segment)
            qty = int(qty_m.group(1)) if qty_m else 1
            pos = msg_low.find(ref.lower(), search_pos)
            if pos >= 0:
                antes = mensaje[prev_end_orig:pos]
                search_pos = pos + len(ref)
                prev_end_orig = pos + len(ref)
            else:
                antes = ''
            tipo_seg = _detectar_repuesto(antes)
            if tipo_seg:
                sticky_tipo = tipo_seg
            elif _CARTUCHO_RE.search(antes):
                sticky_tipo = None
            pairs.append((ref, qty, sticky_tipo))

        # Puntaje de ventas globales para ordenar cuando hay multiples resultados
        def _get_scores(product_ids):
            if not product_ids:
                return {}
            try:
                lines = odoo.search_read(
                    "sale.order.line",
                    [["product_id", "in", product_ids]],
                    ["product_id"], limit=2000,
                )
                scores = {}
                for ln in lines:
                    pid = ln["product_id"][0] if isinstance(ln["product_id"], list) else ln["product_id"]
                    scores[pid] = scores.get(pid, 0) + 1
                return scores
            except Exception:
                return {}

        results = []
        not_found_refs = []
        repuestos_nf = []  # [(ref, busqueda_tipo empaquetado)] repuestos sin match

        for ref, qty, tipo_rep in pairs:
            confidence = None
            tmpl_ids = []

            # Fase 0: la referencia es de un REPUESTO — nunca resolver como cartucho
            if tipo_rep:
                tmpl_ids, rep_parciales = _buscar_repuesto_directo(tipo_rep, ref)
                if tmpl_ids:
                    confidence = "exacta"
                else:
                    bt = 'repuesto_no_encontrado:%s' % tipo_rep
                    if rep_parciales:
                        bt += ':' + ' | '.join(rep_parciales)
                    repuestos_nf.append((ref, bt))
                    continue

            # Fase 1: exacto en jpc_search_exact_ids
            if not tmpl_ids:
                term_recs = odoo.search_read(
                    "jpc.product.search.term",
                    [("name", "=ilike", ref)],
                    ["id"], limit=10,
                )
                if term_recs:
                    term_ids = [t["id"] for t in term_recs]
                    tmpl_recs = odoo.search_read(
                        "product.template",
                        [("jpc_search_exact_ids", "in", term_ids), ("active", "=", True)],
                        ["id"], limit=5,
                    )
                    tmpl_ids = [r["id"] for r in tmpl_recs]
                    if tmpl_ids:
                        confidence = "exacta"

            # Fase 2: similar en jpc_search_similar_ids (nunca para refs cortas)
            if not tmpl_ids and not _ref_es_corta(ref):
                if ch_id and _bot_id:
                    try:
                        odoo.execute_kw(
                            "jpc.whatsapp.bot.config", "enviar_aviso_busqueda_similar",
                            [[_bot_id], ch_id, ref],
                        )
                    except Exception as _e:
                        logger.warning("buscar_referencias_mensaje aviso: %s", _e)

                sim_recs = odoo.search_read(
                    "product.template",
                    [("jpc_search_similar_ids.name", "ilike", ref), ("active", "=", True)],
                    ["id"], limit=5,
                )
                tmpl_ids = [r["id"] for r in sim_recs]
                if tmpl_ids:
                    confidence = "similar"

            # Fase 3: solo digitos
            if not tmpl_ids:
                digits = _re.sub(r"[^0-9]", "", ref)
                if len(digits) >= 3:
                    # Buscar con limite de numero completo (boundary)
                    boundary = _re.compile(r"(?<!\d)" + _re.escape(digits) + r"(?!\d)", _re.IGNORECASE)
                    term_recs3 = odoo.search_read(
                        "jpc.product.search.term",
                        [("name", "ilike", digits), ("search_type", "=", "similar")],
                        ["id", "name"], limit=100,
                    )
                    matching_ids = [r["id"] for r in term_recs3 if boundary.search(r["name"])]
                    if matching_ids:
                        tmpl_recs3 = odoo.search_read(
                            "product.template",
                            [("jpc_search_similar_ids", "in", matching_ids), ("active", "=", True)],
                            ["id"], limit=5,
                        )
                        tmpl_ids = [r["id"] for r in tmpl_recs3]
                        if tmpl_ids:
                            confidence = "numero"

            # No encontrado
            if not tmpl_ids:
                not_found_refs.append(ref)
                continue

            # Resolver product.product desde templates
            prods = odoo.search_read(
                "product.product",
                [("product_tmpl_id", "in", tmpl_ids), ("active", "=", True)],
                ["id", "default_code", "qty_available", "product_tmpl_id"],
                limit=10,
                context=_wh_ctx if _warehouse_id else None,
            )
            if _warehouse_id:
                prods = [p for p in prods if (p.get("qty_available") or 0) > 0]

            if not prods:
                not_found_refs.append(ref)
                continue

            # Ordenar por ventas globales
            prod_ids = [p["id"] for p in prods]
            scores = _get_scores(prod_ids)
            prods.sort(key=lambda p: scores.get(p["id"], 0), reverse=True)
            best = prods[0]

            # Nombre del template
            tmpl_raw = best.get("product_tmpl_id")
            tmpl_id = tmpl_raw[0] if isinstance(tmpl_raw, list) else tmpl_raw
            tmpls = odoo.search_read(
                "product.template", [("id", "=", tmpl_id)],
                ["id", "jpc_pos_name", "name"], limit=1,
            )
            display_name = (
                (tmpls[0].get("jpc_pos_name") or tmpls[0].get("name"))
                if tmpls else best.get("default_code", str(best["id"]))
            )

            # Registrar en carrito
            if ch_id:
                _upsert_carrito_linea(
                    product_id=best["id"],
                    product_name=display_name,
                    search_term=ref,
                    tarjeta_mostrada=False,
                    precio_mostrado=False,
                )

            # Crear sugerencia de aprendizaje si no fue exacto
            if confidence in ("similar", "numero") and ch_id:
                try:
                    odoo.execute_kw(
                        "jpc.search.term.suggestion", "registrar_sugerencia",
                        [[]], {
                            "term": ref,
                            "product_id": best["id"],
                            "channel_id": ch_id,
                            "confidence": confidence,
                        },
                    )
                except Exception as _e:
                    logger.warning("buscar_referencias_mensaje sugerencia: %s", _e)

            results.append({
                "ref": ref,
                "qty": qty,
                "product_id": best["id"],
                "product_name": display_name,
                "confidence": confidence,
                "qty_available": best.get("qty_available", 0),
            })

        # Notificar al asesor los no encontrados (incluye repuestos sin match)
        _notif_refs = not_found_refs + [
            "%s (%s)" % (r, _NOMBRES_REPUESTO.get(bt.split(':')[1] if ':' in bt else 'repuesto', 'repuesto'))
            for r, bt in repuestos_nf
        ]
        if _notif_refs and ch_id and _bot_id:
            try:
                odoo.execute_kw(
                    "jpc.whatsapp.bot.config", "notificar_producto_no_encontrado",
                    [[_bot_id], ch_id, _notif_refs],
                )
            except Exception as _e:
                logger.warning("buscar_referencias_mensaje not_found: %s", _e)

        # Deduplicar: si 49A y 53A resuelven al mismo product_id, conservar solo el primero
        seen_pids = set()
        deduped = []
        for r in results:
            if r["product_id"] not in seen_pids:
                seen_pids.add(r["product_id"])
                deduped.append(r)
        results = deduped

        # Construir respuesta
        lines = []
        conf_icon = {"exacta": "✅", "similar": "⚠️ similar", "numero": "⚠️ aprox."}
        for r in results:
            icon = conf_icon.get(r["confidence"], "")
            stock = f"Stock:{r['qty_available']:.0f}" if r.get("qty_available") is not None else ""
            lines.append(
                f"PRODUCT_ID:{r['product_id']} | {r['product_name']} "
                f"| Qty:{r['qty']} | {icon} | {stock}"
            )
        for ref in not_found_refs:
            lines.append(f"[PRODUCTO_NO_ENCONTRADO: {ref}]")
        for ref, bt in repuestos_nf:
            lines.append(_msg_repuesto_no_encontrado(bt, ref))

        if not lines:
            return "No se encontraron productos para las referencias indicadas."

        return "\n".join(lines)

    return [
        obtener_cliente_whatsapp, buscar_cliente, registrar_cliente, obtener_perfil_cliente,
        consultar_datos_facturacion, completar_datos_facturacion,
        obtener_precio, buscar_producto, buscar_producto_cotizacion,
        crear_cotizacion, agregar_linea_cotizacion, obtener_cotizacion, registrar_espera_respuesta,
        confirmar_orden, enviar_cotizacion_whatsapp,
        listar_facturas, obtener_factura, enviar_factura_whatsapp, enviar_factura_pdf_whatsapp, estado_de_cuenta,
        generar_link_cotizacion, generar_link_pago, enviar_link_pago_whatsapp, confirmar_cotizacion_y_link_pago,
        listar_pedidos_cliente, estado_entrega, consultar_pedidos_cliente,
        listar_tickets_cliente, crear_ticket_garantia, obtener_ticket, actualizar_ticket,
        verificar_garantia, registrar_solucion,
        registrar_pago, listar_pagos, enviar_estado_cuenta_whatsapp, crear_acuerdo_pago,
        escalar_a_asesor, limpiar_carrito, ver_carrito,
        seleccionar_linea_carrito, crear_cotizacion_desde_carrito,
        search_contacts, search_odoo_model, buscar_producto_ficha, web_search,
        consultar_faq,
        consultar_estado_pedido_cliente,
        calcular_envio_orden,
        agregar_envio_orden,
        verificar_historial_envio,
        buscar_referencias_mensaje,
    ]


def get_odoo_tools():
    return create_odoo_tools()
