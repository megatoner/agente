"""Candado dry-run para el harness de evaluación.

Todo acceso a Odoo desde las tools pasa por `OdooClient.execute_kw` — incluido
el envío de tarjetas y documentos por WhatsApp (`jwb_enviar_tarjeta_v3`,
`action_enviar_factura_pdf_agente`, ...). Interceptando ese único método se
controla el 100% de los efectos secundarios.

## Por qué no es un bloqueo total

El precio que MIA le dice al cliente se calcula creando una `sale.order`
borrador efímera, leyendo `price_unit` y borrándola (`_get_product_price_for_client`
en tools.py). Si se bloquea esa creación, el agente responde SIN precios y el
A/B queda ciego justo en la señal más crítica del negocio.

Por eso el modo por defecto es **sandbox de borradores**:

  PERMITIDO  create de sale.order / sale.order.line  (borradores desechables)
  PERMITIDO  write/unlink SOLO sobre IDs creados dentro de esta sesión
  PERMITIDO  lecturas
  BLOQUEADO  todo lo demás: confirmar, facturar, cobrar, message_post,
             cualquier jwb_enviar_*, y cualquier write/unlink sobre registros
             PREEXISTENTES

La propiedad de seguridad clave: **jamás se modifica ni se borra un registro
que existía antes del replay**, y ningún cliente recibe nada. Al salir, los
borradores que hayan quedado se eliminan.

Con `sandbox_orders=False` se bloquea también la creación de borradores
(modo paranoico, a costa de perder los precios en la comparación).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)

# Métodos ORM estándar de solo lectura.
_READ_ORM = frozenset({
    "search_read", "read", "search", "search_count", "read_group",
    "fields_get", "name_search", "name_get", "default_get",
    "check_access_rights", "get_views", "fields_view_get",
})

# Métodos custom de jpc_* que solo LEEN (verificado leyendo tools.py).
_READ_CUSTOM = frozenset({
    "jwb_consultar_datos_facturacion",
    "jwb_get_base_url",
})

_READ_ALLOWED = _READ_ORM | _READ_CUSTOM

# Modelos donde se permiten borradores desechables.
_SANDBOX_MODELS = frozenset({"sale.order", "sale.order.line"})

# Nunca permitidos, ni siquiera sobre un borrador propio: confirman, facturan,
# cobran o le escriben al cliente.
_NEVER = frozenset({
    "action_confirm", "action_post", "button_confirm", "action_cancel",
    "action_invoice_create", "create_invoices", "action_set_followup",
    "message_post", "action_quotation_send",
})

_FAKE_ID_BASE = 900_000_000


class DryRunBlocked(Exception):
    """Se intentó una operación prohibida con el candado en modo estricto."""


def _canned(method: str, args: List[Any], fake_id: int) -> Any:
    """Valor sintético plausible para una operación bloqueada."""
    if method == "create":
        return fake_id
    if method in ("write", "unlink"):
        return True
    if method == "message_post":
        return fake_id
    if method == "_portal_ensure_token":
        return f"dryrun-token-{fake_id}"
    if method == "jwb_enviar_tarjeta_v3":
        try:
            n = len(args[1]) if len(args) > 1 else 1
        except Exception:
            n = 1
        return {"enviados": n, "errors": []}
    if method == "create_invoices":
        return [fake_id]
    if method == "jwb_completar_datos_facturacion":
        return {"success": True, "mensaje": "[DRY-RUN] datos actualizados"}
    if method in ("jwb_merge_bot_context", "jwb_enviar_factura_completa_correo"):
        return True
    if method.startswith("action_") or method.startswith("button_"):
        return True
    return True


def _ids_from(args: List[Any]) -> List[int]:
    """Extrae la lista de IDs del primer argumento de write/unlink."""
    if not args:
        return []
    first = args[0]
    if isinstance(first, int):
        return [first]
    if isinstance(first, (list, tuple)):
        return [i for i in first if isinstance(i, int)]
    return []


class DryRunGuard:
    """Envuelve `execute_kw`. Usar como context manager.

    with DryRunGuard() as guard:
        ...
    guard.blocked  -> operaciones bloqueadas
    guard.created  -> borradores creados (y limpiados al salir)
    """

    def __init__(self, sandbox_orders: bool = True, strict: bool = False):
        self.sandbox_orders = sandbox_orders
        self.strict = strict
        self.blocked: List[Dict[str, Any]] = []
        self.sandboxed: List[Dict[str, Any]] = []
        self.created: Dict[str, Set[int]] = {}
        self.reads = 0
        self._client = None
        self._original = None
        self._counter = 0

    def _next_id(self) -> int:
        self._counter += 1
        return _FAKE_ID_BASE + self._counter

    def _own(self, model: str, ids: List[int]) -> bool:
        """True si TODOS los ids fueron creados dentro de esta sesión."""
        mine = self.created.get(model, set())
        return bool(ids) and all(i in mine for i in ids)

    def _block(self, model: str, method: str, args: List[Any], reason: str) -> Any:
        preview = str(args)[:200] if args else ""
        self.blocked.append({
            "model": model, "method": method,
            "args_preview": preview, "reason": reason,
        })
        logger.info("[DRY-RUN] bloqueado %s.%s (%s) args=%s", model, method, reason, preview)
        if self.strict:
            raise DryRunBlocked(f"{model}.{method} ({reason})")
        return _canned(method, args or [], self._next_id())

    def __enter__(self) -> "DryRunGuard":
        from app.odoo.client import get_odoo_client

        self._client = get_odoo_client()
        self._original = self._client.execute_kw
        guard = self

        def execute_kw(model, method, args=None, kwargs=None):
            args = args or []

            if method in _READ_ALLOWED:
                guard.reads += 1
                return guard._original(model, method, args, kwargs)

            if method in _NEVER:
                return guard._block(model, method, args, "operacion irreversible")

            if guard.sandbox_orders and model in _SANDBOX_MODELS:
                if method == "create":
                    new_id = guard._original(model, method, args, kwargs)
                    guard.created.setdefault(model, set()).add(new_id)
                    guard.sandboxed.append({"model": model, "method": "create", "id": new_id})
                    return new_id
                if method in ("write", "unlink", "_portal_ensure_token"):
                    ids = _ids_from(args)
                    if guard._own(model, ids):
                        res = guard._original(model, method, args, kwargs)
                        if method == "unlink":
                            for i in ids:
                                guard.created.get(model, set()).discard(i)
                        guard.sandboxed.append({"model": model, "method": method, "ids": ids})
                        return res
                    return guard._block(model, method, args, "registro preexistente")

            return guard._block(model, method, args, "escritura/envio")

        self._client.execute_kw = execute_kw
        return self

    def __exit__(self, *exc) -> None:
        try:
            # Limpiar borradores que las tools no alcanzaron a borrar.
            leftovers = sorted(self.created.get("sale.order", set()))
            if leftovers and self._original is not None:
                try:
                    self._original("sale.order", "unlink", [leftovers], None)
                    logger.info("[DRY-RUN] limpiados %s borradores: %s",
                                len(leftovers), leftovers)
                except Exception:
                    logger.warning("[DRY-RUN] no se pudieron limpiar borradores: %s",
                                   leftovers, exc_info=True)
        finally:
            if self._client is not None and self._original is not None:
                self._client.execute_kw = self._original
            self._client = None
            self._original = None
        return None
