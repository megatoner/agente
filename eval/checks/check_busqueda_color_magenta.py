"""'magenta' + código no debe perder el producto — ni en buscar_producto ni en
buscar_referencias_mensaje.

Caso real IST INGENIERIA (573165803563, canal 3228, 2026-09-30): pidió "tóner
magenta" para su M475dw, MIA ofreció Amarillo y Cian (ningún magenta), y 7
minutos después un asesor tuvo que enviarle el producto correcto a mano.

Dos causas combinadas:
1. El término de búsqueda exacto 'CE413A' (código propio del magenta de esta
   familia) existía en catálogo pero no estaba vinculado a NINGÚN producto —
   dato roto, ya corregido — mientras que 'CE412A'/'CE411A'/'CE410A' (los
   otros 3 colores) sí estaban bien vinculados.
2. Por eso la búsqueda caía a la Fase 3 (atributo/SKU), que hace ilike con la
   CADENA COMPLETA — y 'magenta' sin limpiar nunca es substring de
   'CC533A/CE413A/CF383A', así que ni con el código exacto encontraba nada.
   Los otros colores nunca llegaban a esa fase (su Fase 1 ya los resolvía),
   así que el bug solo se notaba con magenta — pero cualquier producto futuro
   con el mismo tipo de dato roto habría tenido el mismo problema.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_busqueda_color_magenta.py
"""
import sys

sys.path.insert(0, '/opt/odoo-agents')
from app.odoo.tools import create_odoo_tools  # noqa: E402

tools = {t.name: t for t in create_odoo_tools({'partner_id': None, 'bot_id': 4})}
ok, fail = [], []


def check(n, c, d=''):
    (ok if c else fail).append(n)
    print(f"  [{'OK ' if c else 'FALLA'}] {n}{(' — ' + d) if d else ''}")


casos = [
    ('toner magenta HP 305A CE413A M475dw', 'buscar_producto'),
    ('magenta CE413A', 'buscar_producto'),
    ('magenta CF383A', 'buscar_producto'),
    ('magenta CE413A', 'buscar_referencias_mensaje'),
]
for query, tool_name in casos:
    out = tools[tool_name].invoke(
        {'referencia': query} if tool_name == 'buscar_producto' else {'mensaje': query})
    check(f"{tool_name}({query!r}) encuentra el magenta (ID:2543 / PRODUCT_ID:2543)",
          'ID:2543' in out or 'PRODUCT_ID:2543' in out, out.splitlines()[0][:70])

# Control negativo: los otros 3 colores de la misma familia siguen funcionando
for query, esperado in [('amarillo CE412A', '2542'), ('cian CE411A', '2541'), ('negro CE410A', '2544')]:
    out = tools['buscar_producto'].invoke({'referencia': query})
    check(f"'{query}' sigue encontrando su color (sin regresión)", f'ID:{esperado}' in out)

print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
