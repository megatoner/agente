"""Bot con categorías Y catálogo de cartuchos (bot Usuario Final, 2026-11-22).

La búsqueda por nombre de Blizka descartaba números y palabras de <3 letras: con categorías, «667»,
«HP 667», «3635» y «150A» daban «no encontrado» aunque los productos existen DENTRO de esas
categorías. Ahora un catálogo con términos de búsqueda usa el motor normal y las categorías son
solo un candado sobre el resultado. Blizka (sin términos) y un bot sin categorías no cambian.

Solo lectura sobre producción. Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_catalogo_cartuchos_con_categorias.py
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


def fuentes(bot_id):
    rows = call('jpc.whatsapp.bot.busqueda.fuente', 'search_read', [['bot_id', '=', bot_id]],
                fields=['tipo', 'atributo_id', 'coincidencia', 'activo'], order='sequence')
    return [{'tipo': r['tipo'], 'atributo_id': (r['atributo_id'] or [0])[0], 'coincidencia': r['coincidencia']}
            for r in rows if r['activo']]


def tools(**ctx):
    return {t.name: t for t in create_odoo_tools(ctx)}


def ids_en(out):
    return [int(i) for i in re.findall(r'PRODUCT_ID:(\d+)', out)]


def todos_en_categorias(ids, categs):
    if not ids:
        return True
    n = call('product.product', 'search_count', [['id', 'in', ids], ['categ_id', 'child_of', categs]])
    return n == len(set(ids))


CATEGS_BOT2 = [15, 18, 17, 146, 4]
SITIO_MEGATONER, SITIO_BLIZKA = 1, 7
base = {'bot_id': 2, 'bot_pricelist_id': 9, 'bot_warehouse_id': 1, 'price_fallback': 'list_price',
        'busqueda_fuentes': fuentes(2)}

print("\nBot Usuario Final con categorías (cartuchos, tintas, cintas, impresoras)")
t = tools(**base, bot_categ_ids=CATEGS_BOT2)
for q, esperado in (('667', 'HP 667'), ('HP 667', 'HP 667'), ('150A', '150'),
                    ('HP DeskJet Ink Advantage 3635', 'HP')):
    out = t['buscar_producto'].invoke({'referencia': q})
    print(f"    {q!r} -> {out.splitlines()[0][:90]}")
    check(f"{q!r} encuentra producto", 'No encontré' not in out and 'No se encontró' not in out and esperado in out)
    check(f"{q!r}: nada de lo que dice a Blizka", 'Blizka' not in out and 'colchón' not in out.lower())
    check(f"{q!r}: todo está dentro de las categorías", todos_en_categorias(ids_en(out), CATEGS_BOT2))
out = t['buscar_referencias_mensaje'].invoke({'mensaje': 'necesito 667'})
check("buscar_referencias_mensaje ya no usa la lista de Blizka", 'colchón' not in out.lower() and 'Blizka' not in out)
out = t['buscar_referencias_mensaje'].invoke({'mensaje': '2 de 664'})
check("buscar_referencias_mensaje encuentra el 664 dentro de las categorías", 67 in ids_en(out), out.splitlines()[0][:90])
check("buscar_referencias_mensaje: todo dentro de las categorías", todos_en_categorias(ids_en(out), CATEGS_BOT2))

print("\nCandado: categorías que NO incluyen el producto")
tc = tools(**base, bot_categ_ids=[15])   # solo Cartucho Laser; el 667 es tinta
out = tc['buscar_producto'].invoke({'referencia': '667'})
check("el 667 (tinta) no aparece si el bot solo atiende láser", 2061 not in ids_en(out), out.splitlines()[0][:90])
check("y no manda a preguntar por colchones", 'colchón' not in out.lower())
out = tc['buscar_referencias_mensaje'].invoke({'mensaje': '2 de 664'})
check("buscar_referencias_mensaje respeta el candado: el 664 (tinta) no aparece con solo láser", 67 not in ids_en(out), out.splitlines()[0][:90])

print("\nControl: Blizka no cambia")
tb = tools(bot_id=7, bot_pricelist_id=512, bot_warehouse_id=1, bot_categ_ids=[222],
           bot_site_url='https://www.blizka.co')
out = tb['buscar_producto'].invoke({'referencia': 'colchones híbridos'})
check("encuentra colchones por nombre", 'Encontré' in out and 'Blizka' in out and 'No encontré' not in out)
out = tb['buscar_producto'].invoke({'referencia': '667'})
check("un cartucho NO existe en Blizka y se le dice que pregunte por colchón/almohada",
      'No encontré' in out and 'colchón' in out.lower())

print("\nControl: bot sin categorías no cambia")
tl = tools(**base)
out = tl['buscar_producto'].invoke({'referencia': '667'})
check("sin categorías sigue encontrando el 667", 'HP 667' in out and 'No encontré' not in out)

print(f"\n{len(ok)} OK / {len(fail)} FALLAS")
sys.exit(1 if fail else 0)
