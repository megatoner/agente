"""Prueba de humo del harness: un run real por el camino de produccion.

Ejecuta `run_agent` con el system prompt y las tools reales del agente 26,
con el candado dry-run puesto (ninguna escritura ni envio llega a Odoo) y la
captura apuntando a un archivo aparte. Sirve para validar la cadena completa
run_agent -> capture.record -> archivo, sin esperar trafico real.

    ./.venv/bin/python -m eval.smoke
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "/opt/odoo-agents")

OUT = "/opt/odoo-agents/eval/data/smoke.jsonl"
os.environ["JWB_EVAL_CAPTURE"] = OUT

from app.agents.executor import run_agent  # noqa: E402  (tras fijar la env var)
from eval.dryrun import DryRunGuard  # noqa: E402

SP_PATH = "/opt/odoo-agents/eval/data/sp26.txt"
TOOLS_PATH = "/opt/odoo-agents/eval/data/a26_tools.txt"


def main() -> int:
    system_prompt = open(SP_PATH, encoding="utf-8").read().strip()
    tool_names = [n.strip() for n in open(TOOLS_PATH).read().split(",") if n.strip()]

    # Contexto minimo pero realista: cliente identificado, sin carrito.
    odoo_context = {
        "partner_id": 1,
        "partner_name": "GRUPO JPC  S. A. S",
        "commercial_partner_name": "GRUPO JPC  S. A. S",
        "phone": "573000000000",
        "channel_id": 0,          # sin canal -> no intenta enviar tarjetas
        "price_fallback": "list_price",
    }

    agent_config = {
        "model": "anthropic/claude-sonnet-4-6",
        "system_prompt": system_prompt,
        "tools": tool_names,
        "temperature": 0.2,
        "max_iterations": 8,
        "max_tokens": 4096,
        "memory_enabled": False,
    }

    print("Ejecutando run real (dry-run) ...")
    with DryRunGuard() as guard:
        res = run_agent(
            message="hola, tienes toner para una HP M402?",
            session_id="smoke-harness",
            db=None,
            agent_config=agent_config,
            odoo_context=odoo_context,
        )

    print()
    print("output    :", (res.get("output") or "")[:400])
    print("tools_used:", res.get("tools_used"))
    print("iterations:", res.get("iterations"))
    print("usage     :", res.get("usage"))
    print("lecturas a Odoo   :", guard.reads)
    print("escrituras BLOQUEADAS:", len(guard.blocked))
    for b in guard.blocked:
        print("   -", b["model"], b["method"])

    print()
    if os.path.exists(OUT):
        recs = [json.loads(l) for l in open(OUT, encoding="utf-8") if l.strip()]
        print(f"CAPTURA OK: {len(recs)} registro(s) en {OUT}")
        r = recs[-1]
        print("  system_sha:", r["system_sha"], "| tools:", len(r["tool_names"]),
              "| ctx keys:", sorted(r["odoo_context"].keys()))
        print("  baseline:", {k: v for k, v in r["baseline"].items() if k != "output"})
    else:
        print("FALLO: no se escribio el archivo de captura")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
