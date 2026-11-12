"""Corrida ÚNICA sobre los términos no encontrados pendientes (2026-11-12).

Toma de jpc.whatsapp.bot.unknown.term los términos pendientes que parecen un modelo de
impresora y se repitieron `--min` veces, y aplica `aprender_impresora` (misma verificación
que en vivo: solo escribe con respaldo textual del sitio del fabricante).

    cd /opt/odoo-agents && .venv/bin/python scripts/aprender_impresoras_backlog.py [--min 2] [--dry]
Resultado: logs/backlog_impresoras_<fecha>.tsv
"""
import argparse, csv, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, '/opt/odoo-agents')
from app.odoo.client import get_odoo_client            # noqa: E402
from app.odoo.fabricante_impresoras import aprender_impresora, parece_impresora  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('--min', type=int, default=2)
ap.add_argument('--dry', action='store_true')
ap.add_argument('--reintentar-errores', metavar='TSV', help='solo los términos con error de un TSV anterior')
a = ap.parse_args()

odoo = get_odoo_client()
rows = odoo.execute_kw('jpc.whatsapp.bot.unknown.term', 'read_group',
                       [[('state', '=', 'pending')], ['term'], ['term']], {'lazy': False})
cuenta = {}
for r in rows:
    t = (r.get('term') or '').strip()
    if t and parece_impresora(t):
        cuenta[t.upper()] = cuenta.get(t.upper(), 0) + r.get('__count', 0)
terminos = sorted([t for t, n in cuenta.items() if n >= a.min], key=lambda t: -cuenta[t])
if a.reintentar_errores:
    con_error = {r['termino'] for r in csv.DictReader(open(a.reintentar_errores), delimiter='\t')
                 if r['motivo'].startswith('error')}
    terminos = [t for t in terminos if t in con_error]
print(len(terminos), 'términos a procesar', flush=True)
if a.dry:
    sys.exit(0)

out = open(f'/opt/odoo-agents/logs/backlog_impresoras_{time.strftime("%Y%m%d_%H%M")}.tsv', 'w', newline='')
w = csv.writer(out, delimiter='\t')
w.writerow(['termino', 'apariciones', 'aprendido', 'motivo', 'terminos_creados', 'productos', 'url'])


def uno(t):
    r = aprender_impresora(odoo, t, None, 0)
    w.writerow([t, cuenta[t], r.get('aprendido'), r.get('motivo', ''), '|'.join(r.get('terminos') or []),
                '|'.join(p['name'][:40] for p in (r.get('productos') or [])), r.get('url', '')])
    out.flush()
    return r


with ThreadPoolExecutor(max_workers=3) as pool:
    res = list(pool.map(uno, terminos))
ok = sum(1 for r in res if r.get('aprendido'))
print(f'listo: {ok}/{len(res)} aprendidos', flush=True)
