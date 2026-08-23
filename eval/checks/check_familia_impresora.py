"""La familia de impresora decide la línea de consumible.

Un mismo número de modelo existe en la línea de tinta y en la de láser, y los
consumibles son incompatibles. La palabra que las distingue ('deskjet',
'laserjet') se descartaba antes de buscar: no tiene 2 dígitos, así que
`_valido_para_similar` la rechazaba y quedaba solo el número.

Caso real 2026-08-23 (Hernan, 573103720445): "hp deskjet 3050" devolvió un
Tóner HP 12A. El término '3050' está ligado a ese tóner por la LaserJet 3050 —
correcto para láser, inservible para una DeskJet, que usa tinta HP 122.

Segundo defecto del mismo caso: buscar 'HP 122' devolvía el Tóner HP 12A porque
`_similar` hacía ilike '%122%' y matcheaba el término '4122' (LaserJet 4122) sin
respetar el límite de número completo.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_familia_impresora.py
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
db = env['ODOO_DB']
pwd = env.get('ODOO_PASSWORD') or env.get('ODOO_API_KEY')
user = env.get('ODOO_USER') or env.get('ODOO_USERNAME')
uid = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common').authenticate(db, user, pwd, {})
M = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')


def call(model, method, *a, **kw):
    return M.execute_kw(db, uid, pwd, model, method, list(a), kw)


bot = call('jpc.whatsapp.bot.config', 'read', [2],
           fields=['default_pricelist_id', 'warehouse_id', 'price_fallback'])[0]
partner = call('res.partner', 'search', [['is_company', '=', True]], limit=1)[0]
tools = {t.name: t for t in create_odoo_tools({
    'partner_id': partner,
    'bot_pricelist_id': bot['default_pricelist_id'][0],
    'bot_warehouse_id': bot['warehouse_id'][0] if bot['warehouse_id'] else 0,
    'price_fallback': bot['price_fallback'],
})}

ok, fail = [], []


def check(nombre, cond, detalle=''):
    (ok if cond else fail).append(nombre)
    print(f"  [{'OK ' if cond else 'FALLA'}] {nombre}{(' — ' + detalle) if detalle else ''}")


def buscar(q):
    return tools['buscar_producto'].invoke({'referencia': q})


print("\n1) El caso Hernan: una DeskJet no recibe un tóner láser")
for consulta in ('hp deskjet 3050', 'HP 61 deskjet 3050'):
    out = buscar(consulta)
    check(f"{consulta!r} no ofrece el Tóner HP 12A", '12A' not in out,
          out.splitlines()[0][:70])

print("\n2) Buscar 'HP 122' no devuelve el Tóner HP 12A")
# '122' matcheaba el término '4122' por ilike sin límite de número.
out = buscar('HP 122')
check("no confunde 122 con 12A", '12A' not in out, out.splitlines()[0][:70])

print("\n3) Sin regresión: lo que debía encontrarse se sigue encontrando")
for consulta, esperado in (('122XL', 'Encontré'), ('HP 122XL', 'Encontré'),
                           ('12A', 'Encontré'), ('544', 'Tenemos')):
    out = buscar(consulta)
    check(f"{consulta!r} sigue devolviendo resultados", out.startswith(esperado),
          out.splitlines()[0][:70])

print("\n4) Una LaserJet sí recibe tóner (el guard no invierte el sentido)")
out = buscar('hp laserjet 1020')
check("'hp laserjet 1020' ofrece un tóner",
      '12A' in out or 'óner' in out or 'Encontré' in out,
      out.splitlines()[0][:70])

print(f"\n{'='*58}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*58}\n")
sys.exit(1 if fail else 0)
