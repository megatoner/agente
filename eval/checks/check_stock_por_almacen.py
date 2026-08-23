"""Comprueba que el stock se filtra por el almacén de CADA bot.

Odoo 19 filtra qty_available con context['warehouse_id']; la clave 'warehouse'
se ignora en silencio y devuelve el stock sumado de todos los almacenes. Eso hizo
que el bot Distribuidor ofreciera 7 tintas 544 agotadas en su almacén porque
existían en el de Usuario Final (caso MEGATINTAS, 2026-08-22).

Dos condiciones, y las dos importan:
  - cada bot ve el stock de SU almacén,
  - los dos almacenes dan resultados DISTINTOS para el mismo producto — si
    coincidieran, el filtro podría estar sin aplicarse y la prueba pasaría igual.

Ejecutar:
    cd /opt/odoo-agents && python3 eval/checks/check_stock_por_almacen.py
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

ok, fail = [], []


def check(nombre, cond, detalle=''):
    (ok if cond else fail).append(nombre)
    print(f"  [{'OK ' if cond else 'FALLA'}] {nombre}{(' — ' + detalle) if detalle else ''}")


def stock(bot_id):
    """Lee el stock igual que tools.py: contexto con el warehouse_id del bot."""
    bot = models.execute_kw(db, uid, pwd, 'jpc.whatsapp.bot.config', 'read',
                            [[bot_id]], {'fields': ['name', 'warehouse_id'],
                                         'context': {'active_test': False}})[0]
    wh = bot['warehouse_id']
    wh_id = wh[0] if isinstance(wh, (list, tuple)) else 0
    ctx = {'warehouse_id': wh_id} if wh_id else {}
    kwargs = {'fields': ['default_code', 'qty_available'], 'limit': 30}
    if ctx:
        kwargs['context'] = ctx
    filas = models.execute_kw(db, uid, pwd, 'product.product', 'search_read',
                              [[['product_tmpl_id', 'in', TMPLS],
                                ['active', '=', True]]], kwargs)
    return bot['name'], (wh[1] if isinstance(wh, (list, tuple)) else None), \
        {f['default_code']: f.get('qty_available') or 0 for f in filas}


TMPLS = [1839, 1841, 1843, 1845]  # tintas Epson 544 compatibles
COLORES = (('225006', 'cian'), ('225007', 'magenta'), ('225008', 'amarillo'))

print("\n1) Cada bot tiene su almacén configurado")
datos = {}
for bot_id in (2, 4):
    nombre, almacen, qty = stock(bot_id)
    datos[bot_id] = qty
    check(f"bot {bot_id} ({nombre}) con almacén", bool(almacen), almacen or 'SIN ALMACÉN')
    print(f"        " + ", ".join(f"{k}={v:.0f}" for k, v in sorted(qty.items())))

print("\n2) Distribuidor: negro disponible, colores agotados")
check("el negro está disponible", datos[4].get('225005', 0) > 0,
      f"{datos[4].get('225005', 0):.0f} unidades")
for cod, color in COLORES:
    check(f"el {color} se ve agotado", datos[4].get(cod, -1) == 0)

print("\n3) Usuario Final ve SU almacén, no el del Distribuidor")
# Si los dos bots vieran lo mismo, el filtro podría no estar aplicándose.
distintos = [c for c, _ in COLORES if datos[2].get(c, -1) != datos[4].get(c, -1)]
check("los dos almacenes dan cifras distintas", bool(distintos),
      f"difieren en {len(distintos)} de {len(COLORES)} colores")
for cod, color in COLORES:
    check(f"el {color} SÍ está en Usuario Final", datos[2].get(cod, 0) > 0,
          f"{datos[2].get(cod, 0):.0f} unidades")

print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
