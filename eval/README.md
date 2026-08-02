# Harness de evaluación de MIA

Sirve para responder una sola pregunta: **¿este cambio degrada al bot?**
Se usa antes de tocar el system prompt, cambiar de modelo o recortar tools.

## Por qué existe

El costo de MIA está dominado por el **prefijo estático** (system prompt +
definiciones de tools = ~13.500 tokens), que se relee **una vez por iteración**
del loop del agente. Medido en producción:

| tools en el run | runs (7d) | cache_read | costo/run |
|---|---|---|---|
| 0 | 665 | 12.779 | $0,0092 |
| 1 | 428 | 26.695 | $0,0266 |
| 3 | 94 | 56.422 | $0,0395 |

El incremento por tool call (~14.000 tokens) es casi todo prefijo releído: el
resultado de la tool en sí pesa ~600 tokens. Por eso la única palanca real de
costo es **achicar el prefijo** — y eso toca reglas de negocio, así que hay que
verificarlo con datos, no con criterio.

## Flujo de trabajo

### 1. Capturar tráfico real

La captura se activa con la variable `JWB_EVAL_CAPTURE`, puesta en el drop-in
de systemd `/etc/systemd/system/odoo-agents.service.d/eval-capture.conf`.

> **NO poner esa variable en `.env`.** `pydantic-settings` valida ese archivo
> con `extra="forbid"` y el servicio no arranca. (Pasó: 45 s de caída.)

Para desactivar: borrar el drop-in, `systemctl daemon-reload && systemctl
restart odoo-agents`.

Cada run real escribe un registro en `eval/data/captura.jsonl` con todo lo
necesario para re-ejecutarlo: mensajes, `odoo_context`, tools habilitadas,
modelo, y el resultado que se obtuvo en producción. El system prompt se guarda
deduplicado en `eval/data/systems/<sha>.txt`. Las imágenes se despojan (solo
queda un marcador) y esos escenarios se saltan en el replay.

> `captura.jsonl` contiene datos de clientes (nombre, NIT, teléfono).
> No debe salir del servidor.

Juntar **al menos 50 escenarios** antes de sacar conclusiones. A ~450 runs/día
eso es un par de horas.

### 1-bis. …o reconstruirlos de conversaciones pasadas (más rápido)

No hace falta esperar tráfico nuevo: `backfill.py` arma escenarios a partir de
burbujas ya contestadas. Primero se extrae una muestra **estratificada** (máx.
2 burbujas por canal, para no sobre-representar una sola conversación):

```bash
sudo -u postgres psql -d megatoner2026 -At -c "
select json_agg(row_to_json(t)) from (
  select id, channel_id, partner_id, bot_id, unified_text, create_date from (
    select b.id, b.channel_id, b.partner_id, b.bot_id, b.unified_text,
           to_char(b.create_date,'YYYY-MM-DD HH24:MI:SS') as create_date,
           row_number() over (partition by b.channel_id order by b.id) as rn
    from jpc_whatsapp_bot_burbuja b
    where b.state='answered' and b.answered_via='bot'
      and b.create_date > now() - interval '7 days'
      and coalesce(b.unified_text,'') <> ''
      and length(b.unified_text) between 8 and 600
  ) s where s.rn <= 2
  order by random()
) t;" > eval/data/hist_raw.json

./.venv/bin/python -m eval.backfill --limit 60
```

Qué se reconstruye fielmente: el mensaje del cliente, sus datos, la
configuración del bot (pricelist / bodega / `price_fallback`), el historial del
canal **filtrado a mensajes anteriores a esa burbuja**, y las FAQs con el mismo
filtro de keywords del bridge.

Qué **no** se reconstruye (no queda registro histórico): el estado del carrito,
la cotización de asesor vigente entonces, y el resumen de sesión anterior. Para
un A/B da igual — ambos brazos reciben el mismo contexto. Pero los flujos de
carrito hay que cubrirlos con captura en vivo.

### 2. Replicar los brazos

```bash
cd /opt/odoo-agents
set -a; . ./.env; set +a

# CONTROL — mismo config que producción, para medir el piso de ruido
./.venv/bin/python -m eval.replay --out eval/data/arm_control.jsonl

# BASE — idéntico al control (segunda corrida del mismo config)
./.venv/bin/python -m eval.replay --out eval/data/arm_base.jsonl

# VARIANTE — system prompt alterno
./.venv/bin/python -m eval.replay \
    --system /ruta/prompt_nuevo.txt --out eval/data/arm_nuevo.jsonl

# VARIANTE — modelo alterno
./.venv/bin/python -m eval.replay \
    --model anthropic/claude-haiku-4-5-20251001 --out eval/data/arm_haiku.jsonl
```

