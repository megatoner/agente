"""
executor.py — Loop de agente usando SDK nativa en lugar de LangChain messages.

Ventajas vs. versión anterior:
- Preserva reasoning_content (modelos thinking: kimi-k2, claude extended thinking)
- Funciona con cualquier proveedor OpenAI-compatible
- Control total sobre el formato de mensajes en multi-turn
"""
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from app.agents.providers import get_llm
from app.odoo.tools import create_odoo_tools
from app.memory.store import store_memory, get_session_memories
from sqlalchemy.orm import Session
from typing import Optional, Dict, Any, List
import logging
import re

logger = logging.getLogger(__name__)

# ── Guard anti-alucinación de precios ────────────────────────────────────────
# Si la respuesta final contiene un precio pero el agente NO llamó ninguna
# herramienta en el turno, el precio viene de su memoria de la sesión (puede
# estar inventado o desactualizado) — se fuerza un reintento con re-búsqueda.
_PRECIO_RE = re.compile(r'\$\s?\d{1,3}(?:[.,]\d{3})+')

_PRICE_RETRY_MSG = (
    "[SISTEMA] Tu respuesta incluye un precio pero NO llamaste ninguna herramienta "
    "en este turno. Los precios recordados de turnos anteriores pueden estar errados "
    "o desactualizados. Llama buscar_producto u obtener_precio AHORA y responde "
    "únicamente con los precios que retorne la herramienta."
)

DEFAULT_SYSTEM_PROMPT = """Eres un agente experto en Odoo ERP. Tu trabajo es ayudar al usuario a gestionar el sistema Odoo usando las herramientas disponibles.

REGLAS:
1. Siempre verifica si un contacto existe antes de crear uno nuevo.
2. Usa search_contacts para buscar por nombre o email.
3. Cuando crees una oportunidad, intenta vincularla a un contacto existente si es posible.
4. Fechas deben estar en formato YYYY-MM-DD.
5. Si no sabes el ID de algo, búscalo primero.
6. Responde en español de forma clara y concisa.
7. Después de ejecutar una acción, confirma el resultado al usuario.
"""

# Modelos que solo aceptan temperature=1
_TEMP_ONE_ONLY = ("kimi-k2",)


def _model_name(llm_model: str) -> str:
    """Extrae solo el nombre del modelo de 'provider/model'."""
    return llm_model.split("/", 1)[-1] if "/" in llm_model else llm_model


def _get_temperature(llm_model: str, config_temp: float) -> Optional[float]:
    """Retorna la temperatura adecuada para el modelo."""
    name = _model_name(llm_model).lower()
    if any(name.startswith(p) for p in _TEMP_ONE_ONLY):
        return 1
    return config_temp


