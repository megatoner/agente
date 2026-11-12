"""Verificación (sin red) del aprendizaje de impresoras desde el fabricante.

Regla: solo se escribe con respaldo textual del sitio del fabricante. Aquí se prueba
`verificar` y los filtros con extractos de ejemplo.

    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_impresora_fabricante_verificar.py
"""
import sys
sys.path.insert(0, '/opt/odoo-agents')
from app.odoo.fabricante_impresoras import (  # noqa: E402
    verificar, parece_impresora, detectar_marca, tokens_modelo)

ok, fail = [], []


def check(n, c, d=''):
    (ok if c else fail).append(n)
    print(f"  [{'OK ' if c else 'FALLA'}] {n}{(' — ' + str(d)) if d else ''}")


HP = 'https://support.hp.com/co-es/document/ish_2821157-1228967-16'
cita_m406 = [{'url': HP, 'title': 'Consumibles de las impresoras HP LaserJet Enterprise M406 y M407',
              'cited_text': 'Cartucho de tóner negro original HP LaserJet 58A CF258A y cartucho de gran capacidad 58X CF258X para M406dn'}]
ext = {'es_impresora': True, 'marca': 'HP', 'modelos': ['M406dn', 'M406'], 'cartuchos': ['CF258A', 'CF258X']}

# --- gate barato
for t in ('HP LaserJet Enterprise M406', 'L3110', 'M127FN', 'impresora epson L5590', 'P1102W', 'M1212NF'):
    check(f'parece impresora: {t}', parece_impresora(t))
for t in ('544', 'X1', '2K', '711', 'CF258A', 'TN830XL', 'W1105A', 'Q7553A', '964XL', 'hola', 'T544120'):
    check(f'NO parece impresora: {t}', not parece_impresora(t))
check('marca HP', detectar_marca('HP LaserJet Enterprise M406') == 'HP')
check('marca Epson por EcoTank', detectar_marca('ecotank L3110') == 'Epson')
check('sin marca', detectar_marca('M406') is None)

# --- caso feliz
v = verificar('HP LaserJet Enterprise M406', 'HP', cita_m406, ext)
check('M406: ok', v['ok'], v)
check('M406: ambos cartuchos', v.get('codigos') == ['CF258A', 'CF258X'], v.get('codigos'))
check('M406: términos incluyen M406 y M406dn', {'M406', 'M406dn'} <= set(v.get('terminos', [])), v.get('terminos'))

# --- región por URL y foros
au = [dict(cita_m406[0], url='https://www.hp.com/au-en/shop/hp-laserjet-enterprise-m406dn-printer-3pz15a.html')]
check('tienda de Australia: rechaza', not verificar('HP M406', 'HP', au, ext)['ok'])
es = [dict(cita_m406[0], url='https://support.hp.com/es-es/document/x')]
check('España sin mención de Américas: rechaza', not verificar('HP M406', 'HP', es, ext)['ok'])
es2 = [dict(cita_m406[0], url='https://support.hp.com/es-es/document/x', cited_text='Norteamérica y Latinoamérica: 58A CF258A y 58X CF258X. M406dn')]
check('España pero el texto dice Américas: acepta', verificar('HP M406', 'HP', es2, ext)['ok'])
foro = [dict(cita_m406[0], url='https://h30434.www3.hp.com/t5/Printer-Ink/Hp-m406/td-p/123')]
check('foro de la comunidad HP: rechaza', not verificar('HP M406', 'HP', foro, ext)['ok'])
mx = [dict(cita_m406[0], url='https://www.hp.com/mx-es/shop/x')]
check('México: acepta', verificar('HP M406', 'HP', mx, ext)['ok'])
sin = [dict(cita_m406[0], url='https://epson.com.co/i/C11CG87301')]
check('Epson Colombia sin locale: acepta', verificar('Epson M406', 'Epson', sin, dict(ext, marca='Epson'))['ok'])

