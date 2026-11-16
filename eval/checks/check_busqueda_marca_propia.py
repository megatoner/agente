"""«664 megatoner»: la marca propia no es una marca de impresora.

Caso real (54 filas en «Términos no encontrados», todas del bot Usuario Final): el cliente pide
«664 megatoner», «T664120 megatoner», «664 magenta megatoner»… y el agente respondía «No encontré» y
pasaba el chat a un asesor, aunque «664» y «epson 664» sí encontraban las 10 opciones.

Causa: «Megatoner» está dada de alta como marca OEM (`jpc.product.brand.oem`, solo 3 productos) y
`_get_marca_filter` la tomaba por la marca de la impresora: restringía el catálogo a esos 3 productos
y se perdía todo lo demás. Las tintas compatibles tienen marca OEM Epson/HP y solo su marca comercial
(`jpc_marca`) es Megatoner. «Megatoner» es el nombre de la casa: ruido, como «original» o «compatible».

Qué verifica (contra el catálogo real, SOLO lectura: toda llamada que no sea de lectura se bloquea):
  · «664 megatoner» (y variantes) encuentra lo mismo que «664»;
  · el código con la marca propia («T664120 megatoner») encuentra su producto;
  · las marcas de verdad siguen filtrando («hp 664» da solo los HP, «epson 664» solo los Epson);
  · «megatoner» a secas no revienta.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_busqueda_marca_propia.py
"""
import re
import sys

sys.path.insert(0, '/opt/odoo-agents')
from app.odoo import client as _cl  # noqa: E402
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


BASE = [f('exacto'), f('similar', modo='contiene'), f('atributo', 14, 'contiene', True), f('atributo', 13, 'contiene')]
H = {t.name: t for t in create_odoo_tools({'partner_id': None, 'bot_id': 2, 'busqueda_fuentes': BASE})}
NO = 'No encontre'


def bp(q):
    return ' '.join(str(H['buscar_producto'].invoke({'referencia': q})).split())


def opciones(texto):
    m = re.search(r'en (\d+) (?:opciones|colores|versiones)', texto)
    return int(m.group(1)) if m else (1 if texto and NO not in texto else 0)


print("== La marca propia no filtra el catálogo")
base = bp('664')
check("control: «664» encuentra opciones", NO not in base and opciones(base) >= 8, base[:90])
for q in ('664 megatoner', 'megatoner 664', '664 cian megatoner', 'tinta 664 megatoner',
          '664 megatoner T664120', '664 megatoner epson botella'):
    r = bp(q)
    check(f"«{q}» encuentra", NO not in r and '664' in r, r[:110])
# Desde que el agente respeta la línea pedida (check_busqueda_intencion.py), «megatoner» deja solo los
# compatibles: las 4 botellas Megatoner, sin los Original ni los HP.
r4 = bp('664 megatoner')
check("«664 megatoner» da solo los 4 compatibles Megatoner", opciones(r4) == 4 and 'Originales' not in r4, r4[:110])
r = bp('T664120 megatoner')
check("«T664120 megatoner» encuentra la botella negra", NO not in r and 'T664120' in r, r[:110])

print("== Las marcas de verdad siguen filtrando")
r_hp, r_ep = bp('hp 664'), bp('epson 664')
# Los HP 664 son solo Negro y Tricolor; los Epson 664 son T664xxx en cuatro colores.
check("«hp 664» solo trae HP (Negro/Tricolor, sin los T664 de Epson)",
      NO not in r_hp and 'Tricolor' in r_hp and 'T664' not in r_hp and 'Cian' not in r_hp, r_hp[:110])
check("«epson 664» solo trae Epson (T664, sin los HP Tricolor)",
      NO not in r_ep and 'T664' in r_ep and 'Tricolor' not in r_ep, r_ep[:110])

print("== «megatoner» a secas no revienta")
try:
    r = bp('megatoner')
    check("«megatoner» responde algo (aunque sea «no encontré»)", isinstance(r, str) and len(r) > 0, r[:90])
except Exception as e:  # noqa: BLE001
    check("«megatoner» no lanza excepción", False, repr(e))

print("== Solo lectura")
# La única llamada que no es de lectura es el aviso interno de «respuesta de producto» (se bloquea).
check("ninguna escritura salvo la consulta interna bloqueada",
      all(met == 'jwb_obtener_respuesta_producto' for _, met in bloqueadas), str(sorted(set(bloqueadas))))

print(f"\n{len(ok)} OK, {len(fail)} FALLA")
sys.exit(1 if fail else 0)
