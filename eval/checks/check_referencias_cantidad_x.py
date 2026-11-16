"""«x2» / «x3» es una CANTIDAD, no una referencia de producto (buscar_referencias_mensaje).

Defecto: `REF_PAT` acepta «letra + dígitos», así que «664 cian megatoner x2» sacaba la referencia «X2»:
devolvía `[PRODUCTO_NO_ENCONTRADO: X2]` con la instrucción de pasar el chat a un asesor, la cantidad se
quedaba en 1 (QTY_PAT solo reconocía números sueltos) y el mensaje contaba como DOS referencias, lo que
además desactivaba el filtro por color/línea (check_busqueda_intencion.py).

Reglas que fija (y por qué):
  · «xN» (1-3 dígitos, con o sin espacio: «x2», «X3», «x 10») es la cantidad de la referencia ANTERIOR;
  · solo si ya hay una referencia antes: existe un término real «X10», así que «x10» a secas sigue siendo
    una referencia;
  · «Nx» NO se toca: «83X», «05X», «10X»… son referencias reales (hay decenas como término).

Catálogo real, SOLO lectura.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_referencias_cantidad_x.py
"""
import re
import sys

sys.path.insert(0, '/opt/odoo-agents')
from app.odoo import client as _cl  # noqa: E402
from app.odoo.tools import create_odoo_tools  # noqa: E402

LECTURA = {'search_read', 'read', 'search', 'search_count', 'fields_get', 'name_search', 'read_group'}
_orig = _cl.OdooClient.execute_kw


def _espia(self, model, method, args=None, kwargs=None):
    return _orig(self, model, method, args, kwargs) if method in LECTURA else True


_cl.OdooClient.execute_kw = _espia
ok, fail = [], []


def check(n, c, d=''):
    (ok if c else fail).append(n)
    print(f"  [{'OK ' if c else 'FALLA'}] {n}{(' — ' + d) if d else ''}")


def f(tipo, atributo=0, modo='exacta', imp=False):
    return {'tipo': tipo, 'atributo_id': atributo, 'coincidencia': modo, 'es_impresora': imp}


LISTA = [f('codigo'), f('exacto'), f('atributo', 48, 'contiene'), f('similar', modo='contiene'),
         f('atributo', 14, 'contiene', True), f('atributo', 13, 'contiene')]
H = {t.name: t for t in create_odoo_tools({'partner_id': None, 'bot_id': 4, 'busqueda_fuentes': LISTA})}


def brm(m):
    return ' '.join(str(H['buscar_referencias_mensaje'].invoke({'mensaje': m})).split())


def lineas(r):
    return re.findall(r'PRODUCT_ID:\d+ \|[^|]*(?:\|[^|]*)*?\| Qty:(\d+)', r)


NF = 'PRODUCTO_NO_ENCONTRADO'
print("== xN es la cantidad de la referencia anterior")
r = brm('664 cian megatoner x2')
check("«664 cian megatoner x2»: cantidad 2, sin «X2» no encontrado", lineas(r) == ['2'] and NF not in r, r[:150])
check("…y como ya es UNA sola referencia, deja la Cyan Megatoner", 'Cyan' in r.split('Qty')[0] and 'Megatoner' in r.split('Qty')[0], r[:150])
r = brm('quiero GT52 amarillo x3')
check("«quiero GT52 amarillo x3»: cantidad 3", lineas(r) == ['3'] and NF not in r, r[:150])
r = brm('664 X2')
check("mayúscula: «664 X2» → cantidad 2", lineas(r) == ['2'] and NF not in r, r[:120])
r = brm('664 x 2')
check("con espacio: «664 x 2» → cantidad 2", lineas(r) == ['2'] and NF not in r, r[:120])
r = brm('664 x10')
check("tres dígitos admitidos: «664 x10» → cantidad 10 (y no la referencia X10)", lineas(r) == ['10'] and NF not in r, r[:150])
r = brm('664 x2 y GT52 x3')
check("dos referencias, cada una con su cantidad: 664 → 2 y GT52 → 3", lineas(r) == ['2', '3'] and NF not in r, r[:200])

print("== Lo que NO debe cambiar")
r = brm('664 2')
check("número suelto sigue siendo cantidad: «664 2» → 2", lineas(r) == ['2'] and NF not in r, r[:100])
r = brm('necesito 83X')
check("«83X» sigue siendo una REFERENCIA (no una cantidad): trae el tóner 83X", 'PRODUCT_ID' in r and '83X' in r, r[:120])
r = brm('x10')
check("«x10» a secas sigue siendo una referencia (existe el término X10): no dice «no detecté referencias»",
      'No detecte referencias' not in r, r[:140])
r = brm('664')
check("sin cantidad: cantidad 1", lineas(r) == ['1'], r[:100])

print(f"\n{len(ok)} OK, {len(fail)} FALLA")
sys.exit(1 if fail else 0)
