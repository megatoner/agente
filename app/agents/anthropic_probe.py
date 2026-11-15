"""Estado de la cuenta de Anthropic, visible en /v1/agents/health.

POR QUÉ EXISTE
El bot dejó de responder horas sin que nadie se enterara dos veces: 2026-10-02 (un
modelo rechazaba `temperature`) y 2026-11-15 (saldo agotado, «credit balance is too
low»). Odoo solo se enteraba cuando un cliente escribía y la llamada fallaba.

Esto hace una llamada MÍNIMA (1 token de salida, ~8 de entrada, Haiku 5.5: ~US$ 0,0000002)
como mucho cada `TTL` segundos y deja el resultado clasificado. Así el estado se conoce
también sin tráfico y se ve cuando el saldo se recupera. `health()` nunca espera por la
llamada: devuelve lo último que se sabe y, si está viejo, lo refresca en un hilo.
"""
import logging
import threading
import time

from app.config import get_settings

logger = logging.getLogger(__name__)

TTL = 300  # segundos entre pruebas
MODELO = "claude-haiku-5-5"

_lock = threading.Lock()
_refrescando = False
_estado = {"estado": "desconocido", "detalle": "", "revisado": None}


def _clasificar(exc):
    texto = str(exc)
    bajo = texto.lower()
    nombre = type(exc).__name__
    if "credit balance is too low" in bajo:
        return "saldo_agotado"
    if nombre == "AuthenticationError" or "invalid x-api-key" in bajo:
        return "clave_invalida"
    if nombre == "RateLimitError" or "rate_limit" in bajo or "usage limit" in bajo:
        return "limite"
    return "error"


def _probar():
    """Una llamada de 1 token. Devuelve {estado, detalle}."""
    import anthropic

    clave = get_settings().anthropic_api_key
    if not clave:
        return {"estado": "clave_invalida", "detalle": "ANTHROPIC_API_KEY no configurada"}
    try:
        cliente = anthropic.Anthropic(api_key=clave, timeout=8.0, max_retries=0)
        cliente.messages.create(
            model=MODELO, max_tokens=1, messages=[{"role": "user", "content": "ok"}])
        return {"estado": "ok", "detalle": ""}
    except Exception as exc:  # noqa: BLE001
        return {"estado": _clasificar(exc), "detalle": str(exc)[:200]}


def _refrescar():
    global _refrescando
    try:
        res = _probar()
        with _lock:
            _estado.update(res)
            _estado["revisado"] = time.time()
        if res["estado"] != "ok":
            logger.warning("anthropic_probe: estado=%s %s", res["estado"], res["detalle"])
    except Exception:  # noqa: BLE001  — nunca debe tumbar el servicio
        logger.exception("anthropic_probe: fallo inesperado")
    finally:
        _refrescando = False


def estado_anthropic():
    """Último estado conocido (sin esperar). Si está viejo, lanza un refresco en segundo plano."""
    global _refrescando
    with _lock:
        copia = dict(_estado)
        vencido = copia["revisado"] is None or (time.time() - copia["revisado"]) > TTL
        lanzar = vencido and not _refrescando
        if lanzar:
            _refrescando = True
    if lanzar:
        threading.Thread(target=_refrescar, name="anthropic-probe", daemon=True).start()
    if copia["revisado"]:
        copia["hace_s"] = int(time.time() - copia["revisado"])
    copia.pop("revisado", None)
    return copia