def _extract_text(content) -> str:
    """Extrae texto de content str o lista de bloques."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
            if not isinstance(b, dict) or b.get("type") == "text"
        )
    return str(content) if content else ""


def _msg_to_dict(msg) -> dict:
    """Convierte un mensaje OpenAI a dict preservando todos los campos."""
    if hasattr(msg, "model_dump"):
        return {k: v for k, v in msg.model_dump().items() if v is not None}
    if hasattr(msg, "to_dict"):
        return {k: v for k, v in msg.to_dict().items() if v is not None}
    return dict(msg)


def _get_openai_tools(tools: list) -> list:
    """Obtiene schemas de tools en formato OpenAI desde tools LangChain."""
    result = []
    for tool in tools:
        try:
            if hasattr(tool, "as_openai_tool"):
                result.append(tool.as_openai_tool())
            else:
                from langchain_core.utils.function_calling import convert_to_openai_tool
                result.append(convert_to_openai_tool(tool))
        except Exception as e:
            logger.warning("_get_openai_tools: no se pudo convertir %s: %s", tool.name, e)
    return result



def _get_anthropic_tools(tools: list) -> list:
    """Convierte tools LangChain a formato Anthropic tool_use."""
    result = []
    for tool in tools:
        try:
            if hasattr(tool, "as_openai_tool"):
                oai = tool.as_openai_tool()
            else:
                from langchain_core.utils.function_calling import convert_to_openai_tool
                oai = convert_to_openai_tool(tool)
            fn = oai.get("function", {})
            result.append({
                "name": fn.get("name", tool.name),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        except Exception as e:
            logger.warning("_get_anthropic_tools: %s: %s", tool.name, e)
    return result


def _run_with_anthropic_client(
    llm_obj,
    model_name: str,
    messages: list,
    anthropic_tools: list,
    tools_by_name: dict,
    max_iterations: int,
    temperature: float,
    session_id: str,
    max_tokens: int = 4096,
) -> tuple:
    """
    Loop de agente usando el cliente nativo de Anthropic.
    Retorna (output_text, tools_used, iterations, usage).
    """
    client = llm_obj._client  # anthropic.Anthropic() instance
    tools_used = []
    output = ""
    iterations = 0
    _price_retry_done = False
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0}

    # Anthropic separa system del resto de mensajes
    system_content = ""
    msg_list = []
    for m in messages:
        if m.get("role") == "system":
            system_content = m.get("content", "")
        else:
            msg_list.append(m)

    call_kwargs = {
        "model": model_name,
        "max_tokens": min(max_tokens or 1024, 1024),  # respuestas WhatsApp raramente > 500 tokens
        "messages": msg_list,
    }
    # Prompt caching con TTL de 1 hora: los mensajes de WhatsApp llegan con gaps
    # de varios minutos; el TTL default de 5 min expira entre mensajes y degrada
    # el caché a puro sobrecosto de escritura. El system y los tools DEBEN ser
    # estables entre llamadas (el contexto dinámico viaja en el user message).
    _CACHE_1H = {"type": "ephemeral", "ttl": "1h"}
    if system_content:
        call_kwargs["system"] = [
            {"type": "text", "text": system_content, "cache_control": dict(_CACHE_1H)}
        ]
    if temperature is not None:
        call_kwargs["temperature"] = temperature
    if anthropic_tools:
        # Marcar el ultimo tool para activar cache del bloque completo de tools
        cached_tools = [dict(t) for t in anthropic_tools]
        if cached_tools:
            cached_tools[-1] = dict(cached_tools[-1])
            cached_tools[-1]["cache_control"] = dict(_CACHE_1H)
        call_kwargs["tools"] = cached_tools

    for i in range(max_iterations):
        iterations += 1
        response = client.messages.create(**call_kwargs)

        _u = getattr(response, "usage", None)
        if _u:
            usage["input_tokens"] += getattr(_u, "input_tokens", 0) or 0
            usage["output_tokens"] += getattr(_u, "output_tokens", 0) or 0
            usage["cache_write_tokens"] += getattr(_u, "cache_creation_input_tokens", 0) or 0
            usage["cache_read_tokens"] += getattr(_u, "cache_read_input_tokens", 0) or 0

        # Agregar respuesta del asistente al historial
        call_kwargs["messages"] = call_kwargs["messages"] + [
            {"role": "assistant", "content": response.content}
        ]

        if response.stop_reason != "tool_use":
            # Extraer texto de los bloques de contenido
            output = " ".join(
                b.text for b in response.content
                if hasattr(b, "text") and getattr(b, "type", "") == "text"
            )
            # Guard: precio en la respuesta sin haber llamado ninguna tool en
            # el turno → el precio es de memoria; forzar re-búsqueda (1 vez)
            if (not tools_used and not _price_retry_done and anthropic_tools
                    and i < max_iterations - 1 and _PRECIO_RE.search(output or "")):
                _price_retry_done = True
                logger.warning(
                    "run_agent anthropic: session=%s precio sin tools en output — "
                    "forzando re-búsqueda",
                    session_id,
                )
                call_kwargs["messages"] = call_kwargs["messages"] + [
                    {"role": "user", "content": _PRICE_RETRY_MSG}
                ]
                continue
            logger.info(
                "run_agent anthropic: session=%s iter=%s FINAL output_len=%s tools_used=%s "
                "usage[in=%s out=%s cache_w=%s cache_r=%s]",
                session_id, iterations, len(output), tools_used,
                usage["input_tokens"], usage["output_tokens"],
                usage["cache_write_tokens"], usage["cache_read_tokens"],
            )
            break

        # Procesar tool_use blocks
        tool_results = []
        for block in response.content:
            if getattr(block, "type", "") != "tool_use":
                continue

            tool_name = block.name
            tool_args = block.input or {}
            tool_id = block.id

            if tool_name not in tools_used:
                tools_used.append(tool_name)

            selected = tools_by_name.get(tool_name)
            logger.info(
                "run_agent anthropic: session=%s iter=%s tool=%s args=%s",
                session_id, iterations, tool_name, str(tool_args)[:200],
            )
            if selected:
                try:
                    result = selected.invoke(tool_args)
                    logger.info(
                        "run_agent anthropic: session=%s tool=%s result=%s",
                        session_id, tool_name, str(result)[:300],
                    )
                except Exception as e:
                    result = f"Error ejecutando {tool_name}: {str(e)}"
                    logger.error("run_agent anthropic: session=%s tool=%s ERROR: %s", session_id, tool_name, e)
            else:
                result = f"Herramienta {tool_name} no disponible."
                logger.warning("run_agent anthropic: session=%s tool=%s NOT FOUND", session_id, tool_name)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": str(result),
            })

        # Caché incremental dentro del run: quitar la marca anterior (máx. 4
        # breakpoints por request: tools + system + este) y marcar el último
        # tool_result para que la siguiente iteración lea cacheado todo el
        # prefijo de la conversación (incluye resultados de tools grandes).
        for _m in call_kwargs["messages"]:
            _c = _m.get("content")
            if isinstance(_c, list):
                for _b in _c:
                    if isinstance(_b, dict):
                        _b.pop("cache_control", None)
        if tool_results:
            tool_results[-1]["cache_control"] = {"type": "ephemeral"}

        # Agregar resultados de tools como mensaje de usuario
        call_kwargs["messages"] = call_kwargs["messages"] + [
            {"role": "user", "content": tool_results}
        ]
    else:
        logger.warning("run_agent anthropic: session=%s alcanzó max_iterations=%s", session_id, max_iterations)

    return output, tools_used, iterations, usage


def _run_with_openai_client(
    llm_obj,
    model_name: str,
    messages: list,
    openai_tools: list,
    tools_by_name: dict,
    max_iterations: int,
    temperature: float,
    session_id: str,
) -> tuple:
    """
    Loop de agente usando el cliente nativo openai.
    Preserva reasoning_content y cualquier campo extra del response.
    Retorna (output_text, tools_used, iterations, usage).
    """
    # llm_obj.client en LangChain ChatOpenAI ya ES el objeto chat.completions
    # (equivale a openai_client.chat.completions), NO el root client.
    client = llm_obj.client
    tools_used = []
    output = ""
    iterations = 0
    _price_retry_done = False
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0}

    call_kwargs = {"model": model_name, "tools": openai_tools or None}
    if temperature is not None:
        call_kwargs["temperature"] = temperature

    for i in range(max_iterations):
        iterations += 1
        response = client.create(
            messages=messages,
            **call_kwargs,
        )
        choice = response.choices[0]

        _u = getattr(response, "usage", None)
        if _u:
            usage["input_tokens"] += getattr(_u, "prompt_tokens", 0) or 0
            usage["output_tokens"] += getattr(_u, "completion_tokens", 0) or 0

        # Preservar mensaje completo (incluyendo reasoning_content si existe)
        # Si content="" y hay tool_calls, eliminar content para evitar rechazo de Kimi
        asst_dict = _msg_to_dict(choice.message)
        if asst_dict.get("content") == "" and asst_dict.get("tool_calls"):
            asst_dict.pop("content", None)
        messages.append(asst_dict)

        finish = choice.finish_reason
        tool_calls = getattr(choice.message, "tool_calls", None) or []

        if finish != "tool_calls" or not tool_calls:
            output = _extract_text(choice.message.content)
            # Guard: precio en la respuesta sin haber llamado ninguna tool en
            # el turno → el precio es de memoria; forzar re-búsqueda (1 vez)
            if (not tools_used and not _price_retry_done and openai_tools
                    and i < max_iterations - 1 and _PRECIO_RE.search(output or "")):
                _price_retry_done = True
                logger.warning(
                    "run_agent: session=%s precio sin tools en output — forzando re-búsqueda",
                    session_id,
                )
                messages.append({"role": "user", "content": _PRICE_RETRY_MSG})
                continue
            logger.info(
                "run_agent: session=%s iter=%s FINAL output_len=%s tools_used=%s",
                session_id, iterations, len(output), tools_used,
            )
            break

        # Ejecutar tool calls
        for tc in tool_calls:
            tool_name = tc.function.name
            try:
                import json as _json
                tool_args = _json.loads(tc.function.arguments or "{}")
            except Exception:
                tool_args = {}

            if tool_name not in tools_used:
                tools_used.append(tool_name)

            selected = tools_by_name.get(tool_name)
            logger.info(
                "run_agent: session=%s iter=%s tool=%s args=%s",
                session_id, iterations, tool_name, str(tool_args)[:200],
            )
            if selected:
                try:
                    result = selected.invoke(tool_args)
                    logger.info(
                        "run_agent: session=%s tool=%s result=%s",
                        session_id, tool_name, str(result)[:300],
                    )
                except Exception as e:
                    result = f"Error ejecutando {tool_name}: {str(e)}"
                    logger.error("run_agent: session=%s tool=%s ERROR: %s", session_id, tool_name, e)
            else:
                result = f"Herramienta {tool_name} no encontrada o no habilitada."
                logger.warning("run_agent: session=%s tool=%s NOT FOUND", session_id, tool_name)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result),
            })
    else:
        logger.warning("run_agent: session=%s alcanzó max_iterations=%s", session_id, max_iterations)

    return output, tools_used, iterations, usage



# Modelos que NO soportan imágenes
_NO_VISION_MODELS = ("moonshot", "kimi", "mistral", "command", "gemma")

def _analyze_image_with_vision(image_content: dict, api_key_anthropic: str = None) -> str:
    """Analiza imagen con Claude Haiku cuando el modelo principal no soporta visión."""
    try:
        import anthropic
        import os
        key = api_key_anthropic or os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            return ""
        client = anthropic.Anthropic(api_key=key)
        media_type = image_content.get("media_type", "image/jpeg")
        data = image_content.get("data", "")
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Analiza esta imagen. Si hay una etiqueta de toner/cartucho, "
                            "lee la referencia exacta (ej: M-CF283X, 85A, Q2612A). "
                            "Si es una impresora, lee el modelo exacto. "
                            "Responde solo con: 'Toner: [referencia]' o 'Impresora: [modelo]'. "
                            "Si no hay texto legible, describe brevemente."
                        ),
                    },
                ],
            }],
        )
        return response.content[0].text.strip() if response.content else ""
    except Exception as e:
        logger.warning("_analyze_image_with_vision: %s", e)
        return ""

def run_agent(
    message: str,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    db: Session = None,
    agent_config: Optional[Dict[str, Any]] = None,
    odoo_context: Optional[Dict[str, Any]] = None,
    image_content: Optional[Dict[str, Any]] = None,
) -> dict:
    """Ejecuta el agente con configuración dinámica."""
    config = agent_config or {}
    llm_model = model or config.get("model")
    llm_obj = get_llm(llm_model)

    # Herramientas
    # None → usar todas; [] → ninguna; ["name",...] → filtrar por nombre
    all_tools = create_odoo_tools(odoo_context)
    enabled_names = config.get("tools")  # None si no viene la clave
    if enabled_names is None:
        tools = all_tools
    elif enabled_names:
        tools = [t for t in all_tools if t.name in enabled_names]
    else:
        tools = []  # lista vacía explícita = sin tools (simple retry)
    tools_by_name = {t.name: t for t in tools}
    openai_tools = _get_openai_tools(tools)

    logger.info(
        "run_agent: session=%s model=%s tools_available=%s msg_len=%s",
        session_id, llm_model, len(tools), len(message),
    )

    system_prompt = config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT

    # ── Contexto dinámico ────────────────────────────────────────────────────
    # IMPORTANTE (prompt caching): todo lo que cambia entre mensajes (cliente,
    # carrito, historial del canal, FAQs) se acumula en dynamic_context y viaja
    # en el MENSAJE DE USUARIO, nunca en el system prompt. El system prompt debe
    # permanecer byte-idéntico entre llamadas para que el caché de Anthropic
    # acierte (prefijo exacto). Meter contexto variable aquí rompe el caché y
    # además factura cada llamada con sobrecosto de cache-write (1.25x).
    dynamic_context = ""
    ctx = odoo_context or {}
    _partner_id = ctx.get("partner_id")
    _partner_name = ctx.get("partner_name", "")
    _partner_vat = ctx.get("partner_vat", "")
    _partner_city = ctx.get("partner_city", "")
    _phone = ctx.get("phone", "")
    _commercial_partner_name = ctx.get("commercial_partner_name", "")
    if _partner_id and int(_partner_id or 0) > 0 and _partner_name:
        _ctx_block = (
            f"\n\n[CONTEXTO DEL CANAL WHATSAPP]\n"
            f"Cliente identificado: {_partner_name} (partner_id={_partner_id})"
        )
        if _commercial_partner_name:
            _ctx_block += f" | Empresa/cuenta: {_commercial_partner_name}"
        if _partner_vat:
            _ctx_block += f" | NIT/CC: {_partner_vat}"
        if _partner_city:
            _ctx_block += f" | Ciudad: {_partner_city}"
        if _phone:
            _ctx_block += f" | Tel: {_phone}"
        _ctx_block += (
            f"\nUSA partner_id={_partner_id} directamente en las herramientas. "
            f"NO pidas el nombre ni NIT al cliente — ya está identificado."
        )
        if _commercial_partner_name:
            _ctx_block += (
                f"\nLas cotizaciones/facturas de este cliente SIEMPRE quedan a nombre de "
                f"\"{_commercial_partner_name}\" (así funciona el sistema, sin importar quién "
                f"escriba). Si el cliente menciona el nombre de una empresa y coincide con esta, "
                f"es la MISMA cuenta — no preguntes ni pidas confirmación, solo procede."
            )
        dynamic_context += _ctx_block

    # Inyectar estado del carrito si existe
    _cart = ctx.get("cart") or {}
    _cart_lines = _cart.get("lines") or []
    _carrito_id = _cart.get("carrito_id")

    if _carrito_id and _cart_lines:
        _cart_order_id = _cart.get("order_id")
        _cart_block = f"\n\n[CARRITO DE COMPRA — {_cart.get('name', f'ID:{_carrito_id}')}]\n"
        if _cart_order_id:
            _cart_block += f"Cotización abierta: order_id={_cart_order_id}\n"

        _cart_block += "Productos en carrito:\n"
        _needs_price = []
        _has_multiple = len(_cart_lines) > 1
        _selected_lines = [l for l in _cart_lines if l.get("seleccionado_cotizar")]
        for line in _cart_lines:
            pid = line.get("product_id")
            pname = line.get("product_name", "")
            qty = line.get("qty_solicitada", 0)
            tarjeta = "✅" if line.get("tarjeta_mostrada") else "⬜"
            precio_ok = line.get("precio_mostrado", False)
            total = line.get("precio_total", 0)
            cotizado = "✅" if line.get("cotizado") else "⬜"
            sel = "✅" if line.get("seleccionado_cotizar") else "⬜"
            precio_str = f"✅ ${total:,.0f}" if precio_ok and total else ("⚠️PENDIENTE" if not precio_ok else "✅")
            _cart_block += (
                f"  • ID:{pid} {pname} | qty={qty:.0f}"
                f" | precio:{precio_str} | sel:{sel} | cotizado:{cotizado}\n"
            )
            if not precio_ok and not line.get("cotizado"):
                _needs_price.append(line)

        _cart_block += "\nREGLAS:\n"
        _cart_block += "- NO busques de nuevo productos ya en el carrito.\n"
        if _needs_price:
            p = _needs_price[0]
            _cart_block += (
                f"- ⚠️ OBLIGATORIO ANTES DE COTIZAR: llama obtener_precio"
                f"(product_id={p.get('product_id')}, partner_id=...) y muestra el precio al cliente.\n"
            )
        elif _cart_order_id:
            _cart_block += (
                f"- ✅ Todos con precio mostrado. Si el cliente da cantidad,"
                f" agrega a cotización existente (order_id={_cart_order_id}).\n"
            )
        elif _has_multiple and len(_selected_lines) == len(_cart_lines):
            _cart_block += (
                f"- Hay {len(_cart_lines)} productos (todos seleccionados). "
                f"Si el cliente elige uno: seleccionar_linea_carrito(product_id). "
                f"Si confirma todos: crear_cotizacion_desde_carrito(partner_id).\n"
            )
        else:
            _cart_block += "- ✅ Precio(s) mostrado(s). Si el cliente confirma cantidad, crea la cotización.\n"

        dynamic_context += _cart_block

    # Inyectar cotización que un asesor humano armó a mano en Odoo (sale.order
    # en 'sent', fuera del carrito del bot — ver jwb_agent_bridge.py). Sin esto
    # el agente no tiene forma de saber que existe y termina buscando el
    # último pedido CONFIRMADO (viejo) cuando el cliente pregunta por "su
    # pedido" (caso real: CAJASPRINT SA, 2026-07-30).
    _human_quote = ctx.get("cotizacion_asesor") or {}
    _hq_order_id = _human_quote.get("order_id")
    if _hq_order_id:
        _hq_block = f"\n\n[COTIZACIÓN ABIERTA — preparada por un asesor: {_human_quote.get('name', '')}]\n"
        _hq_block += f"order_id={_hq_order_id} | Total: ${_human_quote.get('amount_total', 0):,.0f}\n"
        for _l in _human_quote.get("lines") or []:
            _hq_block += (
                f"  • {_l.get('product_name', '')} × {_l.get('qty', 0):.0f} = "
                f"${_l.get('price_subtotal', 0):,.0f}\n"
            )
        _hq_block += (
            "IMPORTANTE: esta cotización la armó un asesor y sigue abierta (sin confirmar). "
            "Si el cliente pregunta por 'su pedido', 'la cotización' o algo similar, "
            "esta es la que corresponde — TIENE PRIORIDAD sobre buscar pedidos "
            "confirmados antiguos con listar_pedidos_cliente/consultar_pedidos_cliente. "
            f"Usa obtener_cotizacion(order_id={_hq_order_id}) si necesitas más detalle, "
            "o generar_link_cotizacion/confirmar_orden si el cliente quiere proceder.\n"
        )
        dynamic_context += _hq_block

    # Inyectar mensajes recientes del canal (incluyendo respuestas de asesores humanos)
    _recent_msgs = ctx.get("recent_channel_messages") or []
    if _recent_msgs:
        _msgs_block = "\n\n[HISTORIAL RECIENTE DEL CANAL]\n"
        _msgs_block += "Últimos mensajes enviados en esta conversación (del más antiguo al más reciente):\n"
        for _msg in _recent_msgs[-8:]:
            _who = _msg.get("author", "?")
            _is_int = _msg.get("is_internal_user", False)
            _role = "🔵 Asesor" if _is_int else "👤 Cliente"
            _body = (_msg.get("body") or "").strip()
            if _body:
                _msgs_block += f"  {_role} ({_who}): {_body}\n"
        _msgs_block += (
            "IMPORTANTE: Si el cliente dice 'ese', 'ese producto', 'ese precio', etc., "
            "se refiere al último producto/precio/opción mencionado en el historial anterior.\n"
        )
        dynamic_context += _msgs_block

    # Inyectar FAQs de la empresa directamente en el prompt (sin tool call)
    _faqs = ctx.get("faqs") or []
    if _faqs:
        _cat_labels = {
            "pagos": "Metodos de pago",
            "garantia": "Garantia",
            "envio": "Envios",
            "horario": "Horario y atencion",
            "general": "General",
        }
        _faq_block = "\n\n[INFORMACION OFICIAL DE LA EMPRESA - MEGATONER]\n"
        _faq_block += (
            "USA esta informacion para responder preguntas del cliente. "
            "NUNCA inventes datos que no esten aqui. "
            "Si el cliente pregunta sobre metodos de pago, horario, garantia, envios u otra politica, "
            "responde CON EXACTAMENTE el texto de esta seccion, no uses conocimiento general.\n\n"
        )
        _by_cat = {}
        for faq in _faqs:
            cat = faq.get("categoria", "general")
            _by_cat.setdefault(cat, []).append(faq)
        for cat, items in _by_cat.items():
            label = _cat_labels.get(cat, cat.title())
            _faq_block += f"**{label}**\n"
            for item in items:
                pregunta = item.get("pregunta", "")
                respuesta = item.get("respuesta", "")
                _faq_block += f"P: {pregunta}\nR: {respuesta}\n\n"
        dynamic_context += _faq_block

    # Mensaje de usuario final: contexto dinámico + mensaje del cliente.
    # El contexto va antes del mensaje para que el modelo lo lea como antecedente.
    if dynamic_context:
        user_text = f"{dynamic_context}\n\n[MENSAJE DEL CLIENTE]\n{message}"
    else:
        user_text = message

    temperature = _get_temperature(llm_model, config.get("temperature", 0.2))
    # Cap de iteraciones: un run sano termina en <= 8; más es señal de loop.
    max_iterations = min(int(config.get("max_iterations") or 8), 8)
    memory_enabled = config.get("memory_enabled", True)

    # Construir mensajes como dicts raw
    messages = [{"role": "system", "content": system_prompt}]

    if memory_enabled and session_id and db:
        memories = get_session_memories(db, session_id, limit=6)
        for m in reversed(memories):
            role = m.meta_data.get("role")
            content = (m.content or "").strip()
            if not content:
                continue  # nunca incluir mensajes vacíos — Kimi rechaza con 400
            if role == "user":
                messages.append({"role": "user", "content": content})
            elif role == "assistant":
                messages.append({"role": "assistant", "content": content})

    # Construir mensaje de usuario con imagen si está disponible
    if image_content and image_content.get("data"):
        _media_type = image_content.get("media_type", "image/jpeg")
        _img_data = image_content.get("data", "")
        _provider_prefix = llm_model.split("/")[0].lower() if "/" in llm_model else ""
        _model_lower = _model_name(llm_model).lower()
        _no_vision = any(_model_lower.startswith(p) for p in _NO_VISION_MODELS)

        if _no_vision:
            # Modelo sin visión: analizar imagen con Claude Haiku y pasar descripción como texto
            import os as _os
            _description = _analyze_image_with_vision(
                image_content,
                api_key_anthropic=_os.getenv("ANTHROPIC_API_KEY", ""),
            )
            if _description:
                _augmented = f"{user_text}\n\n[Imagen analizada por visión IA: {_description}]"
                logger.info(
                    "run_agent: session=%s imagen analizada via vision fallback: %s",
                    session_id, _description[:100],
                )
            else:
                _augmented = user_text
            messages.append({"role": "user", "content": _augmented})
        elif _provider_prefix == "anthropic":
            _user_content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _media_type,
                        "data": _img_data,
                    },
                },
                {"type": "text", "text": user_text},
            ]
            messages.append({"role": "user", "content": _user_content})
            logger.info("run_agent: session=%s imagen incluida en user message (anthropic)", session_id)
        else:
            # OpenAI y compatibles con visión (gpt-4o, etc.)
            _user_content = [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{_media_type};base64,{_img_data}"},
                },
                {"type": "text", "text": user_text},
            ]
            messages.append({"role": "user", "content": _user_content})
            logger.info("run_agent: session=%s imagen incluida en user message (openai)", session_id)
    else:
        messages.append({"role": "user", "content": user_text})

    # Ejecutar con el cliente apropiado según proveedor
    model_name = _model_name(llm_model)
    _provider = llm_model.split("/")[0].lower() if "/" in llm_model else ""
    if _provider == "anthropic":
        _anthropic_tools = _get_anthropic_tools(tools)
        output, tools_used, iterations, usage = _run_with_anthropic_client(
            llm_obj=llm_obj,
            model_name=model_name,
            messages=messages,
            anthropic_tools=_anthropic_tools,
            tools_by_name=tools_by_name,
            max_iterations=max_iterations,
            temperature=temperature,
            session_id=session_id,
            max_tokens=int(config.get("max_tokens") or 4096),
        )
    else:
        output, tools_used, iterations, usage = _run_with_openai_client(
            llm_obj=llm_obj,
            model_name=model_name,
            messages=messages,
            openai_tools=openai_tools,
            tools_by_name=tools_by_name,
            max_iterations=max_iterations,
            temperature=temperature,
            session_id=session_id,
        )

    if memory_enabled and session_id and db:
        store_memory(db, session_id, message, meta_data={"role": "user"})
        if output and output.strip():  # nunca guardar output vacío en memoria
            store_memory(db, session_id, output, meta_data={"role": "assistant"})

    # Captura para el harness de evaluación (apagada salvo JWB_EVAL_CAPTURE).
    # Envuelta en try/except: la telemetría nunca puede tumbar una conversación.
    try:
        from eval import capture as _eval_capture

        if _eval_capture.enabled():
            _eval_capture.record(
                session_id=session_id,
                model_name=llm_model,
                system_content=system_prompt,
                messages=[m for m in messages if m.get("role") != "system"],
                tool_names=[t.name for t in tools],
                temperature=temperature,
                max_iterations=max_iterations,
                odoo_context=odoo_context or {},
                max_tokens=int(config.get("max_tokens") or 4096),
                result={
                    "output": output,
                    "tools_used": tools_used,
                    "iterations": iterations,
                    "usage": usage,
                },
            )
    except Exception:
        logger.debug("eval capture: ignorado", exc_info=True)

    return {
        "output": output,
        "session_id": session_id,
        "model_used": llm_model or "default",
        "tools_used": tools_used,
        "iterations": iterations,
        "usage": usage,
        "success": True,
    }
