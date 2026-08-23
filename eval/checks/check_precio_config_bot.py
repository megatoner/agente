"""Las dos reglas de precio del bot:

  1. Precios y órdenes se calculan con la configuración del bot (pricelist +
     almacén). Sin fijarlos, la orden toma la pricelist del cliente y el almacén
     por defecto del usuario bot_agent — se cotiza de una bodega y se despacha
     de otra (108 de 141 pedidos del bot Distribuidor salieron de GRUPO-MED-UF
     teniendo GRUPO-MED-DIS configurado).

  2. Un producto que calcula precio 0 NO se muestra. Un $0 no es un producto
     gratis, es una regla de pricelist rota: en la lista 11 'Canales' el
     HP 38A/42X/45A y el HP 37A calculaban $0 y ya se materializó en el pedido
     S48158 (2 unidades a $0).

Ejecutar:
    cd /opt/odoo-agents && python3 eval/checks/check_precio_config_bot.py
"""
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


def call(model, method, *args, **kw):
    return models.execute_kw(db, uid, pwd, model, method, list(args), kw)


ok, fail = [], []


def check(nombre, cond, detalle=''):
    (ok if cond else fail).append(nombre)
    print(f"  [{'OK ' if cond else 'FALLA'}] {nombre}{(' — ' + detalle) if detalle else ''}")


def precio_con_lista(product_id, partner_id, pricelist_id, warehouse_id=None):
    """Igual que _get_product_price_for_client: precio a qty=1 con esa lista."""
    vals = {'partner_id': partner_id, 'state': 'draft', 'payment_method_id': 215}
    if pricelist_id:
        vals['pricelist_id'] = pricelist_id
    if warehouse_id:
        vals['warehouse_id'] = warehouse_id
    oid = call('sale.order', 'create', vals)
    lid = call('sale.order.line', 'create',
               {'order_id': oid, 'product_id': product_id, 'product_uom_qty': 1.0})
    linea = call('sale.order.line', 'read', [lid], fields=['price_unit'])[0]
    orden = call('sale.order', 'read', [oid], fields=['warehouse_id', 'pricelist_id'])[0]
    call('sale.order', 'unlink', [oid])
    return float(linea.get('price_unit') or 0), orden


bot = call('jpc.whatsapp.bot.config', 'read', [4],
           fields=['name', 'default_pricelist_id', 'warehouse_id'])[0]
PL = bot['default_pricelist_id'][0]
WH = bot['warehouse_id'][0]
# Un cliente cualquiera del canal Distribuidor sirve de sujeto de prueba.
PARTNER = call('res.partner', 'search', [['is_company', '=', True]], limit=1)[0]

# HP 38A/42X/45A: regla en $0 en la lista 11 (el caso que motivó la regla)
CERO = call('product.product', 'search',
            [['product_tmpl_id', '=', 2522], ['active', '=', True]], limit=1)[0]
# Tinta 544 negra compatible: precio sano, sirve de control
SANO = call('product.product', 'search', [['default_code', '=', '225005']], limit=1)[0]

print(f"\nBot: {bot['name']} · lista {PL} · almacén {bot['warehouse_id'][1]}")

print("\n1) La orden se crea con la configuración del bot")
_, orden = precio_con_lista(SANO, PARTNER, PL, WH)
check("la orden queda con la pricelist del bot",
      orden['pricelist_id'] and orden['pricelist_id'][0] == PL,
      str(orden['pricelist_id']))
check("la orden queda con el almacén del bot",
      orden['warehouse_id'] and orden['warehouse_id'][0] == WH,
      str(orden['warehouse_id']))

print("\n2) Sin fijar el almacén, la orden NO cae sola en el del bot")
# Si cayera sola, el fix sería innecesario y esta prueba lo delataría.
_, orden_sin = precio_con_lista(SANO, PARTNER, PL, None)
check("el almacén por defecto es otro",
      not orden_sin['warehouse_id'] or orden_sin['warehouse_id'][0] != WH,
      f"por defecto: {orden_sin['warehouse_id']}")

print("\n3) El producto con regla en $0 se detecta como no vendible")
p_cero, _ = precio_con_lista(CERO, PARTNER, PL, WH)
check("calcula 0 con la lista del bot", p_cero <= 0, f"${p_cero:,.0f}")
check("el producto de control sí tiene precio",
      precio_con_lista(SANO, PARTNER, PL, WH)[0] > 0)

print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
