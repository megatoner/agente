"""buscar_referencias_mensaje: un producto AGOTADO no es «no encontrado» (2026-11-22).

Caso Majo Taborda (573235909230): el HP 667 Negro existe, está publicado y tiene stock 0 en el almacén del
bot. La herramienta lo descartaba y respondía `[PRODUCTO_NO_ENCONTRADO: 667]` con la instrucción de escalar,
aunque `buscar_producto` sí lo mostraba (agotado). Ahora se reporta AGOTADO, no se mete al carrito para
cotizar, y lo que sí tiene stock o lo que no existe se comporta igual que antes.

Solo lectura sobre producción:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_referencias_agotado_no_es_no_encontrado.py
"""
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
buscar = lambda m: t["buscar_referencias_mensaje"].invoke({"mensaje": m})

stock = c.execute_kw("product.product", "read", [[2061]], {"fields": ["qty_available"], "context": {"warehouse_id": 1}})[0]["qty_available"]
print(f"\n(HP 667 Negro id 2061, stock en el almacén 1 = {stock:.0f})")
if stock > 0:
    print("  (el 667 ya tiene stock: no se puede probar el caso agotado con él)")

print("\nProducto existente SIN stock")
out = buscar("667")
if stock <= 0:
    check("ya no responde «no encontrado»", "PRODUCTO_NO_ENCONTRADO" not in out, out.splitlines()[0][:80])
    check("trae el producto 2061", "PRODUCT_ID:2061" in out)
    check("lo marca AGOTADO (no cotizar)", "AGOTADO (no cotizar)" in out)
    check("indica no dar precio ni cotizar", "No des precio" in out)
    check("NO manda a escalar por esto", "NO escales" in out and "escalar_a_asesor con motivo" not in out)
    check("«necesito 667» igual", "PRODUCT_ID:2061" in buscar("necesito 667"))

print("\nControl: algo CON stock no cambia")
con_stock = c.execute_kw("product.product", "search_read",
                         [[("is_published", "=", True), ("product_tmpl_id.jpc_website_ids", "in", [1]), ("qty_available", ">", 0)]],
                         {"fields": ["id", "product_tmpl_id"], "limit": 1, "context": {"warehouse_id": 1}})
out = buscar("664 x2")
check("664 (con stock) sigue sin marca de agotado", "PRODUCT_ID:67" in out and "AGOTADO" not in out, out.splitlines()[0][:80])
check("y con su cantidad (la cantidad va DESPUÉS de la referencia: «2 de 664» da 1, limitación previa)", "Qty:2" in out)

print("\nControl: lo que no existe sigue siendo «no encontrado»")
out = buscar("zzqx99999")
check("código inexistente → PRODUCTO_NO_ENCONTRADO", "PRODUCTO_NO_ENCONTRADO" in out and "AGOTADO" not in out)

print(f"\n{len(ok)} OK / {len(fail)} FALLAS")
sys.exit(1 if fail else 0)
