"""Tools del Agente de Caja.

Son envoltorios finos sobre ``jpc.caja.agent.service`` de Odoo: toda la logica
(reglas, auditoria, niveles de automatizacion) vive y se prueba en el modulo
``jpc_caja_agent``. Aqui solo se llama por RPC.

Dos decisiones de diseno:
- Entran a Odoo con credenciales PROPIAS (CAJA_ODOO_USERNAME / CAJA_ODOO_API_KEY
  en el .env), no con la cuenta global del servidor: el usuario "Agente de Caja"
  solo tiene el grupo Contador de Caja, y lo que firma queda a su nombre.
- Solo se exponen herramientas de lectura y de PROPUESTA. Aplicar propuestas y
  contabilizar lo hace el ciclo de Odoo segun el nivel configurado; el modelo
  de lenguaje nunca contabiliza.
"""
import json
import logging
from typing import List, Optional

from langchain.tools import tool

from app.config import get_settings
from app.odoo.client import OdooClient

logger = logging.getLogger(__name__)

_SERVICE = "jpc.caja.agent.service"
_client: Optional[OdooClient] = None


def _caja_client() -> OdooClient:
    global _client
    if _client is None:
        s = get_settings()
        if not s.caja_odoo_username or not s.caja_odoo_api_key:
            raise RuntimeError(
                "Faltan CAJA_ODOO_USERNAME / CAJA_ODOO_API_KEY en el .env "
                "del servidor de agentes.")
        _client = OdooClient(
            username=s.caja_odoo_username, api_key=s.caja_odoo_api_key)
    return _client


def _clean_error(exc: Exception) -> str:
    """El mensaje util de un error de Odoo (UserError) sin el traceback."""
    text = str(exc)
    marker = "'message': "
    idx = text.rfind(marker)
    if idx < 0:
        return text[:500]
    seg = text[idx + len(marker):]
    quote = seg[:1]
    seg = seg[1:] if quote in ("'", '"') else seg
    end = seg.find(quote + ", 'arguments'") if quote in ("'", '"') else -1
    return (seg[:end] if end >= 0 else seg)[:500]


def _call(method: str, *args, **kwargs) -> str:
    try:
        result = _caja_client().execute_kw(_SERVICE, method, list(args), kwargs)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 - el agente debe ver el motivo
        message = _clean_error(exc)
        logger.warning("caja tool %s: %s", method, message)
        return json.dumps({"error": message}, ensure_ascii=False)


def create_caja_tools():
    @tool
    def caja_estado() -> str:
        """Nivel de automatizacion actual del Agente de Caja y sus topes
        (monto maximo, confianza minima, muestra). Llamala primero para saber
        que esta permitido."""
        return _call("status")

    @tool
    def caja_listar_pendientes(session_id: int = 0, limit: int = 0) -> str:
        """Movimientos pendientes de contabilizar de cajas cerradas, con
        concepto, beneficiario, cuenta, facturas y la sugerencia de las reglas
        (confianza y razon). session_id=0 trae todas las cajas."""
        return _call("pending_lines", session_id=session_id, limit=limit)

    @tool
    def caja_auditar_sesion(session_id: int) -> str:
        """Audita una caja cerrada: diferencia de arqueo, movimientos
        duplicados, montos atipicos y salidas sin beneficiario o sin cuenta.
        Solo lee y deja el informe en la sesion."""
        return _call("audit_session", session_id=session_id)

    @tool
    def caja_sugerir(line_id: int) -> str:
        """Sugerencia de las reglas para un movimiento: cuenta, beneficiario,
        factura, confianza (0 a 1), razon y alertas."""
        return _call("suggest", line_id=line_id)

    @tool
    def caja_proponer(
        line_id: int,
        confidence: float,
        reasoning: str,
        partner_id: int = 0,
        concept_id: int = 0,
        account_id: int = 0,
        invoice_ids: Optional[List[int]] = None,
    ) -> str:
        """Registra TU propuesta para un movimiento: beneficiario, concepto,
        cuenta y facturas de proveedor (0 = no cambia), con tu confianza (0 a
        1) y la razon en una frase. No modifica el movimiento: una persona, o el
        nivel autonomo de Odoo, la decide. Usa solo ids que hayas visto en las
        herramientas."""
        return _call(
            "submit_proposal", line_id=line_id, partner_id=partner_id,
            concept_id=concept_id, account_id=account_id,
            invoice_ids=invoice_ids or [], confidence=confidence,
            reasoning=reasoning, source="llm")

    return [caja_estado, caja_listar_pendientes, caja_auditar_sesion,
            caja_sugerir, caja_proponer]
