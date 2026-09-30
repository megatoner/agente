"""quitar_envio_orden debe instruir al agente a seguir respondiéndole al
cliente en el MISMO turno — esta tool, a diferencia de escalar_a_asesor o
enviar_tarjeta_producto, no le dice nada al cliente por su cuenta.

Caso real INNOVACION & SISTEMAS IT S.A.S (573245780633, canal 3214,
2026-09-29): el cliente dijo "recoger en la tienda", el agente llamó
quitar_envio_orden (el fix de VICTOR funcionó: no se cobró flete de más), pero
no escribió nada al cliente después — tools_used=['quitar_envio_orden'],
output_len=0. Como esa tool no está en _TOOLS_QUE_COMUNICAN_SOLAS (jwb_agent_
bridge.py), el bot trató el silencio como fallo y escaló a un asesor sin
necesidad; el cliente esperó ~25 min a que un humano retomara. La escalación
en sí es el comportamiento correcto ante un silencio real (mejor escalar de
más que dejar al cliente sin respuesta) — lo que sobra es el silencio.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_quitar_envio_avisa_seguir_respondiendo.py
"""
import sys
import xmlrpc.client

sys.path.insert(0, '/opt/odoo-agents')
from app.odoo.tools import create_odoo_tools  # noqa: E402

env = {}
for l in open('/opt/odoo-agents/.env'):
    l = l.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
url = env.get('ODOO_URL', 'http://127.0.0.1:8019')
db, pwd = env['ODOO_DB'], (env.get('ODOO_PASSWORD') or env.get('ODOO_API_KEY'))
user = env.get('ODOO_USER') or env.get('ODOO_USERNAME')
uid = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common').authenticate(db, user, pwd, {})
M = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')


def call(model, method, *a, **kw):
    return M.execute_kw(db, uid, pwd, model, method, list(a), kw)


ok, fail = [], []


def check(n, c, d=''):
    (ok if c else fail).append(n)
    print(f"  [{'OK ' if c else 'FALLA'}] {n}{(' — ' + d) if d else ''}")


# Reutiliza un contacto YA EXISTENTE — crear res.partner por XML-RPC con este
# usuario está roto hoy (hallazgo aparte, no relacionado, ver CLAUDE.md).
cli = call('res.partner', 'search', [['customer_rank', '>', 0]], limit=1)[0]
prod = call('product.product', 'search', [['sale_ok', '=', True]], limit=1)[0]
orden = call('sale.order', 'create', {
    'partner_id': cli, 'state': 'draft', 'payment_method_id': 215,
    'order_line': [(0, 0, {'product_id': prod, 'product_uom_qty': 1})]})

tools = {t.name: t for t in create_odoo_tools({'partner_id': cli})}
out = tools['quitar_envio_orden'].invoke({'order_id': orden})
print("    " + out)

check("marca la orden como recogida (comportamiento original, sin cambios)",
      'RECOGIDA EN TIENDA' in out)
check("instruye responderle al cliente EN ESTE MISMO MENSAJE",
      'EN ESTE MISMO MENSAJE' in out or 'respóndele al cliente' in out.lower())
check("advierte que la tool no habla sola (a diferencia de escalar_a_asesor)",
      'no le dice nada al cliente' in out.lower())

call('sale.order', 'action_cancel', [orden])

print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
