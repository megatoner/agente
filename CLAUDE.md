# odoo-agents (VPS2) — CLAUDE.md

Servidor de agentes IA para el bot de WhatsApp de Grupo JPC S.A.S. (Megatoner).
Recibe peticiones del módulo Odoo `jpc_whatsapp_bot` (jwb_agent_bridge.py) y ejecuta
agentes LLM con tools que acceden a Odoo vía XML-RPC.

## Servicio
- systemd: `odoo-agents.service` (uvicorn, puerto 8000).
- venv: `/opt/odoo-agents/.venv`. Para scripts/repros: `PYTHONPATH=/opt/odoo-agents .venv/bin/python3 script.py`.
- Logs: `journalctl -u odoo-agents` (zona horaria del servidor: CEST/UTC+2; Bogotá es UTC-5).
- Tras editar código: `systemctl restart odoo-agents`.

## Estructura clave
- `app/agents/executor.py`: loop del agente (Anthropic y OpenAI). Contiene:
  - **Guard anti-precio-alucinado**: si la respuesta FINAL matchea `_PRECIO_RE` y no se
    llamó ninguna tool en el turno, se inyecta mensaje `[SISTEMA]` y se fuerza re-búsqueda
    (máx 1 reintento). Log: "precio sin tools en output — forzando re-búsqueda". NO quitar.
  - El texto que el LLM emite JUNTO a un tool_use se DESCARTA; solo el output final llega
    al cliente. `store_memory` guarda solo mensaje del usuario + output final.
- `app/odoo/tools.py`: tools de Odoo (búsqueda, precios, cotizaciones, facturas). Contiene:
  - `_commercial_id()`: **cotizaciones/facturas SIEMPRE a nombre de la empresa**
    (`commercial_partner_id`), sin importar qué contacto escribe. Aplicado en crear/agregar
    línea/listar pedidos/facturas/estado de cuenta/historial de envío. NO revertir.
  - `_tiene_precio_canal()` + `price_fallback`: si `odoo_context["price_fallback"] == "hide"`,
    los productos sin regla (qty≤1) en la pricelist del bot se OCULTAN de la búsqueda y de
    `obtener_precio`; si TODOS quedan ocultos se retorna `busqueda_tipo='sin_precio_canal'`
    (el agente no inventa precio y escala). Ante error en el chequeo → no ocultar.
  - `_build_resumen`: variantes con mismo color se diferencian por chip → "2 versiones"
    (🔧 Con chip / 🔩 Sin chip), nunca "2 colores".
  - `_get_marca_filter`: usar `limit` alto (5000) en los search_read intermedios; con
    limit=500 quedaban invisibles 19 templates de HP (bug del U4, 2026-07-07).
- Prompts de agentes: en la tabla de agentes de la DB de Odoo (agentes 19/20/21/26 = router
  + sub-agentes del bot Distribuidor).

## Prompt caching (CRÍTICO)
El system prompt debe permanecer **byte-idéntico** entre turnos para no romper el prompt
caching de Anthropic (cache_control en system + última tool). Todo contexto dinámico
(carrito, mensajes recientes, price_fallback, fecha, etc.) va en el mensaje de USUARIO,
nunca en el system prompt.

## Convenciones de trabajo
- Antes de editar un archivo en producción: backup `archivo.bak.YYYYMMDD` (los `.bak.*`
  están en .gitignore).
- Este directorio es un repo git local; commitear los cambios de cada sesión.
- Repro de búsquedas sin ensuciar canales de WhatsApp: script tipo `/tmp/repro_busqueda.py`
  usando `create_odoo_tools(ctx)` con `channel_id: None` (evita envío de tarjetas/carrito).
- La DB de Odoo es `megatoner2026` (en el servidor de Odoo, no aquí).

## Reglas de negocio (vienen del lado Odoo, respetarlas aquí)
- Meta cobra por mensaje → el agente responde TODO en un solo mensaje ordenado y legible.
- El carrito/cotización abierta solo es válido dentro de las últimas 24 h (filtro en el bridge).
- Al escalar a asesor, el bridge envía cortesía por defecto si el output queda vacío.
