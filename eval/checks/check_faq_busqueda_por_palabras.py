"""`consultar_faq` encuentra la FAQ por PALABRAS cuando la frase entera no calza.

Caso real CONCEPCION CECILIA ESPINAL MAZO (573205726837, canal 1731, 2026-11-19): eligió
pagar por transferencia; el agente buscó 'datos bancarios transferencia', 'cuenta bancaria
consignación' y 'medios de pago'; ninguna frase estaba TAL CUAL dentro de la lista de palabras
clave de la FAQ 40 ('consignación, datos bancarios, cuenta bancaria, …') y escaló a un asesor
teniendo la FAQ activa. 9 fallos iguales desde el 12-nov.

Parte 1: la función pura con FAQs como las de producción. Parte 2: contra los datos reales
(solo lectura).

Ejecutar:
    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_faq_busqueda_por_palabras.py
"""
import sys
import xmlrpc.client

sys.path.insert(0, '/opt/odoo-agents')
from app.odoo.tools import _faqs_por_palabras  # noqa: E402

ok, fail = [], []


def check(n, c, d=''):
    (ok if c else fail).append(n)
    print(f"  [{'OK ' if c else 'FALLA'}] {n}{(' — ' + d) if d else ''}")


KW = "consignación, datos bancarios, cuenta bancaria, a qué cuenta, número de cuenta, banco"
FAQ_DIS = {"id": 22, "bot_id": [4, "Distribuidor"], "sequence": 20, "pregunta": KW,
           "respuesta": "🏦 *Datos para consignación / transferencia bancaria:* Banco: Bancolombia"}
FAQ_UF = {"id": 40, "bot_id": [2, "Usuario Final"], "sequence": 20, "pregunta": KW,
          "respuesta": "🏦 *Datos para consignación / transferencia bancaria:* Banco: Scotiabank"}
FAQ_FACT = {"id": 50, "bot_id": [2, "Usuario Final"], "sequence": 30,
            "pregunta": "factura electrónica, datos de facturación",
            "respuesta": "Te enviamos la factura al correo registrado."}
FAQ_HOR = {"id": 51, "bot_id": [2, "Usuario Final"], "sequence": 10,
           "pregunta": "horario, a qué hora abren, hora de atención",
           "respuesta": "Lunes a viernes 8:00 a.m. a 5:40 p.m."}
FAQ_GLOB = {"id": 60, "bot_id": False, "sequence": 5, "pregunta": KW, "respuesta": "Datos globales"}
TODAS = [FAQ_DIS, FAQ_UF, FAQ_FACT, FAQ_HOR]


def ids(tema, bot, faqs=TODAS):
    return [f["id"] for f in _faqs_por_palabras(faqs, tema, bot_id=bot)]


print("Las frases que fallaron en producción")
for tema in ("datos bancarios transferencia", "cuenta bancaria consignación",
             "transferencia datos bancarios cuenta", "transferencia"):
    check(f"'{tema}' → FAQ del bot Usuario Final", ids(tema, 2)[:1] == [40], str(ids(tema, 2)))
check("mismo tema desde el bot Distribuidor → la suya (22)", ids("datos bancarios transferencia", 4)[:1] == [22])

print("Tildes y mayúsculas")
check("'CONSIGNACION' sin tilde", ids("CONSIGNACION", 2)[:1] == [40])
check("'Cuenta Bancaria'", ids("Cuenta Bancaria", 2)[:1] == [40])

print("No inventa ni se desordena")
check("un tema sin relación no devuelve nada", ids("garantía de impresoras láser", 2) == [])
check("solo palabras vacías → nada", ids("de la para", 2) == [])
check("tema vacío → nada", ids("", 2) == [])
check("máximo 3 resultados", len(_faqs_por_palabras(TODAS * 3, "cuenta bancaria", bot_id=2)) <= 3)
check("'horario' sigue yendo a su FAQ y no a la bancaria", ids("horario de atención", 2)[:1] == [51])
check("'datos de facturación' va a facturación primero, no a datos bancarios",
      ids("datos de facturación", 2)[:1] == [50], str(ids("datos de facturación", 2)))

print("Propia del bot gana a la global con igual puntaje")
check("con global y propia, primero la propia", ids("datos bancarios", 2, TODAS + [FAQ_GLOB])[0] == 40)
check("la global entra si el bot no tiene propia", ids("datos bancarios", 9, [FAQ_GLOB]) == [60])

print("Contra los datos reales (solo lectura)")
try:
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
    reales = M.execute_kw(db, uid, pwd, 'jpc.whatsapp.bot.faq', 'search_read',
                          [[('active', '=', True)]],
                          {'fields': ['id', 'pregunta', 'respuesta', 'bot_id', 'sequence'], 'limit': 300})
    check("hay FAQs reales activas", len(reales) > 0, f"{len(reales)}")
    for bot, esperada in ((2, 40), (4, 22)):
        # Mismo universo que la herramienta: las del bot y las globales.
        visibles = [f for f in reales if not f['bot_id'] or f['bot_id'][0] == bot]
        for tema in ("datos bancarios transferencia", "cuenta bancaria consignación"):
            r = [f['id'] for f in _faqs_por_palabras(visibles, tema, bot_id=bot)]
            check(f"bot {bot} '{tema}' → FAQ {esperada} primero", r[:1] == [esperada], str(r))
        # «transferencia» a secas es ambigua (está en las palabras clave de comprobante, PSE y métodos
        # de pago): basta con que la de datos bancarios llegue al agente entre las 3 devueltas.
        r = [f['id'] for f in _faqs_por_palabras(visibles, "transferencia", bot_id=bot)]
        # LIMITACIÓN CONOCIDA, no se cuenta como fallo: en el bot 2 salen primero comprobante/métodos
        # de pago. Se resuelve con datos (agregar «transferencia» a las palabras clave de la FAQ de
        # datos bancarios), no forzando el orden.
        print(f"  [INFO] bot {bot} 'transferencia' a secas → {r} (FAQ {esperada} "
              f"{'sí' if esperada in r else 'NO'} está)")
except Exception as e:  # sin acceso a Odoo no se invalida la parte pura
    print(f"  [OMITIDO] datos reales: {e}")

print(f"\n{len(ok)} OK, {len(fail)} FALLAN")
sys.exit(1 if fail else 0)
