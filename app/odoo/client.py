import time
import logging
import httpx
from typing import Any, Dict, List, Optional
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

# Errores de red transitorios que ameritan reintento
_RETRYABLE = (
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    ConnectionResetError,
    ConnectionRefusedError,
    OSError,
)
_MAX_RETRIES = 3
_RETRY_DELAYS = (1.0, 2.5, 5.0)  # segundos entre intentos


class OdooClient:
    def __init__(self, url=None, db=None, username=None, api_key=None):
        self.url = (url or settings.odoo_url).rstrip("/")
        self.db = db or settings.odoo_db
        self.username = username or settings.odoo_username
        self.api_key = api_key or settings.odoo_api_key
        self._uid: Optional[int] = None

    def _jsonrpc(self, endpoint: str, payload: dict) -> Any:
        url = f"{self.url}{endpoint}"
        headers = {"Content-Type": "application/json"}
        last_exc = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = httpx.post(url, json=payload, headers=headers, timeout=60.0)
                response.raise_for_status()
                data = response.json()
                if data.get("error"):
                    raise Exception(f"Odoo JSON-RPC error: {data['error']}")
                return data.get("result")
            except _RETRYABLE as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_DELAYS[attempt]
                    logger.warning(
                        "OdooClient._jsonrpc: intento %s/%s falló (%s: %s) — reintento en %.1fs",
                        attempt + 1, _MAX_RETRIES, type(exc).__name__, exc, delay,
                    )
                    time.sleep(delay)
                    # Si parece fallo de autenticación por uid obsoleto, resetear
                    if attempt == 1:
                        self._uid = None
                else:
                    logger.error(
                        "OdooClient._jsonrpc: %s intentos agotados — %s: %s",
                        _MAX_RETRIES, type(exc).__name__, exc,
                    )
        raise last_exc

    def authenticate(self) -> int:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "common",
                "method": "authenticate",
                "args": [self.db, self.username, self.api_key, {}]
            },
            "id": 1
        }
        uid = self._jsonrpc("/jsonrpc", payload)
        if not uid:
            raise Exception("Autenticación fallida: usuario o API key incorrectos")
        self._uid = uid
        return uid

    def ensure_auth(self) -> int:
        if self._uid is None:
            return self.authenticate()
        return self._uid

    def execute_kw(
        self,
        model: str,
        method: str,
        args: List[Any] = None,
        kwargs: Dict[str, Any] = None
    ) -> Any:
        self.ensure_auth()
        args = args or []
        kwargs = kwargs or {}
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "object",
                "method": "execute_kw",
                "args": [self.db, self._uid, self.api_key, model, method, args, kwargs]
            },
            "id": 1
        }
        return self._jsonrpc("/jsonrpc", payload)

    def search_read(
        self,
        model: str,
        domain: List[Any] = None,
        fields: List[str] = None,
        limit: int = 80,
        context: Dict[str, Any] = None,
    ) -> List[Dict]:
        kwargs = {"limit": limit}
        if fields:
            kwargs["fields"] = fields
        if context:
            kwargs["context"] = context
        return self.execute_kw(model, "search_read", [domain or []], kwargs)

    def create(self, model: str, values: Dict[str, Any]) -> int:
        return self.execute_kw(model, "create", [values])

    def write(self, model: str, ids: List[int], values: Dict[str, Any]) -> bool:
        return self.execute_kw(model, "write", [ids, values])

    def unlink(self, model: str, ids: List[int]) -> bool:
        return self.execute_kw(model, "unlink", [ids])

    def read(self, model: str, ids: List[int], fields: List[str] = None) -> List[Dict]:
        kwargs = {}
        if fields:
            kwargs["fields"] = fields
        return self.execute_kw(model, "read", [ids], kwargs)


_odoo_client: Optional[OdooClient] = None


def get_odoo_client() -> OdooClient:
    global _odoo_client
    if _odoo_client is None:
        _odoo_client = OdooClient()
    return _odoo_client
