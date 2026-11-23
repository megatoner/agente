"""buscar_referencias_mensaje: cantidades en palabras y «cajas» (2026-11-22).

«una caja de 105x» = unidades por caja del producto (Compras › Cajas › Cantidad B, jpc.box.model.qty_b).
Sin caja definida no se inventa: se avisa. Solo lectura sobre producción.
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_referencias_cajas_y_palabras.py
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


def run(msg):
    return t["buscar_referencias_mensaje"].invoke({"mensaje": msg})


def qty(msg):
    return [(int(p), int(q)) for p, q in re.findall(r"PRODUCT_ID:(\d+) \|.*?\| Qty:(\d+)", run(msg))]


P664, P85A = 67, 1847
check("«dos 664» = 2", qty("necesito dos 664") == [(P664, 2)], str(qty("necesito dos 664")))
check("«tres de 664» = 3", qty("tres de 664") == [(P664, 3)])
check("«una 664» = 1", qty("quiero una 664") == [(P664, 1)])
check("«dos de 664 y tres de 85A»", qty("dos de 664 y tres de 85A") == [(P664, 2), (P85A, 3)],
      str(qty("dos de 664 y tres de 85A")))
check("«tengo dos impresoras, necesito 664» = 1", qty("tengo dos impresoras, necesito 664") == [(P664, 1)])
o = run("una caja de 664")
check("caja sin Cantidad B definida: avisa y no inventa", "NO tiene cantidad por caja" in o, o[:200])
o = run("una caja de 105x")
m = re.search(r"Qty:(\d+)", o)
check("«una caja de 105x» = 20 unidades", bool(m) and int(m.group(1)) == 20 and "1 caja(s) = 20" in o, o[:250])
o = run("2 cajas de 105x")
m = re.search(r"Qty:(\d+)", o)
check("«2 cajas de 105x» = 40", bool(m) and int(m.group(1)) == 40, o[:250])
o = run("105x 2 cajas")
m = re.search(r"Qty:(\d+)", o)
check("«105x 2 cajas» = 40", bool(m) and int(m.group(1)) == 40, o[:250])
print(f"\n{len(ok)} OK, {len(fail)} FALLAS")
sys.exit(1 if fail else 0)
