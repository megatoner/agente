"""Bot con catálogo restringido por categoría (Blizka, 2026-11-01).

Un bot con `bot_categ_ids` solo debe buscar, mostrar, precificar y cotizar
productos de esas categorías (child_of). Un bot SIN categorías debe comportarse
exactamente como siempre (Megatoner). Las medidas son variantes con precio y
stock propios.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_catalogo_restringido_categoria.py
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


HOGAR = call('product.category', 'search', [['name', '=', 'Linea Hogar']], limit=1)[0]
cli = call('res.partner', 'create', {'name': 'E2E CATALOGO RESTRINGIDO', 'company_type': 'company'})
toner = call('product.product', 'search',
             [['categ_id.complete_name', 'ilike', 'Cartucho Laser'], ['is_published', '=', True]], limit=1)[0]
blizka_wh = 1
base = {'partner_id': cli, 'bot_pricelist_id': 512, 'bot_warehouse_id': blizka_wh}
rest = {t.name: t for t in create_odoo_tools({**base, 'bot_categ_ids': [HOGAR],
                                              'bot_site_url': 'https://www.blizka.co'})}
libre = {t.name: t for t in create_odoo_tools(base)}

print("\nBot restringido a Línea Hogar")
out = rest['buscar_producto'].invoke({'referencia': 'colchones híbridos'})
print("    " + "\n    ".join(out.splitlines()[:8]))
check("encuentra colchones por nombre/plural/acentos", 'Colchón Híbrido' in out)
check("lista las medidas como variantes", 'Medida:' in out and 'x190' in out)
check("no mezcla productos de cartuchos", 'tóner' not in out.lower() and 'toner' not in out.lower())
check("URL del sitio de Blizka (no megatoner)",
      'megatoner.co' not in out and ('blizka.co' in out or 'URL_PRODUCTO' not in out))

out = rest['buscar_producto'].invoke({'referencia': 'toner HP 85A'})
check("un tóner NO se encuentra", 'No encontré' in out and 'NOMBRE_EXACTO' not in out,
      out.splitlines()[0][:70])
check("no manda a escalar por no encontrarlo", 'NO escales' in out)

out = rest['buscar_referencias_mensaje'].invoke({'mensaje': 'quiero 2 colchón híbrido cobalto 140'})
print("    " + "\n    ".join(out.splitlines()[:4]))
check("pedido con medida: una línea PRODUCT_ID", out.count('PRODUCT_ID:') >= 1)
check("detecta la cantidad 2 (no confunde 140 con cantidad)", 'Qty:2' in out and 'Qty:140' not in out)
check("filtra la medida pedida", 'x190)' in out and '(100x190)' not in out.split('\n')[0])

out = rest['agregar_linea_cotizacion'].invoke({'order_id': 1, 'product_id': toner, 'cantidad': 1})
out2 = rest['obtener_precio'].invoke({'product_id': toner, 'partner_id': cli})
check("obtener_precio rechaza producto fuera de categoría", 'no pertenece al catálogo' in out2, out2[:70])

print("\nBot SIN restricción (control: comportamiento de siempre)")
out = libre['buscar_producto'].invoke({'referencia': 'toner HP 85A'})
check("sigue encontrando cartuchos", 'No encontre' not in out and 'NOMBRE_EXACTO' in out or 'FICHA' in out, out.splitlines()[0][:70])
out = libre['obtener_precio'].invoke({'product_id': toner, 'partner_id': cli})
check("obtener_precio no se bloquea", 'no pertenece al catálogo' not in out)

call('res.partner', 'write', [cli], {'active': False})
print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
