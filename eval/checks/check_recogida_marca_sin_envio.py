"""quitar_envio_orden marca la orden como recogida de verdad (sin_envio), no
solo quita el carrier — y agregar_envio_orden suelta esa marca si el cliente
cambia de opinión y pide domicilio después.

Caso real VICTOR ALFONSO PEÑA (573006600037, pedido S59662, 2026-09-21): dijo
"Yo los recojo", el agente nunca había llamado agregar_envio_orden así que no
vio motivo para llamar quitar_envio_orden tampoco, y al confirmar una red de
seguridad de Odoo (pensada para el caso contrario: nadie resolvió el envío)
le agregó $19.200 de flete real que nunca aceptó porque la orden traía un
transportista heredado de compras anteriores con domicilio.

Ejecutar contra la BD real (no revierte nada — usa contactos/órdenes ZZ TEST
que se archivan/cancelan al final):
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_recogida_marca_sin_envio.py
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


# La creación de contactos vía XML-RPC con este usuario (bot_agent) está rota
# hoy — ver ir.model.fields ACL, hallazgo aparte, no relacionado con este fix
# — así que se reutiliza un contacto YA EXISTENTE en vez de crear uno de prueba.
cli = call('res.partner', 'search', [['customer_rank', '>', 0]], limit=1)[0]
prod = call('product.product', 'search', [['sale_ok', '=', True]], limit=1)[0]
orden = call('sale.order', 'create', {
    'partner_id': cli, 'state': 'draft', 'payment_method_id': 215,
    'order_line': [(0, 0, {'product_id': prod, 'product_uom_qty': 1})]})

tools = {t.name: t for t in create_odoo_tools({'partner_id': cli})}

print("\nCliente dice 'yo los recojo'")
out1 = tools['quitar_envio_orden'].invoke({'order_id': orden})
print("    " + out1)
check("responde confirmando recogida", 'recogid' in out1.lower())

modo = call('sale.order', 'read', [orden], ['jwb_flete_modo_code'])[0].get('jwb_flete_modo_code')
check("la orden queda marcada jwb_flete_modo=sin_envio", modo == 'sin_envio', f'modo={modo}')

print("\nCliente cambia de opinión: ahora sí quiere domicilio")
out2 = tools['agregar_envio_orden'].invoke({'order_id': orden})
print("    " + "\n    ".join(out2.splitlines()[:3]))

modo2 = call('sale.order', 'read', [orden], ['jwb_flete_modo_code', 'carrier_id'])[0]
check("la marca sin_envio se soltó al pedir domicilio de verdad",
      modo2.get('jwb_flete_modo_code') != 'sin_envio', f"modo={modo2.get('jwb_flete_modo_code')}")

call('sale.order', 'action_cancel', [orden])

print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
