"""Re-ejecuta runs capturados contra una configuración alterna.

Reutiliza el MISMO loop de producción (`_run_with_anthropic_client`), así que
lo que se mide es el comportamiento real del agente, no una simulación. Las
escrituras y envíos quedan bloqueados por `DryRunGuard`, de modo que ambos
brazos del A/B ven exactamente el mismo estado de Odoo y ningún cliente recibe
nada.

Uso:

    # brazo A — línea base (el system prompt con el que corrió en producción)
    ./.venv/bin/python -m eval.replay --out eval/data/arm_base.jsonl

    # brazo B — system prompt alterno
    ./.venv/bin/python -m eval.replay \
        --system /ruta/prompt_nuevo.txt --out eval/data/arm_nuevo.jsonl

    # brazo B — modelo alterno
    ./.venv/bin/python -m eval.replay \
        --model anthropic/claude-haiku-4-5-20251001 --out eval/data/arm_haiku.jsonl
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, "/opt/odoo-agents")

from app.agents.executor import _get_anthropic_tools, _run_with_anthropic_client, _model_name
from app.agents.providers import get_llm
from app.odoo.tools import create_odoo_tools
from eval.dryrun import DryRunGuard

logger = logging.getLogger(__name__)

DEFAULT_CAPTURE = "/opt/odoo-agents/eval/data/captura.jsonl"


def load_records(path: str, limit: int = 0, skip_images: bool = True) -> List[Dict[str, Any]]:
    recs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if skip_images and r.get("has_image"):
                continue  # el base64 no se captura; no se puede replicar fielmente
            recs.append(r)
            if limit and len(recs) >= limit:
                break
    return recs


def system_for(rec: Dict[str, Any], override_path: str, capture_path: str) -> str:
    if override_path:
        return open(override_path, encoding="utf-8").read()
    systems_dir = os.path.join(os.path.dirname(capture_path), "systems")
    sha = rec.get("system_sha", "")
    with open(os.path.join(systems_dir, f"{sha}.txt"), encoding="utf-8") as fh:
        return fh.read()


def replay_one(rec: Dict[str, Any], system_prompt: str, model_override: str) -> Dict[str, Any]:
    llm_model = model_override or rec.get("model") or ""
    llm_obj = get_llm(llm_model)

    ctx = rec.get("odoo_context") or {}
    all_tools = create_odoo_tools(ctx)
    wanted = set(rec.get("tool_names") or [])
    tools = [t for t in all_tools if t.name in wanted] if wanted else all_tools
    tools_by_name = {t.name: t for t in tools}
    anthropic_tools = _get_anthropic_tools(tools)

    messages = [{"role": "system", "content": system_prompt}] + list(rec.get("messages") or [])

    t0 = time.time()
    with DryRunGuard() as guard:
        output, tools_used, iterations, usage = _run_with_anthropic_client(
            llm_obj=llm_obj,
            model_name=_model_name(llm_model),
            messages=messages,
            anthropic_tools=anthropic_tools,
            tools_by_name=tools_by_name,
            max_iterations=int(rec.get("max_iterations") or 8),
            temperature=rec.get("temperature"),
            session_id=f"replay-{rec.get('session_id')}",
            max_tokens=int(rec.get("max_tokens") or 4096),
        )
    elapsed = time.time() - t0

    return {
        "session_id": rec.get("session_id"),
        "ts_original": rec.get("ts"),
        "model": llm_model,
        "output": output,
        "tools_used": tools_used,
        "iterations": iterations,
        "usage": usage,
        "elapsed_s": round(elapsed, 2),
        "writes_blocked": guard.blocked,
        "reads": guard.reads,
        "baseline": rec.get("baseline") or {},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay A/B de runs capturados (dry-run).")
    ap.add_argument("--input", default=DEFAULT_CAPTURE, help="JSONL de captura")
    ap.add_argument("--out", required=True, help="JSONL de resultados")
    ap.add_argument("--system", default="", help="system prompt alterno (archivo)")
    ap.add_argument("--model", default="", help="modelo alterno, ej. anthropic/claude-haiku-4-5-20251001")
    ap.add_argument("--limit", type=int, default=0, help="máximo de escenarios")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    recs = load_records(args.input, limit=args.limit)
    if not recs:
        print(f"Sin registros replicables en {args.input}", file=sys.stderr)
        return 1

    print(f"Replicando {len(recs)} escenarios "
          f"(system={'alterno' if args.system else 'original'}, "
          f"modelo={args.model or 'original'})")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    ok = err = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for i, rec in enumerate(recs, 1):
            try:
                sp = system_for(rec, args.system, args.input)
                res = replay_one(rec, sp, args.model)
                ok += 1
            except Exception as e:
                res = {"session_id": rec.get("session_id"), "error": str(e)[:300],
                       "baseline": rec.get("baseline") or {}}
                err += 1
            fh.write(json.dumps(res, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"  [{i}/{len(recs)}] {'OK ' if 'error' not in res else 'ERR'} "
                  f"tools={res.get('tools_used', [])} iter={res.get('iterations', '-')}")

    print(f"\nListo: {ok} ok, {err} con error -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
