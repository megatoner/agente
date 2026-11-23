"""buscar_referencias_mensaje: la cantidad también puede ir ANTES de la referencia (2026-11-22).

Hasta hoy solo se leía la cantidad escrita DESPUÉS («664 x2», «664 2»); «2 de 664» y «necesito 3 664» daban
1, así que un cliente que pedía 3 recibía una cotización de 1. Con varias referencias, «2 de 664 y 3 de 85A»
daba 3 y 1 (el 3 se lo llevaba la 664). Una referencia anterior exige conector («3 de 85A»): «664 2 85A»
sigue siendo «2» de la 664.

Solo lectura sobre producción:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_referencias_cantidad_antes.py
"""
import re
import sys

sys.path.insert(0, '/opt/odoo-agents')
from app.odoo import tools  # noqa: E402

c = tools.get_odoo_client()
ok, fail = [], []


def check(n, cond, d=''):
    (ok if cond else fail).append(n)
    print(f"  [{'OK ' if cond else 'FALLA'}] {n}{(' — ' + d) if d else ''}")


rows = c.execute_kw("jpc.whatsapp.bot.busqueda.fuente", "search_read", [[("bot_id", "=", 2)]],
                    {"fields": ["tipo", "atributo_id", "coincidencia", "activo"], "order": "sequence"})
f = [{"tipo": r["tipo"], "atributo_id": (r["atributo_id"] or [0])[0], "coincidencia": r["coincidencia"]}
     for r in rows if r["activo"]]
ctx = {"bot_id": 2, "bot_pricelist_id": 9, "bot_warehouse_id": 1, "price_fallback": "list_price",
       "busqueda_fuentes": f, "bot_website_id": 1}
t = {x.name: x for x in tools.create_odoo_tools(ctx)}


def qty(msg):
    out = t["buscar_referencias_mensaje"].invoke({"mensaje": msg})
    return [(int(p), int(q)) for p, q in re.findall(r"PRODUCT_ID:(\d+) \|.*?\| Qty:(\d+)", out)]


P664, P85A = 67, 1847   # Epson 664 / HP 85A (con stock en el almacén 1)
print("\nCantidad ANTES de la referencia")
check("«2 de 664» → 2", qty("2 de 664") == [(P664, 2)])
check("«necesito 3 664» → 3", qty("necesito 3 664") == [(P664, 3)])
check("«3 unidades de 664» → 3", qty("3 unidades de 664") == [(P664, 3)])
check("«2 botellas de la 664» → 2", qty("2 botellas de la 664") == [(P664, 2)])
check("«quiero 5 tintas 664» → 5", qty("quiero 5 tintas 664") == [(P664, 5)])

print("\nVarias referencias")
check("«2 de 664 y 3 de 85A» → 2 y 3", qty("2 de 664 y 3 de 85A") == [(P664, 2), (P85A, 3)])
check("«664 y 3 de 85A» → 1 y 3 (el 3 es de la 85A)", qty("664 y 3 de 85A") == [(P664, 1), (P85A, 3)])

print("\nLo de siempre no cambia")
check("«664 x2» → 2", qty("664 x2") == [(P664, 2)])
check("«664 2» → 2", qty("664 2") == [(P664, 2)])
check("«664 x2 y 85A x3» → 2 y 3", qty("664 x2 y 85A x3") == [(P664, 2), (P85A, 3)])
check("«664 2 85A» sigue siendo 2 de la 664 (sin conector no se reasigna)", qty("664 2 85A") == [(P664, 2), (P85A, 1)])
check("«hola 664» → 1", qty("hola 664") == [(P664, 1)])
check("«tengo 2 impresoras, necesito 664» → 1 (el 2 no es cantidad del cartucho)",
      qty("tengo 2 impresoras, necesito 664") == [(P664, 1)])

print("\nFalsos positivos hallados con mensajes REALES de clientes (direcciones y teléfonos)")
check("«CR 25 4 165»: el 4 pertenece a la dirección, no es cantidad", all(q == 1 for _, q in qty("esa es la direccion CR 25 4 165 CS 9842 Medellín")))
check("«+57 304 3500308»: el 57 es el indicativo, no una cantidad", all(q == 1 for _, q in qty("EDGARD ANDRES CELL: +57 304 3500308 HOME: +57342979326")))
print("\nMensajes reales que SÍ mejoran")
check("«Son 4 toner U4, 1 105x» → 4 y 1", [q for _, q in qty("Son 4 toner U4, 1 105x")] == [4, 1])
check("«2 toner 101S/111S con chip» → 2", qty("Me facturas por favor 2 toner 101S/111S con chip")[0][1] == 2)
check("«20 unidades de 105x» → 20", qty("caja de toner 20 unidades de 105x con chips")[0][1] == 20)

print(f"\n{len(ok)} OK / {len(fail)} FALLAS")
sys.exit(1 if fail else 0)
