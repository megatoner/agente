"""El guard anti-alucinación de precios no dispara sobre un $ que YA venía de
una FAQ oficial (contexto ya verificado, no un tool call).

Caso real YAILYN (573173637315, canal 5236, 2026-09-21): preguntó ubicación,
horario y costo de domicilio a mitad del flujo de pago ('¿domicilio o
recoges?'). Las 3 FAQs existen y son correctas, pero el guard trataba
CUALQUIER '$' sin tool call en el turno como precio sin verificar — incluido
uno que salía TEXTUAL de la FAQ de envío inyectada en el mismo mensaje. El
reintento forzado le hizo perder la ubicación y el horario, y MIA terminó
repitiendo solo la pregunta pendiente del flujo, ignorando lo que la clienta
preguntó — 3 veces seguidas, hasta que pidió un asesor.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_faq_precio_no_dispara_guard.py
"""
import sys

sys.path.insert(0, '/opt/odoo-agents')
from app.agents.executor import _precio_o_codigo_no_verificado  # noqa: E402

ok, fail = [], []


def check(n, c, d=''):
    (ok if c else fail).append(n)
    print(f"  [{'OK ' if c else 'FALLA'}] {n}{(' — ' + d) if d else ''}")


FAQ_ENVIO = (
    "[INFORMACION OFICIAL DE LA EMPRESA - MEGATONER]\n"
    "**Envios**\nP: cuanto vale el domicilio\n"
    "R: Medellín y área metro: desde $12.000 COP\n"
)

caso_yailyn = (
    "📍 Estamos en la Cra 80B 32EE-41, La Castellana – Medellín. "
    "🕗 Horario: lunes a viernes 8:00 a.m. a 5:40 p.m., sábados 8:00 a.m. a 12:00 p.m. "
    "El domicilio dentro de Medellín cuesta $12.000. "
    "¿Te lo enviamos a domicilio o lo recoges en tienda? 😊"
)
check("1. respuesta con precio de FAQ NO dispara el guard",
      not _precio_o_codigo_no_verificado(caso_yailyn, [], "", FAQ_ENVIO))

check("2. mismo monto con puntuación distinta ($12.000,00) tampoco dispara",
      not _precio_o_codigo_no_verificado(
          "El domicilio cuesta $12.000,00.", [], "", FAQ_ENVIO))

check("3. sin ninguna FAQ/tool de respaldo, un precio SÍ dispara (no se relaja el guard)",
      _precio_o_codigo_no_verificado("Ese producto vale $45.000.", [], "", ""))

check("4. un precio que NO está en la FAQ (inventado) sí dispara aunque haya FAQ de otro tema",
      _precio_o_codigo_no_verificado("Ese tóner especial cuesta $999.999.", [], "", FAQ_ENVIO))

check("5. sin ningún $ en el output, nunca dispara",
      not _precio_o_codigo_no_verificado("¿Te lo enviamos a domicilio o lo recoges?", [], "", FAQ_ENVIO))

print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
