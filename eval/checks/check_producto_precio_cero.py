"""Un producto que calcula precio 0 NO llega al cliente.

Invoca la tool real de búsqueda con el contexto del bot Distribuidor — el mismo
camino que usa MIA — y comprueba que el HP 38A/42X/45A (regla en $0 en la lista
11 'Canales') no aparece en la respuesta.

No consume créditos de IA: llama la herramienta, no al modelo.

Ejecutar:
    cd /opt/odoo-agents && python3 eval/checks/check_producto_precio_cero.py
"""
import sys
import xmlrpc.client

sys.path.insert(0, '/opt/odoo-agents')

from app.odoo.tools import create_odoo_tools  # noqa: E402

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


bot = call('jpc.whatsapp.bot.config', 'read', [4],
           fields=['name', 'default_pricelist_id', 'warehouse_id', 'price_fallback'])[0]
partner = call('res.partner', 'search', [['is_company', '=', True]], limit=1)[0]

ctx = {
    'partner_id': partner,
    'bot_pricelist_id': bot['default_pricelist_id'][0],
    'bot_warehouse_id': bot['warehouse_id'][0],
    'price_fallback': bot['price_fallback'],
    # sin channel_id: no se envían tarjetas ni se toca ningún carrito real
}
tools = {t.name: t for t in create_odoo_tools(ctx)}

print(f"\nBot: {bot['name']} · lista {bot['default_pricelist_id'][1]}")
print(f"Tools disponibles: {len(tools)}")

print("\n1) El HP 38A (regla en $0) no se ofrece")
salida = tools['buscar_producto'].invoke({'referencia': '38A'})
print("    " + "\n    ".join(salida.splitlines()[:6]))
check("no lo lista como disponible",
      '38A/42X/45A' not in salida or 'no disponible' in salida.lower()
      or 'asesor' in salida.lower(),
      salida.splitlines()[0][:90] if salida else '')
check("no muestra un precio de $0", '$0' not in salida and '$ 0' not in salida)

print("\n2) Un producto sano sí se ofrece con su precio")
# Por referencia comercial, que es como escribe el cliente — no por código
# interno (225005), que la búsqueda no indexa como término.
salida_ok = tools['buscar_producto'].invoke({'referencia': '544'})
check("encuentra las tintas 544", '544' in salida_ok,
      salida_ok.splitlines()[0][:90] if salida_ok else '')
check("con un precio real, no $0",
      ('$' in salida_ok) and '$0' not in salida_ok and '$ 0' not in salida_ok)
check("marca como agotado lo que no hay en su almacén",
      'agotado' in salida_ok.lower(),
      "el Distribuidor solo tiene el negro")

print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
