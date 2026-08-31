"""El contexto de escalación lleva el estado del pedido."""
import sys, xmlrpc.client
sys.path.insert(0, '/opt/odoo-agents')

env = {}
for l in open('/opt/odoo-agents/.env'):
    l = l.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1); env[k.strip()] = v.strip().strip('"').strip("'")
url = env.get('ODOO_URL','http://127.0.0.1:8019'); db = env['ODOO_DB']
pwd = env.get('ODOO_PASSWORD') or env.get('ODOO_API_KEY')
uid = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common').authenticate(
    db, env.get('ODOO_USER') or env.get('ODOO_USERNAME'), pwd, {})
M = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')
def call(m, me, *a, **kw): return M.execute_kw(db, uid, pwd, m, me, list(a), kw)

ok, fail = [], []
def check(n, c, d=''):
    (ok if c else fail).append(n)
    print(f"  [{'OK ' if c else 'FALLA'}] {n}{(' — ' + d) if d else ''}")

print("\n1) El método RPC del checklist existe y responde")
ch = 4836
try:
    pend = call('jpc.whatsapp.bot.carrito', 'jwb_pendientes_del_canal', ch)
    check("responde sin error", True, repr(pend)[:60])
except Exception as e:
    check("responde sin error", False, str(e)[:70])

print("\n2) Un canal con carrito devuelve lo que falta")
car = call('jpc.whatsapp.bot.carrito','search_read',
           [['state','in',['open','quoted']],['channel_id','!=',False]],
           fields=['channel_id','paso_actual'], limit=1)
if car:
    cid = car[0]['channel_id'][0]
    p = call('jpc.whatsapp.bot.carrito','jwb_pendientes_del_canal', cid)
    check("devuelve texto útil", isinstance(p, str),
          f"paso={car[0]['paso_actual']} -> {p[:60]!r}")
else:
    check("hay un carrito para probar", False)

print("\n3) Canal inexistente no rompe")
try:
    r = call('jpc.whatsapp.bot.carrito','jwb_pendientes_del_canal', 999999)
    check("devuelve vacío sin error", r == '', repr(r))
except Exception as e:
    check("devuelve vacío sin error", False, str(e)[:60])

print(f"\n{'='*54}\n  {len(ok)} OK · {len(fail)} FALLAS")
for f in fail: print(f"    ✗ {f}")
print(f"{'='*54}\n")
sys.exit(1 if fail else 0)
