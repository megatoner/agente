"""Informe del agente investigador (`armar_informe`, pura, sin red).

Garantías: (1) todo código del informe aparece LITERALMENTE en un extracto citado — lo que la IA
invente se descarta; (2) «fabricante» solo con evidencia de un dominio oficial; lo que viene de
tiendas/compatibles es «terceros»; (3) sin evidencia, confianza «ninguna».

    cd /opt/odoo-agents && .venv/bin/python eval/checks/check_investigar_termino.py
"""
import sys
sys.path.insert(0, '/opt/odoo-agents')
from app.odoo.investigar_termino import armar_informe, modelo_valido  # noqa: E402

ok, fail = [], []


def check(n, c, d=''):
    (ok if c else fail).append(n)
    print(f"  [{'OK ' if c else 'FALLA'}] {n}{(' — ' + d) if d else ''}")


OFICIAL = {"url": "https://www.usa.canon.com/shop/p/cartridge-137", "title": "Cartridge 137",
           "cited_text": "Canon Cartridge 137 black toner, part 9435B001, for imageCLASS MF240 series, LBP151dw."}
TIENDA = {"url": "https://www.tiendatoner.example/toner-cf283x", "title": "Tóner CF283X compatible",
          "cited_text": "Tóner compatible CF283X reemplaza al CRG-137 de Canon, rendimiento 2.200 páginas."}
FORO = {"url": "https://community.hp.com/t5/foo", "title": "foro", "cited_text": "usen XX999Z"}

print("Fabricante oficial → confianza fabricante")
r = armar_informe("CRG137", "Canon", [OFICIAL], {"tipo": "cartucho", "marca": "Canon",
                  "codigos_oem": ["9435B001"], "impresoras_compatibles": ["MF240", "LBP151dw"]})
check("confianza fabricante", r["confianza"] == "fabricante", r["confianza"])
check("trae el código OEM", r["codigos_oem"] == ["9435B001"])
check("marca la fuente como oficial", r["fuentes"][0]["oficial"] is True)

print("Solo tiendas → terceros, nunca fabricante")
r = armar_informe("CRG137", "Canon", [TIENDA], {"tipo": "cartucho", "equivalentes": ["CF283X"]})
check("confianza terceros", r["confianza"] == "terceros", r["confianza"])
check("equivalente CF283X", r["equivalentes"] == ["CF283X"])
check("fuente NO oficial", r["fuentes"][0]["oficial"] is False)

print("Lo inventado se descarta")
r = armar_informe("CRG137", "Canon", [OFICIAL, TIENDA], {"tipo": "cartucho",
                  "codigos_oem": ["9435B001", "ZZ12345"], "equivalentes": ["CF283X", "CF999Q"]})
check("el OEM inventado no pasa", r["codigos_oem"] == ["9435B001"], str(r["codigos_oem"]))
check("el equivalente inventado no pasa", r["equivalentes"] == ["CF283X"], str(r["equivalentes"]))
check("queda registrado qué se descartó", set(r["descartados"]) == {"ZZ12345", "CF999Q"}, str(r["descartados"]))
check("no cuela un código parcial (CF283 no está, CF283X sí)",
      armar_informe("x", None, [TIENDA], {"equivalentes": ["CF283"]})["equivalentes"] == [])

print("Foros no cuentan como oficial; sin evidencia → ninguna")
r = armar_informe("X", "HP", [FORO], {"codigos_oem": ["XX999Z"]})
check("un foro no da confianza fabricante", r["confianza"] != "fabricante", r["confianza"])
check("fuente de foro no oficial", r["fuentes"][0]["oficial"] is False)
r = armar_informe("X", None, [], {})
check("sin citas ni datos → ninguna y ok=False", r["confianza"] == "ninguna" and r["ok"] is False)
r = armar_informe("X", None, [TIENDA], {"codigos_oem": ["NOEXISTE1"]})
check("la IA dice un código que no está → ninguna", r["confianza"] == "ninguna" and r["ok"] is False)

print("Respalda al sugerido y robustez")
check("respalda_sugerido solo booleano", armar_informe("x", None, [TIENDA], {"equivalentes": ["CF283X"],
      "respalda_sugerido": "quizá"})["respalda_sugerido"] is None)
check("respalda_sugerido True se conserva", armar_informe("x", None, [TIENDA], {"equivalentes": ["CF283X"],
      "respalda_sugerido": True})["respalda_sugerido"] is True)
check("extraído None no revienta", armar_informe("x", None, [TIENDA], None)["confianza"] == "ninguna")

print("Modelo elegido desde Odoo")
check("claude-sonnet-5-5 es válido", modelo_valido("claude-sonnet-5-5") == "claude-sonnet-5-5")
check("mayúsculas y espacios se normalizan", modelo_valido("  Claude-Haiku-5-5 ") == "claude-haiku-5-5")
check("vacío → None (usa Haiku)", modelo_valido("") is None and modelo_valido(None) is None)
check("otro proveedor se ignora", modelo_valido("gpt-4o") is None and modelo_valido("gemini-2.0-flash") is None)
check("texto raro / inyección se ignora", modelo_valido("claude-x; rm -rf /") is None and modelo_valido("claude-") is None)

print(f"\n{len(ok)} OK, {len(fail)} FALLAN")
sys.exit(1 if fail else 0)
