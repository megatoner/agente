"""«GT52 amarillo megatoner» debe mostrar UN producto: la botella amarilla compatible.

Caso real (canal 2742, 30-jul): el cliente pidió tintas para una HP GT 5810. La búsqueda
«GT52 amarillo megatoner» fallaba (marca propia tomada por marca de impresora; ver
check_busqueda_marca_propia.py). Corregido eso, seguía devolviendo las 6 opciones de GT52 (3 colores ×
Megatoner/Original) porque el color y «megatoner» se descartaban como ruido, y
`buscar_referencias_mensaje` elegía el producto MÁS VENDIDO aunque fuera de otro color («664 cian
megatoner x2» dejaba en el carrito la tinta Magenta Original).

Qué verifica (contra el catálogo real, SOLO lectura):
  · color + línea nombrados → exactamente ese producto («GT52 amarillo megatoner», cian, magenta);
  · solo color → las dos líneas de ese color; solo línea → los tres colores de esa línea;
  · plurales y tildes («amarillas», «cían»);
  · nunca se vacía la lista: un color o una línea sin coincidencia deja todo como estaba;
  · `buscar_referencias_mensaje` con una sola referencia elige el color pedido, y con varias
    referencias no cambia (no se sabe a cuál pertenece cada color);
  · las funciones puras (`_colores_en`, `_linea_pedida`, `_estrechar_por_intencion`).

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_busqueda_intencion.py
"""
import re
import sys

sys.path.insert(0, '/opt/odoo-agents')
from app.odoo import client as _cl  # noqa: E402
from app.odoo import tools as T  # noqa: E402
from app.odoo.tools import create_odoo_tools  # noqa: E402

LECTURA = {'search_read', 'read', 'search', 'search_count', 'fields_get', 'name_search', 'read_group'}
bloqueadas = []
_orig = _cl.OdooClient.execute_kw


def _espia(self, model, method, args=None, kwargs=None):
    if method not in LECTURA:
        bloqueadas.append((model, method))
        return True
    return _orig(self, model, method, args, kwargs)


_cl.OdooClient.execute_kw = _espia

ok, fail = [], []


def check(n, c, d=''):
    (ok if c else fail).append(n)
    print(f"  [{'OK ' if c else 'FALLA'}] {n}{(' — ' + d) if d else ''}")


def f(tipo, atributo=0, modo='exacta', imp=False):
    return {'tipo': tipo, 'atributo_id': atributo, 'coincidencia': modo, 'es_impresora': imp}


# Lista real del bot «Megatoner Usuario Final».
LISTA = [f('codigo'), f('exacto'), f('atributo', 48, 'contiene'), f('similar', modo='contiene'),
         f('atributo', 14, 'contiene', True), f('atributo', 13, 'contiene')]
H = {t.name: t for t in create_odoo_tools({'partner_id': None, 'bot_id': 4, 'busqueda_fuentes': LISTA})}
NO = 'No encontre'


def bp(q):
    return ' '.join(str(H['buscar_producto'].invoke({'referencia': q})).split())


def n_opciones(txt):
    m = re.search(r'en (\d+) (?:opciones|colores|versiones)', txt)
    if m:
        return int(m.group(1))
    m = re.search(r'Encontr[eé] (\d+) producto', txt)
    return int(m.group(1)) if m else (0 if NO in txt else -1)


print("== Funciones puras")
check("_colores_en: amarillas/cían/negros/azul", T._colores_en('2 amarillas y cían, negros, azul') == {'amarillo', 'cian', 'negro'})
check("_colores_en: «rojo» y «red» NO se interpretan", T._colores_en('rojo red') == set())
check("_colores_en: «Color: Yellow - Amarillo» (atributo del producto)", T._colores_en('Color: Yellow - Amarillo') == {'amarillo'})
check("_linea_pedida megatoner/compatible/genérico", all(T._linea_pedida(x) == 'compatible' for x in ('x megatoner', 'compatible', 'genérico')))
check("_linea_pedida: «compatible con mi HP» / «compatible para…» NO es la línea genérica",
      all(T._linea_pedida(x) is None for x in ('tinta compatible con mi hp gt 5810', 'compatible para la smart tank', 'es compatible a mi equipo')))
check("_linea_pedida: «tinta compatible» a secas sí", T._linea_pedida('tinta compatible gt52') == 'compatible')
check("_linea_pedida original", T._linea_pedida('el original') == 'original')
check("_linea_pedida ambas o ninguna → None", T._linea_pedida('original o megatoner') is None and T._linea_pedida('GT52') is None)
mini = [{'name': 'A', 'color_attr': 'Color: Cian - Cyan', 'jpc_marca': 'Megatoner'},
        {'name': 'B', 'color_attr': 'Color: Cian - Cyan', 'jpc_marca': 'HP'},
        {'name': 'C', 'color_attr': 'Color: Magenta', 'jpc_marca': 'Megatoner'}]
check("estrechar: color + línea deja uno", [p['name'] for p in T._estrechar_por_intencion(mini, 'cian megatoner')] == ['A'])
check("estrechar: color sin coincidencia no vacía la lista", len(T._estrechar_por_intencion(mini, 'amarillo')) == 3)
check("estrechar: línea sin coincidencia no vacía la lista",
      len(T._estrechar_por_intencion([p for p in mini if p['jpc_marca'] != 'HP'], 'original')) == 2)
