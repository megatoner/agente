"""
Bitácora local de uso de IA — un JSONL por día (UTC), escrito ANTES de que
la respuesta del agente viaje de vuelta a Odoo.

Por qué existe: 2026-08-02 el servicio se reinició varias veces (~8 veces
en unas horas, ver systemd journal) mientras había turnos de agente en
curso. La llamada a Anthropic ya se había completado y cobrado, pero el
registro en jpc.ai.call.log nunca se creó porque ese registro solo se
escribe en Odoo DESPUÉS de que la respuesta completa vuelve por HTTP desde
este servicio — si el proceso muere antes de responder, el costo real
queda sin ningún rastro. Comparado contra la consola real de Anthropic:
$12.88 de gasto real sin registrar en 16 días, casi todo concentrado en
esa ventana de reinicios.

Este módulo escribe el uso de CADA llamada a la API en el instante en que
se conoce (justo después de que Anthropic responde), en un archivo plano
que sobrevive a un reinicio del proceso. Un cron en Odoo
(jpc_whatsapp_bot, jwb_ai_usage_reconciliation.py) lee estos archivos y
rellena jpc.ai.call.log si el total del día no coincide.

Nunca debe lanzar una excepción hacia el llamador — un fallo acá jamás
debe tumbar la respuesta real al cliente.
"""
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_lock = threading.Lock()


def log_usage(session_id, model, iteration, input_tokens, output_tokens,
              cache_write_tokens, cache_read_tokens):
    """Escribe una línea JSONL con el uso de UNA llamada a la API (no el
    acumulado del turno completo — cada iteración del loop de tools se
    registra por separado, así una caída a mitad de un turno multi-paso
    solo pierde la iteración en curso, no las ya completadas)."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            _LOG_DIR.chmod(0o755)
        except OSError:
            pass
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = _LOG_DIR / f"ai_usage-{day}.jsonl"
        line = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "model": model,
            "iteration": iteration,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cache_read_tokens": cache_read_tokens,
        }, ensure_ascii=False)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            try:
                path.chmod(0o644)
            except OSError:
                pass
    except Exception:
        logger.exception("usage_log: error escribiendo bitácora local (no crítico)")
