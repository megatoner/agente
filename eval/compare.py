"""Compara dos brazos de replay y reporta regresiones.

Uso:
    ./.venv/bin/python -m eval.compare eval/data/arm_base.jsonl eval/data/arm_nuevo.jsonl

Señales que se vigilan (en orden de gravedad para el negocio):

  1. ESCALACIÓN        — el brazo B escala a asesor donde A resolvía (o al revés).
  2. TOOLS DISTINTAS   — llamó otro conjunto de herramientas: cambió la decisión.
  3. PRECIOS DISTINTOS — los importes citados al cliente no coinciden.
  4. SIN RESPUESTA     — output vacío en un brazo y no en el otro.
  5. ITERACIONES       — más iteraciones = más costo y más latencia.
  6. COSTO             — tokens y USD por escenario.

Los primeros cuatro son bloqueantes: si aparecen, el cambio NO se despliega.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List

# Precios con separador de miles, igual que el guard del executor
_PRECIO_RE = re.compile(r"\$\s?\d{1,3}(?:[.,]\d{3})+")

# Tarifas Sonnet 4.6 / Haiku 4.5 (USD por millón de tokens)
_TARIFAS = {
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "cr": 0.30, "cw": 6.0},
    "claude-haiku-4-5": {"in": 1.0, "out": 5.0, "cr": 0.10, "cw": 2.0},
}


def _tarifa(model: str) -> Dict[str, float]:
    m = (model or "").split("/")[-1]
    for k, v in _TARIFAS.items():
        if m.startswith(k):
            return v
    return _TARIFAS["claude-sonnet-4-6"]


def costo(usage: Dict[str, Any], model: str) -> float:
    t = _tarifa(model)
    return (
        (usage.get("input_tokens", 0) or 0) * t["in"] / 1e6
        + (usage.get("output_tokens", 0) or 0) * t["out"] / 1e6
        + (usage.get("cache_read_tokens", 0) or 0) * t["cr"] / 1e6
        + (usage.get("cache_write_tokens", 0) or 0) * t["cw"] / 1e6
    )


def load(path: str) -> Dict[str, Dict[str, Any]]:
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            key = f"{r.get('session_id')}|{r.get('ts_original')}"
            out[key] = r
    return out


def precios(texto: str) -> set:
    return {p.replace(" ", "") for p in _PRECIO_RE.findall(texto or "")}


def main(argv: List[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 1
    a, b = load(argv[1]), load(argv[2])
    claves = [k for k in a if k in b]
    if not claves:
        print("Los dos brazos no comparten escenarios.")
        return 1

    bloqueantes: List[str] = []
    n_tools_dif = n_esc = n_precio = n_vacio = 0
    ca = cb = 0.0
    ia = ib = 0

    for k in claves:
        ra, rb = a[k], b[k]
        if "error" in ra or "error" in rb:
            continue
        ta, tb = sorted(ra.get("tools_used") or []), sorted(rb.get("tools_used") or [])
        oa, ob = ra.get("output") or "", rb.get("output") or ""
        sid = ra.get("session_id")

        ca += costo(ra.get("usage") or {}, ra.get("model", ""))
        cb += costo(rb.get("usage") or {}, rb.get("model", ""))
        ia += ra.get("iterations", 0)
        ib += rb.get("iterations", 0)

        esc_a, esc_b = "escalar_a_asesor" in ta, "escalar_a_asesor" in tb
        if esc_a != esc_b:
            n_esc += 1
            bloqueantes.append(
                f"[ESCALACIÓN] {sid}: base={'escala' if esc_a else 'resuelve'} "
                f"-> nuevo={'escala' if esc_b else 'resuelve'}"
            )
        if bool(oa.strip()) != bool(ob.strip()):
            n_vacio += 1
            bloqueantes.append(
                f"[SIN RESPUESTA] {sid}: base={len(oa)} chars, nuevo={len(ob)} chars"
            )
        if ta != tb:
            n_tools_dif += 1
            bloqueantes.append(f"[TOOLS] {sid}: base={ta} -> nuevo={tb}")
        pa, pb = precios(oa), precios(ob)
        if pa != pb:
            n_precio += 1
            bloqueantes.append(f"[PRECIOS] {sid}: base={sorted(pa)} -> nuevo={sorted(pb)}")

    n = len(claves)
    print(f"Escenarios comparados: {n}\n")
    print("--- REGRESIONES BLOQUEANTES ---")
    print(f"  Escalación cambiada : {n_esc:4d}  ({n_esc / n * 100:.1f}%)")
    print(f"  Respuesta vacía     : {n_vacio:4d}  ({n_vacio / n * 100:.1f}%)")
    print(f"  Tools distintas     : {n_tools_dif:4d}  ({n_tools_dif / n * 100:.1f}%)")
    print(f"  Precios distintos   : {n_precio:4d}  ({n_precio / n * 100:.1f}%)")
    print()
    print("--- COSTO / LATENCIA ---")
    print(f"  Iteraciones  base={ia:5d}   nuevo={ib:5d}   "
          f"({(ib - ia) / ia * 100:+.1f}%)" if ia else "")
    print(f"  Costo USD    base={ca:.4f}  nuevo={cb:.4f}  "
          f"({(cb - ca) / ca * 100:+.1f}%)" if ca else "")
    print(f"  Por escenario base=${ca / n:.4f}  nuevo=${cb / n:.4f}")

    if bloqueantes:
        print(f"\n--- DETALLE ({len(bloqueantes)}) ---")
        for line in bloqueantes[:60]:
            print(" ", line)
        if len(bloqueantes) > 60:
            print(f"  ... y {len(bloqueantes) - 60} más")
        print("\nVEREDICTO: NO desplegar. Hay regresiones que revisar.")
        return 2

    print("\nVEREDICTO: sin regresiones detectadas en las señales bloqueantes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
