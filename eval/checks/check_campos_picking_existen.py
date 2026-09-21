"""Los campos de stock.picking que piden las tools de entrega EXISTEN.

Caso real 2026-09-21, Rituales Universal (573243516702): jpc_ruta eliminó el
booleano `x_entregado` el 2026-08-27, pero `_PICKING_FIELDS_ENTREGA` siguió
pidiéndolo. Odoo respondía "Invalid field", la tool devolvía "No pude consultar
el estado del pedido" y MIA escalaba a un asesor TODA consulta de estado de
pedido (39 errores en los logs 14-21 sep). Un campo pedido que otro módulo
borra rompe en silencio: nadie lo ve hasta que un cliente pregunta.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_campos_picking_existen.py
"""
import re
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


src = open('/opt/odoo-agents/app/odoo/tools.py').read()
bloque = re.search(r'_PICKING_FIELDS_ENTREGA\s*=\s*\[(.*?)\]', src, re.S).group(1)
pedidos = re.findall(r'"([a-z_0-9]+)"', bloque)
existentes = call('stock.picking', 'fields_get', attributes=['type'])
print(f"\nCampos que piden las tools de entrega: {pedidos}")
for campo in pedidos:
    check(f"stock.picking.{campo} existe", campo in existentes)

# La tool completa, sobre un cliente real con pedidos (RITUALES FUNERARIOS).
partner = call('res.partner', 'search', [['name', 'ilike', 'RITUALES FUNERARIOS']], limit=1)
if partner:
    tools = {t.name: t for t in create_odoo_tools({'partner_id': partner[0]})}
    out = tools['consultar_estado_pedido_cliente'].invoke({'partner_id': partner[0]})
    print("    " + "\n    ".join(out.splitlines()[:3]))
    check("consultar_estado_pedido_cliente responde", 'no pude consultar' not in out.lower())

print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
