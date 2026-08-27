"""Faltan datos del cliente: MIA los PIDE, no escala.

Cubre los tres que aparecían en la escalación de Emily (2026-08-27):
NIT/cédula, correo y dirección de entrega. Ninguno es motivo para mandar la
conversación a un humano — son datos que el propio cliente da en un mensaje.

Escalar solo es correcto si el cliente se NIEGA a darlos, y eso lo decide el
agente, no la tool.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_datos_faltantes.py
"""
import sys, xmlrpc.client
sys.path.insert(0, '/opt/odoo-agents')
from app.odoo.tools import create_odoo_tools

env = {}
for l in open('/opt/odoo-agents/.env'):
    l = l.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1); env[k.strip()] = v.strip().strip('"').strip("'")
url = env.get('ODOO_URL','http://127.0.0.1:8019'); db = env['ODOO_DB']
pwd = env.get('ODOO_PASSWORD') or env.get('ODOO_API_KEY')
uid = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common').authenticate(
    db, env.get('ODOO_USER') or env.get('ODOO_USERNAME'), pwd, {})
M = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')
def call(m, me, *a, **kw): return M.execute_kw(db, uid, pwd, m, me, list(a), kw)

ok, fail = [], []
def check(n, c, d=''):
    (ok if c else fail).append(n)
    print(f"  [{'OK ' if c else 'FALLA'}] {n}{(' — ' + d) if d else ''}")

# Cliente SIN nada: ni NIT, ni correo, ni dirección — como Emily
cli = call('res.partner','create',{'name':'E2E SIN DATOS FACT','company_type':'company'})
prod = call('product.product','search',[['sale_ok','=',True]], limit=1)[0]
orden = call('sale.order','create',{'partner_id':cli,'state':'draft','payment_method_id':215,
    'order_line':[(0,0,{'product_id':prod,'product_uom_qty':1})]})
canal = 4836  # el agente no puede LEER discuss.channel (AccessError); se fija el id

bot = call('jpc.whatsapp.bot.config','read',[2],
           fields=['default_pricelist_id','warehouse_id','price_fallback'])[0]
t = {x.name: x for x in create_odoo_tools({
    'partner_id':cli,'channel_id':canal,
    'bot_pricelist_id':bot['default_pricelist_id'][0],
    'bot_warehouse_id':bot['warehouse_id'][0] if bot['warehouse_id'] else 0,
    'price_fallback':bot['price_fallback']})}

def sin_escalar(txt):
    """No manda escalar, o dice explícitamente que NO escale."""
    low = txt.lower()
    return 'no escales' in low or 'escalar_a_asesor' not in low

print("\n1) consultar_datos_facturacion con el cliente vacío")
o1 = t['consultar_datos_facturacion'].invoke({})
print("    " + o1.splitlines()[0][:100])
check("no manda escalar", sin_escalar(o1))
check("dice qué falta", any(k in o1.lower() for k in ('nit','cédula','cedula','correo','falta')))

print("\n2) agregar_envio_orden sin dirección")
o2 = t['agregar_envio_orden'].invoke({'order_id': orden})
print("    " + o2.splitlines()[0][:100])
check("no manda escalar", sin_escalar(o2))
check("pide la dirección", 'direcci' in o2.lower())

print("\n3) generar_link_cotizacion sin NIT ni correo")
o3 = t['generar_link_cotizacion'].invoke({'order_id': orden})
print("    " + o3.splitlines()[0][:100])
check("no escala de entrada", 'no generes el link' in o3.lower() or sin_escalar(o3))
check("nombra los datos que faltan",
      any(k in o3.lower() for k in ('nit','cédula','cedula','correo')), o3[:80])

call('sale.order','unlink',[orden])
call('res.partner','write',[cli],{'active':False})
print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail: print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
