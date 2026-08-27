"""Sin dirección, MIA pide los datos — no escala.

Caso real 2026-08-27, Emily (BSUID CO.1466700915216544): contacto nuevo sin
dirección, pidió domicilio, y `agregar_envio_orden` falló con "carrier_id
nulo". La tool respondía "Solicita a tu asesor que lo gestione", así que MIA
escaló; una asesora tuvo que preguntarle la dirección a mano y rehacer el
pedido. 5 escalaciones así en agosto.

Sin dirección no hay transportista posible — pero eso no es una avería, es
que todavía no se la hemos pedido al cliente.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_envio_sin_direccion.py
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


# Cliente SIN dirección, como Emily
cli = call('res.partner', 'create', {'name': 'E2E SIN DIRECCION', 'company_type': 'company'})
prod = call('product.product', 'search', [['sale_ok', '=', True]], limit=1)[0]
orden = call('sale.order', 'create', {
    'partner_id': cli, 'state': 'draft', 'payment_method_id': 215,
    'order_line': [(0, 0, {'product_id': prod, 'product_uom_qty': 1})]})

bot = call('jpc.whatsapp.bot.config', 'read', [2],
           fields=['default_pricelist_id', 'warehouse_id', 'price_fallback'])[0]
tools = {t.name: t for t in create_odoo_tools({
    'partner_id': cli,
    'bot_pricelist_id': bot['default_pricelist_id'][0],
    'bot_warehouse_id': bot['warehouse_id'][0] if bot['warehouse_id'] else 0,
    'price_fallback': bot['price_fallback'],
})}

print("\nCliente sin dirección pide domicilio")
out = tools['agregar_envio_orden'].invoke({'order_id': orden})
print("    " + "\n    ".join(out.splitlines()[:4]))

check("NO le dice al agente que escale",
      'escalar' not in out.lower() or 'no escales' in out.lower(),
      out.splitlines()[0][:70])
check("le dice que pida la dirección",
      'direcci' in out.lower() and ('pídele' in out.lower() or 'pide' in out.lower()))
check("no menciona 'solicita a tu asesor'",
      'solicita a tu asesor' not in out.lower())

# Limpieza: el usuario del agente no tiene permiso para borrar contactos
# (grupo Contact/Creation), así que se archiva en vez de eliminarse.
call('sale.order', 'unlink', [orden])
call('res.partner', 'write', [cli], {'active': False})

print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
