"""Pure-Python similarity and clustering (no numpy, so no Lambda layer needed).

Datasets in this demo are hundreds of NCRs; O(n^2) pairwise comparison is fine.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import date, timedelta


def signature_text(record: dict) -> str:
    """Compact text that captures *what went wrong and why*, for root-cause matching.

    Deliberately excludes supplier, part number, and program so that the same
    failure mechanism at different suppliers lands close together.
    """
    parts = [
        f"Commodity: {record.get('commodity', '')}.",
        f"Defect: {record.get('defect_description', '')}.",
        f"Root cause ({record.get('root_cause_category', '')}): {record.get('root_cause_detail', '')}",
    ]
    return " ".join(p for p in parts if p.strip())


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def top_k_similar(target: dict, items: list[dict], k: int = 5,
                  other_suppliers_only: bool = True) -> list[dict]:
    """Rank ``items`` by similarity to ``target`` (each has ``vector`` and ``supplier_id``)."""
    scored = []
    for item in items:
        if item["ncr_id"] == target["ncr_id"]:
            continue
        if other_suppliers_only and item.get("supplier_id") == target.get("supplier_id"):
            continue
        scored.append((cosine(target["vector"], item["vector"]), item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{**{k2: v for k2, v in item.items() if k2 != "vector"},
             "similarity": round(score, 4)} for score, item in scored[:k]]


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[rj] = ri


def find_patterns(items: list[dict], threshold: float, recent_window_days: int,
                  today: date, min_ncrs: int = 3, min_recent_suppliers: int = 2) -> list[dict]:
    """Group NCRs whose root-cause signatures are similar across suppliers.

    Edges connect NCRs at *different* suppliers with similarity >= threshold.
    A pattern is reported when it has at least ``min_ncrs`` NCRs and at least
    ``min_recent_suppliers`` distinct suppliers with an NCR in the recent window,
    so long-closed history only shows up as context for an active problem.
    """
    n = len(items)
    uf = _UnionFind(n)
    edge_scores: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            if items[i].get("supplier_id") == items[j].get("supplier_id"):
                continue
            score = cosine(items[i]["vector"], items[j]["vector"])
            if score >= threshold:
                uf.union(i, j)
                edge_scores[(i, j)] = score

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    cutoff = today - timedelta(days=recent_window_days)
    patterns = []
    for members in groups.values():
        if len(members) < min_ncrs:
            continue
        recent_suppliers = {
            items[i].get("supplier_id") for i in members
            if _parse_date(items[i].get("detected_at")) >= cutoff
        }
        if len(recent_suppliers) < min_recent_suppliers:
            continue
        member_set = set(members)
        scores = [s for (i, j), s in edge_scores.items() if i in member_set and j in member_set]
        records = sorted((items[i] for i in members), key=lambda r: r.get("detected_at", ""))
        categories = Counter(r.get("root_cause_category", "Unknown") for r in records)
        patterns.append({
            "ncr_ids": [r["ncr_id"] for r in records],
            "supplier_ids": sorted({r.get("supplier_id") for r in records}),
            "ncr_count": len(records),
            "supplier_count": len({r.get("supplier_id") for r in records}),
            "avg_similarity": round(sum(scores) / len(scores), 4) if scores else 0.0,
            "dominant_root_cause_category": categories.most_common(1)[0][0],
            "records": [{k: v for k, v in r.items() if k != "vector"} for r in records],
        })
    patterns.sort(key=lambda p: (p["supplier_count"], p["ncr_count"]), reverse=True)
    return patterns


def _parse_date(value: str | None) -> date:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return date.min
