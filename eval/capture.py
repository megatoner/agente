"""Captura de runs reales para el harness de evaluación.

Apagado por defecto. Se activa con la variable de entorno:

    JWB_EVAL_CAPTURE=/opt/odoo-agents/eval/data/captura.jsonl

Graba un registro JSON por run con TODO lo necesario para re-ejecutarlo:
system prompt (deduplicado por sha), mensajes de entrada, nombres de tools,
modelo, y el resultado observado en producción (output, tools_used,
iterations, usage). Ese resultado es la línea base contra la que se compara
cualquier configuración alterna.

Diseño defensivo: cualquier excepción aquí se traga. Este módulo NUNCA puede
tumbar una conversación real.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ENV_VAR = "JWB_EVAL_CAPTURE"
# Marcador que reemplaza el base64 de imágenes (pesan MB y no se replican)
_IMG_PLACEHOLDER = "<<IMAGEN-OMITIDA>>"


def enabled() -> bool:
    return bool(os.getenv(_ENV_VAR, "").strip())


def _paths():
    base = os.getenv(_ENV_VAR, "").strip()
    d = os.path.dirname(base) or "."
    return base, os.path.join(d, "systems")


def _strip_images(content: Any) -> Any:
    """Reemplaza data base64 de imágenes por un marcador."""
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "image":
                out.append({"type": "image", "source": {"type": "base64",
                                                        "media_type": (b.get("source") or {}).get("media_type", ""),
                                                        "data": _IMG_PLACEHOLDER}})
            else:
                out.append(b)
        return out
    return content


def _store_system(system_text: str, systems_dir: str) -> str:
    sha = hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:16]
    os.makedirs(systems_dir, exist_ok=True)
    path = os.path.join(systems_dir, f"{sha}.txt")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(system_text)
    return sha


def record(
    session_id: Optional[str],
    model_name: str,
    system_content: str,
    messages: List[Dict[str, Any]],
    tool_names: List[str],
    temperature: Optional[float],
    max_iterations: int,
    result: Dict[str, Any],
    odoo_context: Optional[Dict[str, Any]] = None,
    max_tokens: int = 4096,
    message: str = "",
) -> None:
    """Escribe un registro. Silencioso ante cualquier error."""
    if not enabled():
        return
    try:
        jsonl_path, systems_dir = _paths()
        os.makedirs(os.path.dirname(jsonl_path) or ".", exist_ok=True)
        sha = _store_system(system_content or "", systems_dir)

        has_image = False
        clean_msgs = []
        for m in messages:
            c = m.get("content")
            if isinstance(c, list) and any(
                isinstance(b, dict) and b.get("type") == "image" for b in c
            ):
                has_image = True
            clean_msgs.append({"role": m.get("role"), "content": _strip_images(c)})

        rec = {
            "ts": time.time(),
            "session_id": session_id,
            "model": model_name,
            "system_sha": sha,
            "temperature": temperature,
            "max_iterations": max_iterations,
            "tool_names": sorted(tool_names),
            # Mensaje CRUDO del cliente. El replay se lo pasa a run_agent, que
            # reconstruye el contexto dinámico con el código de producción —
            # así el mismo formato sirve para captura en vivo y para backfill
            # histórico (ver eval/backfill.py).
            "message": message,
            # El array ya armado queda solo como referencia de lo que se envió.
            "messages": clean_msgs,
            "has_image": has_image,
            "max_tokens": max_tokens,
            # Necesario para reconstruir las closures de las tools en el replay
            # (partner_id, channel_id, pricelist, warehouse...). OJO: contiene
            # datos del cliente — el archivo no debe salir del servidor.
            "odoo_context": odoo_context or {},
            "baseline": {
                "output": result.get("output", ""),
                "tools_used": result.get("tools_used", []),
                "iterations": result.get("iterations", 0),
                "usage": result.get("usage", {}),
            },
        }
        with open(jsonl_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # nunca romper una conversación real por telemetría
        logger.debug("eval.capture: fallo al grabar (ignorado)", exc_info=True)
