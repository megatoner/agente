"""«CRG-137», «CRG 137», «CE-413A»: el prefijo de cartucho unido a su número (buscar_referencias_mensaje).

Defecto: `REF_PAT` parte el texto en el guion/espacio y se quedaba SOLO con el número. «CRG-137» era «137» y
resolvía, con ✅ y dejándolo en el carrito, al Tóner Kyocera TK-137. Medido con 59 términos reales del catálogo
(prefijos CRG, TK, TN, DR, CF, CE, CB): con guion la herramienta devolvía el MISMO producto que el código escrito
normal solo en 30 de 48 casos (63 %), y con espacio en 10 de 16; ahora 48 de 48 y 16 de 16, sin regresiones.

Reglas (y por qué):
  · con espacio o guion: solo CRG y TK; con guion: TN, DR, CF, CE, CB;
  · NUNCA CC ni LC, ni con espacio los demás: las direcciones colombianas traen «LC 11» (local) y «CC 85»
    (centro comercial); unirlas inventaba referencias («PRODUCTO_NO_ENCONTRADO: CC85») en mensajes con una
    dirección (caso real: 2 mensajes del historial);
  · siempre seguido de 2+ dígitos: «HP-85A» y «MLT-D203L» no se tocan.

Catálogo real, SOLO lectura.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_referencias_prefijo_guion.py
"""
import re
import sys

sys.path.insert(0, '/opt/odoo-agents')
from app.odoo import client as _cl  # noqa: E402
from app.odoo import tools as T  # noqa: E402
from app.odoo.tools import create_odoo_tools  # noqa: E402

LECTURA = {'search_read', 'read', 'search', 'search_count', 'fields_get', 'name_search', 'read_group'}
_orig = _cl.OdooClient.execute_kw
_cl.OdooClient.execute_kw = lambda self, m, me, a=None, k=None: _orig(self, m, me, a, k) if me in LECTURA else True
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


def ids(r):
    return re.findall(r'PRODUCT_ID:(\d+)', r)


unir = lambda t: T._RE_PREFIJO_CARTUCHO.sub(lambda m: m.group(1) or m.group(2), t)

print("== Función pura: qué se une y qué NO")
for t, esperado in (("quiero 2 toner canon CRG-137", "CRG137"), ("CRG 137", "CRG137"), ("TK 137", "TK137"), ("tk-5242c", "tk5242c"),
                    ("CF-283X x2", "CF283X"), ("TN-760", "TN760"), ("DR-730", "DR730"), ("CE-413A", "CE413A"), ("CB-435A", "CB435A")):
    check(f"«{t}» se une", esperado in unir(t), unir(t))
for t in ("HP-85A", "MLT-D203L", "CL 77 CC 85 B 48 - ROBLEDO", "edificio central LC 11", "LC-3017", "CC-533A", "CE 505X", "TN 760", "DR 730", "CF 283X"):
    check(f"«{t}» NO se toca", unir(t) == t, unir(t))

print("== buscar_referencias_mensaje: el código con guion/espacio resuelve IGUAL que escrito normal")
for normal, variantes in (('CE413A', ('CE-413A',)), ('CF283X', ('CF-283X',)), ('CB435A', ('CB-435A',)), ('CE278A', ('CE-278A',))):
    ref = ids(brm('necesito ' + normal))
    for v in variantes:
        check(f"«{v}» = «{normal}»", bool(ref) and ids(brm('necesito ' + v)) == ref, f"{ids(brm('necesito ' + v))} vs {ref}")
r = brm('necesito CF-283X x2')
check("«CF-283X x2»: cantidad 2 y mismo producto que CF283X", re.findall(r'Qty:(\d+)', r) == ['2'] and ids(r) == ids(brm('necesito CF283X')), r[:120])

print("== CRG-137 / CRG 137 → el CF283X Megatoner (1811), NO el Kyocera (datos deliberados del 2026-11-16)")
for m in ('quiero 2 toner canon CRG-137', 'necesito CRG 137', 'canon CRG-137 x2'):
    r = brm(m)
    check(f"«{m}»", 'HP 83X' in r and 'Kyocera' not in r and 'PRODUCTO_NO_ENCONTRADO' not in r, r[:130])
r = brm('necesito TK 137')
check("«TK 137» (Kyocera de verdad) sigue dando el Kyocera TK-137", 'Kyocera' in r, r[:110])

print("== Direcciones: no se inventan referencias (mensajes reales del historial)")
for m in ("el total con el domicilio seria de Total $ 323.000 te lo enviamos a esta dirección CL 51 49 88 ED edificio central LC 11",
          "CL 77 CC 85 B 48 - ROBLEDO BELLO HORIZONTE, MEDELLIN Casa primer piso, es en una esquina"):
    r = brm(m)
    check("sin «CC85» ni «LC118» inventados", 'CC85' not in r and 'LC118' not in r and 'LC11' not in r, r[:110])

print(f"\n{len(ok)} OK, {len(fail)} FALLA")
sys.exit(1 if fail else 0)
