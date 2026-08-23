"""Ficha de producto en UN mensaje, con la config de cada bot.

Antes, buscar un producto disparaba una tarjeta desde el CÓDIGO, antes de que
el modelo hubiera decidido si el producto era el correcto. El cliente recibía
dos mensajes y el segundo podía contradecir al primero (caso Hernan
2026-08-23: tarjeta del Tóner HP 12A seguida de "tu DeskJet usa HP 61").

Ahora la búsqueda devuelve una ficha en texto y decidir enviarla es del modelo.
Lo que se verifica:
  - la ficha llega armada y no se envía nada por su cuenta,
  - cada bot muestra SUS precios (dos listas vs unidad/caja),
  - lleva rendimiento, chip y el círculo de color,
  - el enlace es el corto (/shop/<id>), no el slug de 150 caracteres.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_ficha_producto.py
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


ok, fail = [], []


def check(nombre, cond, detalle=''):
    (ok if cond else fail).append(nombre)
    print(f"  [{'OK ' if cond else 'FALLA'}] {nombre}{(' — ' + detalle) if detalle else ''}")


def buscar(bot_id, ch_id, ref):
    bot = call('jpc.whatsapp.bot.config', 'read', [bot_id],
               fields=['default_pricelist_id', 'warehouse_id', 'price_fallback'])[0]
    partner = call('res.partner', 'search', [['is_company', '=', True]], limit=1)[0]
    tools = {t.name: t for t in create_odoo_tools({
        'partner_id': partner, 'channel_id': ch_id,
        'bot_pricelist_id': bot['default_pricelist_id'][0],
        'bot_warehouse_id': bot['warehouse_id'][0] if bot['warehouse_id'] else 0,
        'price_fallback': bot['price_fallback'],
    })}
    return tools['buscar_producto'].invoke({'referencia': ref})


# Cuántas tarjetas se han enviado hasta ahora: no debe subir.
def tarjetas_enviadas():
    return call('mail.message', 'search_count',
                [['model', '=', 'discuss.channel'], ['author_id.name', '=', 'OdooBot'],
                 ['message_type', '=', 'comment']])


antes = tarjetas_enviadas()

print("\n1) Usuario Final — dos listas de precio distintas")
uf = buscar(2, 4709, '12A')
check("devuelve la ficha lista para enviar", 'FICHA LISTA PARA ENVIAR' in uf)
check("trae los dos precios de sus listas", '$47.300' in uf and '$42.570' in uf)
check("trae rendimiento y chip", 'Páginas' in uf and 'chip' in uf.lower())
check("trae el círculo de color", '⚫' in uf or '🔵' in uf or '🟡' in uf or '🔴' in uf)
check("usa el enlace corto", '/shop/1800' in uf)
check("no usa el slug largo", 'rendimiento-2000-paginas-1800' not in uf.split('FICHA')[1])
check("pide un solo mensaje", 'UN SOLO MENSAJE' in uf)

print("\n2) Distribuidor — misma lista, unidad y caja")
di = buscar(4, 3153, '12A')
check("precio por unidad y por caja", 'c/u' in di and 'desde 20 und' in di)
check("son SUS precios, no los del otro bot",
      '$21.600' in di and '$47.300' not in di.split('FICHA')[1])

print("\n3) No se envía nada por cuenta propia")
check("no se enviaron tarjetas nuevas", tarjetas_enviadas() == antes,
      f"{antes} antes, {tarjetas_enviadas()} después")

print("\n4) Sigue pudiendo negarse a enviar")
# El bloque debe traer la instrucción de NO enviarlo si el producto no encaja:
# es lo que faltaba el 23-ago y produjo la tarjeta contradictoria.
check("instruye no enviar si el producto no corresponde",
      'NO envíes el bloque' in uf)

print(f"\n{'='*58}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*58}\n")
sys.exit(1 if fail else 0)