# --- fuente que NO es del fabricante
malo = [{'url': 'https://blog.tonerbarato.com/m406', 'title': 'M406', 'cited_text': cita_m406[0]['cited_text']}]
check('dominio no oficial: rechaza', not verificar('HP M406', 'HP', malo, ext)['ok'])
falso = [{'url': 'https://hp.com.evil.com/x', 'title': 'M406', 'cited_text': cita_m406[0]['cited_text']}]
check('dominio parecido (hp.com.evil.com): rechaza', not verificar('HP M406', 'HP', falso, ext)['ok'])

# --- código inventado por la IA (no está en el extracto)
inv = dict(ext, cartuchos=['CF258A', 'CF999Z'])
check('código inventado se descarta, el real queda', verificar('HP M406', 'HP', cita_m406, inv).get('codigos') == ['CF258A'])
check('solo inventados: rechaza', not verificar('HP M406', 'HP', cita_m406, dict(ext, cartuchos=['CF999Z']))['ok'])

# --- modelo de otra impresora (la página habla de otro modelo)
otra = [{'url': HP, 'title': 'HP LaserJet Pro M404', 'cited_text': 'cartucho 58A CF258A para M404dn'}]
check('fuente de otro modelo: rechaza', not verificar('HP LaserJet M406', 'HP', otra, dict(ext, modelos=['M404dn']))['ok'])

# --- regiones: HP da 59A en Europa y 58A en Américas
eur = [{'url': HP, 'title': 'M406', 'cited_text': 'En Europa, Oriente Medio y Asia: 59A CF259A. M406dn'}]
check('solo región Europa: rechaza', not verificar('HP M406', 'HP', eur, dict(ext, cartuchos=['CF259A']))['ok'])
mix = [{'url': HP, 'title': 'M406', 'cited_text': 'Norteamérica y Latinoamérica: 58A CF258A. Europa y Asia: 59A CF259A. M406dn'}]
v2 = verificar('HP M406', 'HP', mix, dict(ext, cartuchos=['CF258A', 'CF259A']))
check('mixto: queda solo el de Américas', v2.get('ok') and v2['codigos'] == ['CF258A'], v2)

# --- el código y el modelo deben ir juntos (caso L3110 vs frascos 544)
sueltos = [
    {'url': 'https://epson.com.co/i/C11CG87301', 'title': 'EcoTank L3210', 'cited_text': 'Botella T544120 negro para EcoTank L3210'},
    {'url': 'https://epson.com.co/i/otra', 'title': 'Epson', 'cited_text': 'Reemplaza a la antigua L3110'}]
e2 = {'es_impresora': True, 'marca': 'Epson', 'modelos': ['L3110'], 'cartuchos': ['T544120']}
check('código y modelo en extractos sueltos: rechaza', not verificar('Epson L3110', 'Epson', sueltos, e2)['ok'])
juntos = [{'url': 'https://epson.com.co/i/x', 'title': 'Tintas', 'cited_text': 'Para L3110 use la botella T664120 negro'}]
check('código y modelo juntos: acepta', verificar('Epson L3110', 'Epson', juntos, dict(e2, cartuchos=['T664120']))['ok'])
titulo = [{'url': 'https://epson.com.co/i/y', 'title': 'Epson L3110 - Botella T664120', 'cited_text': 'Botella de tinta negra T664120'}]
check('modelo en el título de la página: acepta', verificar('Epson L3110', 'Epson', titulo, dict(e2, cartuchos=['T664120']))['ok'])

# --- no es impresora / sin citas
check('IA dice que no es impresora', not verificar('CF258A', 'HP', cita_m406, {'es_impresora': False})['ok'])
check('sin citas', not verificar('HP M406', 'HP', [], ext)['ok'])
check('tokens modelo', tokens_modelo('HP LaserJet Enterprise M406') == ['M406'], tokens_modelo('HP LaserJet Enterprise M406'))

print(f"\n{len(ok)} OK, {len(fail)} FALLAS")
sys.exit(1 if fail else 0)
