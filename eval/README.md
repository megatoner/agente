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
# primero el control, para saber cuánto ruido hay sin cambiar nada
./.venv/bin/python -m eval.compare eval/data/arm_control.jsonl eval/data/arm_base.jsonl

# después la variante, contra el mismo baseline
./.venv/bin/python -m eval.compare eval/data/arm_base.jsonl eval/data/arm_nuevo.jsonl
```

**Regla de lectura: la variante se compara contra el control, no contra cero.**
Si el control da 12% de "tools distintas", un 14% en la variante es ruido; un
40% es regresión.

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

## Piso de ruido conocido

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