check("estrechar: un color sin coincidencia no anula la línea pedida",
      [p['name'] for p in T._estrechar_por_intencion(mini, 'amarillo original')] == ['B'])
check("estrechar: un solo producto no se toca", len(T._estrechar_por_intencion(mini[:1], 'magenta original')) == 1)

print("== buscar_producto: color + línea → ese producto")
base = bp('GT52')
check("control: «GT52» da las 6 opciones", n_opciones(base) == 6, base[:80])
for q, color in (('GT52 amarillo megatoner', 'Amarillo'), ('GT52 cian megatoner', 'Cian'), ('GT52 magenta megatoner', 'Magenta'),
                 ('tinta GT52 amarilla megatoner', 'Amarillo'), ('GT52 cían megatoner', 'Cian')):
    r = bp(q)
    esperado = 'Cyan' if color == 'Cian' else color      # el nombre del producto dice «Cyan»
    check(f"«{q}» → 1 producto, {color}, Megatoner",
          n_opciones(r) == 1 and esperado in r and 'Megatoner' in r and 'Original' not in r, r[:150])
r = bp('GT52 amarillo megatoner')
check("«GT52 amarillo megatoner» es la botella Megatoner amarilla (ID 1893), no el original (M0H56AL)",
      'GT52 Amarillo | Megatoner' in r and 'ID:1893' in r and 'M0H56AL' not in r, r[:200])

print("== Solo color o solo línea")
r = bp('GT52 amarillo')
check("«GT52 amarillo» → 2 (Megatoner y Original)", n_opciones(r) == 2, r[:120])
r = bp('GT52 megatoner')
check("«GT52 megatoner» → 3 (los tres compatibles)", n_opciones(r) == 3 and 'Original' not in r.split('Originales')[0] and 'Originales' not in r, r[:140])
r = bp('GT52 original')
check("«GT52 original» → 3 (los tres originales)", n_opciones(r) == 3 and 'Compatible Megatoner' not in r, r[:140])
r = bp('GT52 amarillas original')
check("«GT52 amarillas original» → 1 (el original amarillo)", n_opciones(r) == 1 and 'Original' in r and 'Amarillo' in r, r[:140])

print("== Nunca se vacía la lista")
r = bp('GT52 verde')
check("«GT52 verde» (color que no existe ni se interpreta) → las 6", n_opciones(r) == 6, r[:100])
r = bp('GT52 negro megatoner')
check("«GT52 negro megatoner»: no hay negro GT52, pero sí compatibles → los 3 Megatoner", n_opciones(r) == 3 and 'Original' not in r, r[:120])

r = bp('tinta compatible con mi hp smart tank GT52')
check("«compatible con mi HP» no oculta los originales (siguen las 6 opciones)", n_opciones(r) == 6, r[:110])

print("== Códigos propios por color de GT52 (términos GT52M / GT52Y cargados el 2026-11-16; GT52C ya existía)")
# Los compatibles GT52 tienen código propio por color (Modelo M-GT52C, M-GT52M, GT52Y) y solo el cian tenía su
# término exacto. Es un dato deliberado: si alguien lo borra, estas aserciones fallan a propósito.
for q, color in (('GT52C', 'Cyan'), ('GT52M', 'Magenta'), ('GT52Y', 'Amarillo'),
                 ('GT52M magenta megatoner', 'Magenta'), ('GT52Y amarillo megatoner', 'Amarillo')):
    r = bp(q)
    check(f"«{q}» → 1 producto, {color}, Megatoner",
          n_opciones(r) == 1 and color in r and 'Megatoner' in r and 'Original' not in r, r[:140])

print("== Marcas de verdad y 664 (regresión)")
r = bp('664 negra')
check("«664 negra» → solo negros", n_opciones(r) >= 1 and all(c not in r for c in ('Cian', 'Amarillo', 'Magenta', 'Tricolor')) and 'Negro' in r, r[:160])
r = bp('hp 664')
check("«hp 664» sigue trayendo solo HP", NO not in r and 'T664' not in r, r[:100])

print("== buscar_referencias_mensaje")
def brm(m):
    return ' '.join(str(H['buscar_referencias_mensaje'].invoke({'mensaje': m})).split())
r = brm('quiero 2 de 664 cian megatoner')
linea = r.split('Qty')[0]
check("«664 cian megatoner» deja la Cyan Megatoner (antes: la Magenta Original)",
      'Cyan' in linea and 'Megatoner' in linea and 'Magenta' not in linea, r[:170])
r = brm('664 amarilla original x3')  # «x3» es cantidad (check_referencias_cantidad_x.py), ya no una 2.ª referencia
linea = r.split('Qty')[0]
check("«664 amarilla original» deja la amarilla Original",
      ('Amarillo' in linea or 'Yellow' in linea) and 'Original' in linea and 'Megatoner' not in linea, r[:170])
r = brm('664')
check("sin color ni línea el comportamiento es el de siempre (elige una, ✅)", r.startswith('PRODUCT_ID:') and '✅' in r, r[:120])

print("== Solo lectura")
check("ninguna escritura salvo la consulta interna bloqueada",
      all(met in ('jwb_obtener_respuesta_producto',) or met.startswith('_upsert') or met in ('enviar_aviso_busqueda_similar',)
          for _, met in bloqueadas), str(sorted(set(bloqueadas))))
print(f"\n{len(ok)} OK, {len(fail)} FALLA")
sys.exit(1 if fail else 0)
