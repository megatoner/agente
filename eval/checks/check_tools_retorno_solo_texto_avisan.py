"""Las tools que solo devuelven texto (no le mandan nada al cliente por su
cuenta) deben traer el aviso explícito de "respóndele ahora" en su resultado —
mismo patrón que quitar_envio_orden (caso VICTOR, 2026-09-21).

Auditoría 2026-09-30: _TOOLS_QUE_COMUNICAN_SOLAS (jwb_agent_bridge.py, Odoo)
tenía 5 nombres muertos y, de los 2 reales, generar_link_cotizacion NO debía
estar — solo devuelve texto. Si el modelo la llamaba y quedaba en silencio, el
sistema lo daba por "silencio intencional": el cliente se quedaba sin nada, SIN
escalar. Se sacó de la whitelist y se le agregó (junto con enviar_link_pago_
whatsapp, enviar_factura_whatsapp, enviar_cotizacion_whatsapp y enviar_estado_
cuenta_whatsapp — todas devuelven texto para que el modelo lo copie) el mismo
aviso explícito que ya llevaba quitar_envio_orden.

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_tools_retorno_solo_texto_avisan.py
"""
import sys
import xmlrpc.client

sys.path.insert(0, '/opt/odoo-agents')
from app.odoo.tools import create_odoo_tools  # noqa: E402

env = {}
for l in open('/opt/odoo-agents/.env'):
    l = l.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
url = env.get('ODOO_URL', 'http://127.0.0.1:8019')
db, pwd = env['ODOO_DB'], (env.get('ODOO_PASSWORD') or env.get('ODOO_API_KEY'))
user = env.get('ODOO_USER') or env.get('ODOO_USERNAME')
uid = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common').authenticate(db, user, pwd, {})
M = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')


def call(model, method, *a, **kw):
    return M.execute_kw(db, uid, pwd, model, method, list(a), kw)


ok, fail = [], []


def check(n, c, d=''):
    (ok if c else fail).append(n)
    print(f"  [{'OK ' if c else 'FALLA'}] {n}{(' — ' + d) if d else ''}")


cli = 140356  # TERMINALES DE TRANSPORTE DE MEDELLIN, con deuda real de prueba
prod = call('product.product', 'search', [['sale_ok', '=', True]], limit=1)[0]
orden = call('sale.order', 'create', {
    'partner_id': cli, 'state': 'draft', 'payment_method_id': 215,
    'order_line': [(0, 0, {'product_id': prod, 'product_uom_qty': 1})]})
factura = call('account.move', 'search', [['move_type', '=', 'out_invoice'], ['partner_id', 'child_of', cli]],
               limit=1)

tools = {t.name: t for t in create_odoo_tools({'partner_id': cli})}
AVISO_FRAGMENTO = 'ESTE TEXTO AL CLIENTE'

out = tools['generar_link_cotizacion'].invoke({'order_id': orden})
check("generar_link_cotizacion avisa", AVISO_FRAGMENTO in out, out.splitlines()[-1][:60])

if factura:
    fid = factura[0]
    out = tools['enviar_link_pago_whatsapp'].invoke({'invoice_id': fid})
    check("enviar_link_pago_whatsapp avisa", AVISO_FRAGMENTO in out)
    out = tools['enviar_factura_whatsapp'].invoke({'invoice_id': fid})
    check("enviar_factura_whatsapp avisa", AVISO_FRAGMENTO in out)
else:
    print("  (sin factura de prueba disponible — se saltan esas 2 verificaciones)")

out = tools['enviar_cotizacion_whatsapp'].invoke({'order_id': orden})
check("enviar_cotizacion_whatsapp avisa", AVISO_FRAGMENTO in out)

out = tools['enviar_estado_cuenta_whatsapp'].invoke({'partner_id': cli})
check("enviar_estado_cuenta_whatsapp avisa", AVISO_FRAGMENTO in out)

call('sale.order', 'action_cancel', [orden])

print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
