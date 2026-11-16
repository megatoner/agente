"""Fuentes de búsqueda configurables por bot («Dónde busca productos el bot», pestaña Ventas).

Caso 121070 (clientes que piden por el CÓDIGO INTERNO que ven en sus cotizaciones): ninguna fase
buscaba por `default_code`, aunque hay 415 variantes publicadas con código. Y la lista de «Términos no
encontrados» se llenaba de productos que SÍ existen (caso «1020»: el término de similitud existe desde
mayo y MIA le contestó bien al cliente, pero se registró como no encontrado) porque el registro ocurría
ANTES de terminar de buscar.

Qué verifica (contra el catálogo real, SOLO lectura: toda llamada que no sea de lectura se bloquea):
  · sin lista el comportamiento es el de siempre (control);
  · código interno, nombre y atributo genérico funcionan cuando se activan, y solo entonces;
  · una fuente apagada no se usa (el orden/la lista manda);
  · el guard de números sueltos (580 no trae L6580);
  · el término se registra como «no encontrado» solo si NINGUNA fuente lo resolvió.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_busqueda_fuentes.py
"""
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
CODIGO = f('codigo')
NOMBRE = f('nombre', modo='inicio')
MODELO = f('atributo', 22, 'contiene')


def herr(fuentes=None, canal=None):
    ctx = {'partner_id': None, 'bot_id': 2}
    if fuentes is not None:
        ctx['busqueda_fuentes'] = fuentes
    if canal:
        ctx['channel_id'] = canal
    return {t.name: t for t in create_odoo_tools(ctx)}


def bp(h, q):
    return h['buscar_producto'].invoke({'referencia': q})


def brm(h, q):
    return h['buscar_referencias_mensaje'].invoke({'mensaje': q})


NO = 'No encontre'

print('\n── Sin lista: el comportamiento de siempre ──')
h = herr()
check("sin lista: 121070 sigue sin encontrarse (el código no se busca hasta activarlo)", NO in bp(h, '121070'))
check("sin lista: '1020' se encuentra (caso del cliente)", 'ID:1834' in bp(h, '1020'))
h = herr(BASE)
check("lista por defecto (sin código): 121070 no se encuentra", NO in bp(h, '121070'))

print('\n── Código interno ──')
h = herr(BASE + [CODIGO])
out = bp(h, '121070')
check("buscar_producto('121070') con la fuente «código» → Tóner HP U4 (ID:1847)", 'ID:1847' in out, out.splitlines()[0][:60])
out = brm(h, 'necesito 121070')
check("buscar_referencias_mensaje('necesito 121070') → PRODUCT_ID:1847", 'PRODUCT_ID:1847' in out, out.splitlines()[0][:70])
check("…y como 'exacta' (✅)", '✅' in out)
check("el código de un producto NO publicado no se ofrece (990001, domicilio)", NO in bp(h, '990001'))
h = herr(BASE)
check("sin la fuente «código», buscar_referencias_mensaje('necesito 121070') no lo encuentra",
      'PRODUCTO_NO_ENCONTRADO' in brm(h, 'necesito 121070'))

print('\n── Nombre ──')
h = herr(BASE + [NOMBRE])
out = bp(h, 'hp 711')
check("con «nombre»: 'hp 711' → HP 711A", '711A' in out, out.splitlines()[0][:60])
check("sin «nombre»: 'hp 711' no se encuentra", NO in bp(herr(BASE), 'hp 711'))
out = bp(h, '580')
check("guard de número suelto: '580' no trae L6580 ni ML-2580N", 'L6580' not in out and '2580N' not in out)

print('\n── Atributo genérico (Modelo) ──')
check("sin la fuente: 'cf258xsc' no se encuentra", NO in bp(herr(BASE), 'cf258xsc'))
out = bp(herr(BASE + [MODELO]), 'cf258xsc')
check("con atributo «Modelo»: 'cf258xsc' → Tóner HP 58X", '58X' in out, out.splitlines()[0][:60])

print('\n── La lista manda (una fuente apagada no se usa) ──')
h = herr([CODIGO])
check("solo «código»: 121070 sí", 'ID:1847' in bp(h, '121070'))
check("solo «código»: 'p1102w' (término exacto) ya no, porque «exacto» está apagado", NO in bp(h, 'p1102w'))
check("lista base: 'p1102w' sí", NO not in bp(herr(BASE), 'p1102w'))

print('\n── «No encontrado» solo si ninguna fuente lo resolvió ──')
h = herr(BASE, canal=1)
del bloqueadas[:]
bp(h, 'zzqx98765')
check("término inexistente → se registra UNA vez", sum(1 for _, m in bloqueadas if m == 'enviar_aviso_busqueda_similar') == 1)
del bloqueadas[:]
brm(h, 'necesito 1020N')   # solo existe como término de SIMILITUD (id 3931): lo resuelve la fase similar
check("1020N (lo resuelve «similar») NO se registra como no encontrado",
      not any(m == 'enviar_aviso_busqueda_similar' for _, m in bloqueadas))
del bloqueadas[:]
brm(h, 'necesito zzqx98765')
check("referencia inexistente en buscar_referencias_mensaje → se registra",
      sum(1 for _, m in bloqueadas if m == 'enviar_aviso_busqueda_similar') == 1)

print(f"\n{'=' * 56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for x in fail:
    print(f"    ✗ {x}")
print(f"{'=' * 56}\n")
sys.exit(1 if fail else 0)