### 3. Comparar

```bash
# primero el control (--control), para saber cuánto ruido hay sin cambiar nada
./.venv/bin/python -m eval.compare \
    eval/data/arm_control.jsonl eval/data/arm_base.jsonl --control

# después la variante, contra el mismo baseline
./.venv/bin/python -m eval.compare eval/data/arm_base.jsonl eval/data/arm_nuevo.jsonl
```

**Regla de lectura: la variante se compara contra el control, no contra cero.**
Con el piso medido abajo (tools 3,3%), un 4% en la variante es ruido; un 25%
es regresión.

Señales bloqueantes que reporta `compare`:

1. **Escalación cambiada** — resuelve donde antes escalaba, o al revés.
2. **Respuesta vacía** — el cliente se queda sin contestación.
3. **Tools distintas** — cambió la decisión del agente.
4. **Precios distintos** — los importes citados no coinciden.

### 4. Compuerta final antes de desplegar

El harness compara *decisiones*, no el pipeline completo. Antes de dejar un
cambio en producción, correr además la prueba end-to-end con webhook simulado
y cliente de prueba dedicado (burbuja → dispatcher → agente → tools → respuesta
real por WhatsApp), y limpiar todo al final.

## Seguridad: qué NO puede pasar en un replay

`eval/dryrun.py` intercepta `OdooClient.execute_kw`, que es el único camino de
las tools hacia Odoo — incluidos los envíos de WhatsApp. Política:

| Operación | Resultado |
|---|---|
| Lecturas (`search_read`, `read`, ...) | pasan al servidor, datos reales |
| `create` de `sale.order` / `sale.order.line` | **permitido** (borrador desechable) |
| `write`/`unlink` sobre IDs creados en la sesión | permitido |
| `write`/`unlink` sobre registros **preexistentes** | **BLOQUEADO** |
| `action_confirm`, `action_post`, `create_invoices`, `message_post` | **BLOQUEADO** |
| `jwb_enviar_tarjeta_v3` y demás `jwb_enviar_*` | **BLOQUEADO** |

Los borradores se crean porque **así se calcula el precio** que MIA le dice al
cliente (`_get_product_price_for_client` crea una `sale.order` efímera, lee
`price_unit` y la borra). Bloquear eso deja al agente sin precios y vuelve
ciego al A/B justo en la señal más crítica. Al salir del guard, cualquier
borrador que haya quedado se elimina.

**Propiedad garantizada:** un replay nunca modifica ni borra un registro que
existía antes, nunca confirma ni factura ni cobra, y ningún cliente recibe
nada.

## Piso de ruido medido (60 escenarios reales, 2026-08-02)

Dos corridas de la **misma** configuración sobre 60 escenarios de 57 canales:

| Señal | Tasa de ruido |
|---|---|
| Escalación cambiada | **0,0%** |
| Respuesta vacía | **0,0%** |
| Tools distintas | **3,3%** |
| Precios distintos | **3,3%** |

Costo: $0,0104 vs $0,0101 por escenario (−2,9%, dentro del ruido).
Iteraciones: 86 vs 86, idéntico.

Lectura: las dos señales más críticas del negocio (escalar a un asesor y dejar
al cliente sin respuesta) son **perfectamente estables**, así que ahí el
harness detecta cualquier regresión real. En tools y precios hay que superar
~3% para hablar de regresión.

Con temperatura 0.2, el mismo escenario corrido dos veces con configuración
idéntica puede producir respuestas materialmente distintas. Ejemplo real de la
validación del harness (mismo input, mismo prompt, mismo modelo):

```
A: * Tóner HP 26A (M-CF226A) — $60.500
   * Tóner HP 26X (M-CF226X/CRG052H) — $99.000

B: * Tóner HP 26A (M-CF226A) — Con chip
   * Tóner HP 26X (M-CF226X/CRG052H) — Con chip (mayor rendimiento)
   ¿Cuál te interesa o te cotizo alguna de las dos?
```

Uno cotizó precios, el otro preguntó primero. Por eso el brazo de control no
es opcional.

## Archivos

| Archivo | Qué hace |
|---|---|
| `capture.py` | Graba runs reales (hook en `run_agent`, apagado por defecto) |
| `dryrun.py` | Candado de efectos secundarios |
| `replay.py` | Re-ejecuta escenarios con config alterna |
| `compare.py` | Diff de dos brazos + veredicto |
| `smoke.py` | Prueba de humo: un run real por el camino completo |
| `backfill.py` | Reconstruye escenarios de conversaciones ya ocurridas |
