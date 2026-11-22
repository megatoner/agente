"""Catálogo del bot por SITIO WEB (2026-11-22): una sola fuente de verdad con la tienda.

Con `bot_website_id` el bot solo busca/precifica/cotiza lo que se vende en ese sitio ("Sitio web" o
"Se vende en los sitios"), y lo SIN asignar a ningún sitio no entra (más estricto que la web).
Sin sitio, manda el respaldo por categorías, y sin ambos no hay restricción.

Solo lectura sobre producción:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_catalogo_por_sitio.py
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


def en_sitio(ids, sitio):
    if not ids:
        return True
    n = call('product.product', 'search_count', [['id', 'in', ids], '|', ['product_tmpl_id.website_id', '=', sitio],
                                                  ['product_tmpl_id.jpc_website_ids', 'in', [sitio]]])
    return n == len(set(ids))


base = {'bot_id': 2, 'bot_pricelist_id': 9, 'bot_warehouse_id': 1, 'price_fallback': 'list_price',
        'busqueda_fuentes': fuentes(2)}

print("\nBot Usuario Final por sitio 1 (Megatoner)")
t = tools(**base, bot_website_id=1)
for q, esperado in (('667', 'HP 667'), ('HP 667', 'HP 667'), ('150A', '150'), ('HP DeskJet Ink Advantage 3635', 'HP')):
    out = t['buscar_producto'].invoke({'referencia': q})
    print(f"    {q!r} -> {out.splitlines()[0][:90]}")
    check(f"{q!r} encuentra producto", 'No encontré' not in out and 'No se encontró' not in out and esperado in out)
    check(f"{q!r}: nada de Blizka/colchones", 'Blizka' not in out and 'colchón' not in out.lower())
    check(f"{q!r}: todo se vende en el sitio 1", en_sitio(ids_en(out), 1))
out = t['buscar_referencias_mensaje'].invoke({'mensaje': '2 de 664'})
check("buscar_referencias_mensaje: 664 dentro del sitio", 67 in ids_en(out) and en_sitio(ids_en(out), 1), out.splitlines()[0][:80])

print("\nEl sitio manda sobre las categorías")
tx = tools(**base, bot_website_id=1, bot_categ_ids=[15])   # categorías solo láser, pero el sitio 1 trae todo
out = tx['buscar_producto'].invoke({'referencia': '667'})
check("con sitio las categorías se ignoran (el 667 sí sale)", 'HP 667' in out and 'No encontré' not in out, out.splitlines()[0][:80])

print("\nCandado: lo que no se vende en el sitio no se precifica (obtener_precio)")
sin = call('product.product', 'search', [['product_tmpl_id.website_id', '=', False], ['product_tmpl_id.jpc_website_ids', '=', False],
                                         ['active', '=', True], ['sale_ok', '=', True]], limit=1)
de_blizka = call('product.product', 'search', [['product_tmpl_id.website_id', '=', 7], ['active', '=', True]], limit=1)
# Reutiliza una ficha existente (el usuario de integración no puede borrar contactos y una regla de
# duplicados impide crear otra con el mismo nombre): la prueba es de solo lectura, no la modifica.
cli = call('res.partner', 'search', [['name', '=', 'E2E CATALOGO RESTRINGIDO']], limit=1)
if not cli:
    cli = call('res.partner', 'search', [['name', '=', 'E2E CATALOGO RESTRINGIDO'], ['active', '=', False]], limit=1)
if not cli:
    cli = [call('res.partner', 'create', {'name': 'E2E CATALOGO RESTRINGIDO', 'company_type': 'company'})]
tb = tools(**base, bot_website_id=1)
if de_blizka:
    out = tb['obtener_precio'].invoke({'product_id': de_blizka[0], 'partner_id': cli[0]})
    check("un producto de Blizka NO se precifica en el bot del sitio 1", 'no pertenece al catálogo' in out, out[:80])
else:
    print("  (no hay productos de Blizka)")
if sin:
    out = tb['obtener_precio'].invoke({'product_id': sin[0], 'partner_id': cli[0]})
    check("un producto SIN sitio asignado tampoco (más estricto que la web)", 'no pertenece al catálogo' in out, out[:80])
else:
    print("  (no hay productos activos y vendibles sin sitio asignado: nada que probar)")
ok_id = call('product.product', 'search', [['id', '=', 2061]])
out = tb['obtener_precio'].invoke({'product_id': 2061, 'partner_id': cli[0]})
check("el HP 667 (sitio 1) sí se precifica", 'no pertenece al catálogo' not in out, out[:80])

print("\nControl: Blizka por sitio 7")
tb7 = tools(bot_id=7, bot_pricelist_id=512, bot_warehouse_id=1, bot_website_id=7, bot_site_url='https://www.blizka.co')
out = tb7['buscar_producto'].invoke({'referencia': 'colchones híbridos'})
check("encuentra colchones", 'Encontré' in out and 'Blizka' in out and 'No encontré' not in out, out.splitlines()[0][:80])
out = tb7['buscar_producto'].invoke({'referencia': '667'})
check("un cartucho no existe en Blizka", 'No encontré' in out)
check("Blizka sigue en modo catálogo simple (sin términos)", 'colchón' in out.lower())

print("\nControl: sin sitio y sin categorías no cambia; respaldo por categorías sigue")
out = tools(**base)['buscar_producto'].invoke({'referencia': '667'})
check("sin restricción encuentra el 667", 'HP 667' in out)
out = tools(**base, bot_categ_ids=[15, 18, 17, 146, 4])['buscar_producto'].invoke({'referencia': '667'})
check("respaldo por categorías (sin sitio) encuentra el 667", 'HP 667' in out and 'No encontré' not in out)

print(f"\n{len(ok)} OK / {len(fail)} FALLAS")
sys.exit(1 if fail else 0)
