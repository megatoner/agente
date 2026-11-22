"""La traza de herramientas guarda argumentos y resultado (y trunca) sin romper nada."""
import sys
sys.path.insert(0, "/opt/odoo-agents")
from app.agents.executor import _anotar_traza, _TRAZA_RESULT_MAX

fallos = []
def chk(c, m):
    print(("OK   " if c else "FALLA"), m)
    if not c:
        fallos.append(m)

u = {"input_tokens": 1}
_anotar_traza(u, "buscar_producto", {"referencia": "150A"}, "No encontre productos")
t = u["_tool_trace"]
chk(len(t) == 1 and t[0]["name"] == "buscar_producto", "registra la herramienta")
chk('"150A"' in t[0]["args"] and "No encontre" in t[0]["result"], "guarda argumentos y resultado")
_anotar_traza(u, "x", {}, "y" * (_TRAZA_RESULT_MAX + 500))
chk(len(u["_tool_trace"][1]["result"]) == _TRAZA_RESULT_MAX and u["_tool_trace"][1]["result_len"] == _TRAZA_RESULT_MAX + 500, "trunca pero conserva el largo real")
class Raro:
    def __str__(self): raise RuntimeError("no se puede")
try:
    _anotar_traza(u, "z", Raro(), Raro()); chk(True, "un objeto imposible de imprimir no lanza")
except Exception:
    chk(False, "un objeto imposible de imprimir no lanza")
sys.exit(1 if fallos else 0)
