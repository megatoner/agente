"""Prueba real: el bot Distribuidor debe marcar agotadas las tintas 544 de color.

Llama a la tool de búsqueda por el mismo camino que el agente (XML-RPC con el
contexto de almacén del bot) y comprueba el stock que ve. No consume créditos de
IA: verifica la capa que estaba rota, no la redacción del mensaje.
"""
import os
import sys
import xmlrpc.client

env = {}
for linea in open('/opt/odoo-agents/.env'):
    linea = linea.strip()
    if linea and not linea.startswith('#') and '=' in linea:
        k, v = linea.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

url = env.get('ODOO_URL', 'http://127.0.0.1:8019')
db = env.get('ODOO_DB', 'megatoner2026')
user = env.get('ODOO_USER') or env.get('ODOO_USERNAME', '')
pwd = env.get('ODOO_PASSWORD') or env.get('ODOO_API_KEY', '')

common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common')
uid = common.authenticate(db, user, pwd, {})
models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')

# Igual que tools.py: _warehouse_id viene de odoo_context['bot_warehouse_id']
WAREHOUSE_BOT_DISTRIBUIDOR = 2
wh_ctx = {"warehouse_id": WAREHOUSE_BOT_DISTRIBUIDOR}

TMPLS = [1839, 1841, 1843, 1845]  # amarillo, cian, magenta, negro compatibles
variantes = models.execute_kw(
    db, uid, pwd, 'product.product', 'search_read',
    [[['product_tmpl_id', 'in', TMPLS], ['active', '=', True]]],
    {'fields': ['default_code', 'display_name', 'qty_available'],
     'limit': 30, 'context': wh_ctx})

ok, fail = [], []


def check(nombre, cond, detalle=''):
    (ok if cond else fail).append(nombre)
    print(f"  [{'OK ' if cond else 'FALLA'}] {nombre}{(' — ' + detalle) if detalle else ''}")


print("\nLo que ve el bot Distribuidor de las tintas 544 compatibles:")
por_codigo = {}
for v in sorted(variantes, key=lambda x: x['default_code'] or ''):
    q = v.get('qty_available') or 0
    por_codigo[v['default_code']] = q
    estado = 'disponible' if q > 0 else '❌ agotado'
    print(f"    {v['default_code']:<9} {q:>7.0f}   {estado}")

print()
check("el negro sigue disponible", por_codigo.get('225005', 0) > 0,
      f"{por_codigo.get('225005', 0):.0f} unidades")
for cod, color in (('225006', 'cian'), ('225007', 'magenta'), ('225008', 'amarillo')):
    check(f"el {color} se ve agotado", por_codigo.get(cod, -1) == 0,
          f"{por_codigo.get(cod)} en el almacén del bot")

print(f"\n{'='*54}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*54}\n")
sys.exit(1 if fail else 0)
