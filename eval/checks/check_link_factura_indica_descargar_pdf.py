"""Las tools que devuelven el link del portal de una FACTURA deben indicarle
al agente que, si el cliente pide el PDF/copia DESPUÉS de recibir el link, se
descarga desde esa misma página y NO se escala a un asesor.

Caso: un cliente (JAISAL COMERCIALIZADORA DE LICORES, 573104695067) seguía
pidiendo el PDF tras recibir el link y MIA lo escalaba; el link ya lo cubre.
Es solo lectura (a lo sumo se asegura el access_token de una factura existente).

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_link_factura_indica_descargar_pdf.py
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


inv = call('account.move', 'search_read',
           [['move_type', '=', 'out_invoice'], ['state', '=', 'posted']],
           fields=['id', 'partner_id'], limit=1)[0]
tools = {t.name: t for t in create_odoo_tools({'partner_id': inv['partner_id'][0]})}

for nombre in ('enviar_factura_whatsapp', 'generar_link_pago', 'enviar_link_pago_whatsapp'):
    out = tools[nombre].invoke({'invoice_id': inv['id']})
    check(f"{nombre}: trae el link", '/my/invoices/' in out)
    check(f"{nombre}: indica descargar el PDF desde el link",
          'descargue el PDF' in out and 'botón de descarga' in out)
    check(f"{nombre}: prohíbe escalar por esto", 'NO escales' in out)

print(f"\n{'='*56}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail:
    print(f"    ✗ {f}")
print(f"{'='*56}\n")
sys.exit(1 if fail else 0)
