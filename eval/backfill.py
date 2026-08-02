"""Reconstruye escenarios de evaluación a partir de conversaciones YA ocurridas.

Evita tener que esperar tráfico nuevo: toma burbujas históricas contestadas por
el bot y arma registros en el mismo formato que `capture.py`, listos para
`replay.py`.

Entrada: un JSON con las burbujas, generado con (como root):

    sudo -u postgres psql -d megatoner2026 -At -c "
    select json_agg(row_to_json(t)) from (
      select b.id, b.channel_id, b.partner_id, b.bot_id, b.unified_text,
             to_char(b.create_date,'YYYY-MM-DD HH24:MI:SS') as create_date
      from jpc_whatsapp_bot_burbuja b
      where b.state='answered' and b.answered_via='bot'
        and b.create_date > now() - interval '7 days'
        and coalesce(b.unified_text,'') <> ''
        and length(b.unified_text) between 3 and 600
      order by b.id desc limit 400
    ) t;" > eval/data/hist_raw.json

Uso:

    ./.venv/bin/python -m eval.backfill --limit 60

## Fidelidad: qué se reconstruye y qué no

RECONSTRUIDO fielmente
  - mensaje del cliente (`unified_text`, tal cual llegó)
  - datos del cliente (nombre, NIT, ciudad, empresa) leídos de Odoo
  - configuración del bot (pricelist, bodega, price_fallback)
  - historial reciente del canal, filtrado a mensajes ANTERIORES a la burbuja
  - FAQs, aplicando el mismo filtro de keywords que usa el bridge

NO reconstruido (no queda registro histórico)
  - estado del carrito en ese momento (`cart`)
  - cotización armada por asesor vigente entonces (`cotizacion_asesor`)
  - resumen de sesión anterior

Para un A/B eso NO invalida nada: ambos brazos reciben exactamente el mismo
contexto reconstruido. Lo que se compara es la decisión del agente ante un
input idéntico, no la reproducción exacta de aquel turno. Los escenarios de
carrito hay que cubrirlos con captura en vivo.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, "/opt/odoo-agents")

from app.odoo.client import get_odoo_client  # noqa: E402

DEFAULT_RAW = "/opt/odoo-agents/eval/data/hist_raw.json"
DEFAULT_OUT = "/opt/odoo-agents/eval/data/captura_hist.jsonl"
SP_PATH = "/opt/odoo-agents/eval/data/sp26.txt"
TOOLS_PATH = "/opt/odoo-agents/eval/data/a26_tools.txt"
SYSTEMS_DIR = "/opt/odoo-agents/eval/data/systems"

# Configuración por bot (jpc_whatsapp_bot_config). Ambos usan el agente 26.
BOTS = {
    4: {"pricelist": 11, "warehouse": 2, "price_fallback": "hide"},
    2: {"pricelist": 9, "warehouse": 0, "price_fallback": "list_price"},
}

# ── Filtro de FAQs: portado literal de jwb_agent_bridge.py ───────────────────
_STOPWORDS_FAQ = frozenset((
    'a', 'al', 'algo', 'como', 'con', 'cual', 'cuales', 'de', 'del', 'el',
    'ella', 'en', 'es', 'esta', 'este', 'esto', 'hay', 'la', 'las', 'le',
    'lo', 'los', 'mas', 'me', 'mi', 'muy', 'no', 'o', 'os', 'para', 'pero',
    'por', 'que', 'se', 'si', 'sin', 'su', 'sus', 'te', 'tu', 'un', 'una',
    'unas', 'unos', 'y', 'ya', 'yo', 'usted', 'ustedes', 'son', 'ser',
    'buenas', 'buenos', 'dias', 'tardes', 'noches', 'hola', 'gracias',
))
_TRANS_ACENTOS = str.maketrans('áéíóúüñ', 'aeiouun')


def _normalizar(s: str) -> str:
    return (s or '').lower().translate(_TRANS_ACENTOS)


def filtrar_faqs(faqs: List[Dict], texto: str, max_n: int = 6) -> List[Dict]:
    if not faqs or not texto:
        return []
    texto_n = _normalizar(texto)
    palabras_msg = set(re.findall(r'[a-z0-9]{2,}', texto_n)) - _STOPWORDS_FAQ
    if not palabras_msg:
        return []
    puntuadas = []
    for f in faqs:
        claves = _normalizar(f.get('pregunta', ''))
        score = 0
        for frase in (p.strip() for p in claves.split(',')):
            if len(frase) >= 3 and frase in texto_n:
                score += 3
        palabras_faq = set(re.findall(r'[a-z0-9]{2,}', claves)) - _STOPWORDS_FAQ
        score += len(palabras_msg & palabras_faq)
        if score:
            puntuadas.append((score, f))
    puntuadas.sort(key=lambda t: -t[0])
    return [f for _, f in puntuadas[:max_n]]


class Builder:
    def __init__(self):
        self.odoo = get_odoo_client()
        self._partners: Dict[int, Dict] = {}
        self._faqs: Dict[int, List[Dict]] = {}

    def partner(self, pid: int) -> Dict[str, Any]:
        if pid in self._partners:
            return self._partners[pid]
        # OJO Odoo 19: res.partner ya no tiene 'mobile' (se fusionó en 'phone').
        recs = self.odoo.read(
            "res.partner", [pid],
            ["name", "vat", "city", "commercial_partner_id", "phone"],
        )
        p = recs[0] if recs else {}
        self._partners[pid] = p
        return p

    def faqs(self, bot_id: int) -> List[Dict]:
        if bot_id in self._faqs:
            return self._faqs[bot_id]
        recs = self.odoo.search_read(
            "jpc.whatsapp.bot.faq",
            ["&", ("active", "=", True),
             "|", ("bot_id", "=", bot_id), ("bot_id", "=", False)],
            ["pregunta", "respuesta", "categoria"], limit=200,
        )
        self._faqs[bot_id] = recs or []
        return self._faqs[bot_id]

    def historial(self, channel_id: int, antes_de: str, partner_id: int) -> List[Dict]:
        """Mensajes del canal ANTERIORES a la burbuja (fidelidad histórica)."""
        recs = self.odoo.search_read(
            "mail.message",
            [("model", "=", "discuss.channel"), ("res_id", "=", channel_id),
             ("date", "<", antes_de)],
            ["body", "author_id", "date"], limit=8,
        ) or []
        recs.sort(key=lambda r: r.get("date") or "")
        out = []
        for r in recs:
            author = r.get("author_id") or [0, "?"]
            aid = author[0] if isinstance(author, (list, tuple)) else 0
            body = re.sub(r"<[^>]+>", " ", r.get("body") or "").strip()
            body = re.sub(r"\s+", " ", body)
            if not body:
                continue
            out.append({
                "author": author[1] if isinstance(author, (list, tuple)) else "?",
                "is_internal_user": aid != partner_id,
                "body": body[:500],
            })
        return out

    def build(self, row: Dict[str, Any]) -> Dict[str, Any]:
        bot_id = int(row.get("bot_id") or 4)
        cfg = BOTS.get(bot_id, BOTS[4])
        pid = int(row.get("partner_id") or 0)
        p = self.partner(pid) if pid else {}
        comm = p.get("commercial_partner_id") or [0, ""]
        comm_id = comm[0] if isinstance(comm, (list, tuple)) else 0
        comm_name = comm[1] if isinstance(comm, (list, tuple)) and comm_id != pid else ""

        ctx = {
            "channel_id": int(row.get("channel_id") or 0),
            "session_id": 0,
            "partner_id": pid,
            "partner_name": p.get("name") or "",
            "commercial_partner_name": comm_name,
            "partner_vat": p.get("vat") or "",
            "partner_city": p.get("city") or "",
            "phone": p.get("phone") or "",
            "bot_id": bot_id,
            "bot_warehouse_id": cfg["warehouse"],
            "bot_pricelist_id": cfg["pricelist"],
            "price_fallback": cfg["price_fallback"],
            "cart": {},
            "cotizacion_asesor": {},
            "recent_channel_messages": self.historial(
                int(row.get("channel_id") or 0), row.get("create_date", ""), pid
            ),
            "faqs": filtrar_faqs(self.faqs(bot_id), row.get("unified_text", "")),
        }
        return ctx


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconstruye escenarios historicos.")
    ap.add_argument("--raw", default=DEFAULT_RAW)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--bot", type=int, default=0, help="filtrar por bot_id")
    args = ap.parse_args()

    rows = json.load(open(args.raw, encoding="utf-8")) or []
    if args.bot:
        rows = [r for r in rows if int(r.get("bot_id") or 0) == args.bot]
    rows = rows[: args.limit]

    system_prompt = open(SP_PATH, encoding="utf-8").read().strip()
    tool_names = sorted(n.strip() for n in open(TOOLS_PATH).read().split(",") if n.strip())

    os.makedirs(SYSTEMS_DIR, exist_ok=True)
    import hashlib
    sha = hashlib.sha256(system_prompt.encode()).hexdigest()[:16]
    with open(os.path.join(SYSTEMS_DIR, f"{sha}.txt"), "w", encoding="utf-8") as fh:
        fh.write(system_prompt)

    b = Builder()
    n = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for row in rows:
            try:
                ctx = b.build(row)
            except Exception as e:
                print(f"  burbuja {row.get('id')}: error {str(e)[:160]}", file=sys.stderr)
                continue
            rec = {
                "ts": time.time(),
                "session_id": f"hist-{row.get('id')}",
                "model": "anthropic/claude-sonnet-4-6",
                "system_sha": sha,
                "temperature": 0.2,
                "max_iterations": 8,
                "max_tokens": 4096,
                "tool_names": tool_names,
                # Mensaje crudo: run_agent le antepone el contexto dinámico.
                "message": row.get("unified_text", ""),
                "messages": [{"role": "user", "content": row.get("unified_text", "")}],
                "has_image": False,
                "odoo_context": ctx,
                "baseline": {},          # se llena corriendo el brazo de control
                "_hist": {"burbuja_id": row.get("id"), "fecha": row.get("create_date")},
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1

    print(f"{n} escenarios historicos -> {args.out}")
    print(f"  bots: {sorted({int(r.get('bot_id') or 0) for r in rows})}")
    print(f"  canales distintos: {len({r.get('channel_id') for r in rows})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
