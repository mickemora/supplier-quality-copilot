"""Supplier scorecards and the rule-based risk tag.

Pure Python over curated NCR records, the supplier master and active pattern alerts,
so it is easy to test and to explain. Every risk tag carries the list of rules that
triggered it; the AI risk brief explains the tag but never sets it.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

RECENT_DAYS = 90
YEAR_DAYS = 365
RESPONSE_DUE_DAYS = 30        # 8D response expected within 30 days of detection
SLOW_CLOSE_DAYS = 45          # average days to close above this is amber
ON_TIME_TARGET = 0.80         # on-time 8D response rate below this is amber
MIN_RESPONSES_MEASURED = 2
REPEAT_DEFECT_MIN = 3          # same defect code this many times in 12 months is amber

# Shown in the UI and README so the tag is transparent and auditable.
RULES = {
    "red": [
        "Part of an active cross-supplier root-cause pattern",
        "2 or more critical NCRs in the last 90 days",
        "An open NCR with an overdue 8D response (more than 30 days)",
        "Supplier is on probation",
    ],
    "amber": [
        "1 critical NCR in the last 90 days",
        "3 or more NCRs in the last 90 days",
        "The same defect code 3 or more times in 12 months",
        "Average time to close above 45 days (last 12 months)",
        "On-time 8D response rate below 80% (last 12 months)",
        "Supplier approval is conditional",
    ],
    "green": ["No rule triggered"],
}
LEVEL_ORDER = {"red": 0, "amber": 1, "green": 2}


def _d(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _due(rec: dict) -> date | None:
    return _d(rec.get("response_due_at")) or (
        _d(rec.get("detected_at")) + timedelta(days=RESPONSE_DUE_DAYS)
        if _d(rec.get("detected_at")) else None)


def _is_critical(rec: dict) -> bool:
    return str(rec.get("severity", "")).lower() == "critical"


def _is_open(rec: dict) -> bool:
    return str(rec.get("status", "")).lower() != "closed"


def supplier_card(sid: str, master: dict, records: list[dict], alerts: list[dict],
                  as_of: date) -> dict:
    recent_cut = as_of - timedelta(days=RECENT_DAYS)
    year_cut = as_of - timedelta(days=YEAR_DAYS)
    mine = [r for r in records if r.get("supplier_id") == sid]
    recent = [r for r in mine if (_d(r.get("detected_at")) or date.min) >= recent_cut]
    year = [r for r in mine if (_d(r.get("detected_at")) or date.min) >= year_cut]

    overdue = [r for r in mine if _is_open(r) and not _d(r.get("response_received_at"))
               and _due(r) and _due(r) < as_of]

    close_days = [(_d(r["closed_at"]) - _d(r["detected_at"])).days for r in year
                  if not _is_open(r) and _d(r.get("closed_at")) and _d(r.get("detected_at"))]
    measured = [r for r in year if _due(r) and _due(r) <= as_of]
    on_time = [r for r in measured if _d(r.get("response_received_at"))
               and _d(r["response_received_at"]) <= _due(r)]

    codes = Counter(r.get("defect_code") for r in year if r.get("defect_code"))
    repeats = [{"defect_code": c, "count": n} for c, n in codes.most_common() if n >= REPEAT_DEFECT_MIN]
    patterns = [a.get("pattern_label") for a in alerts if sid in (a.get("supplier_ids") or [])]

    card = {
        "supplier_id": sid,
        "supplier_name": master.get("supplier_name") or (mine[0].get("supplier_name") if mine else sid),
        "commodity": master.get("commodity") or (mine[0].get("commodity") if mine else ""),
        "tier": master.get("tier", ""),
        "location": master.get("location", ""),
        "approval_status": master.get("approval_status", "unknown"),
        "ncr_90d": len(recent),
        "critical_90d": sum(_is_critical(r) for r in recent),
        "ncr_12m": len(year),
        "critical_12m": sum(_is_critical(r) for r in year),
        "open": sum(_is_open(r) for r in mine),
        "overdue_responses": len(overdue),
        "overdue_ncr_ids": [r["ncr_id"] for r in overdue],
        "avg_days_to_close": round(sum(close_days) / len(close_days)) if close_days else None,
        "on_time_response_rate": (round(len(on_time) / len(measured), 2)
                                  if len(measured) >= MIN_RESPONSES_MEASURED else None),
        "responses_measured": len(measured),
        "repeat_defects": repeats,
        "patterns": patterns,
        "last_ncr_at": max((r.get("detected_at", "") for r in mine), default=None),
    }
    card["risk"], card["reasons"] = assess(card)
    return card


def assess(c: dict) -> tuple[str, list[dict]]:
    """Apply the published rules. Returns the level and every rule that fired."""
    reasons = []

    def fire(level, text):
        reasons.append({"level": level, "text": text})

    for label in c["patterns"]:
        fire("red", f"Part of an active cross-supplier pattern: {label}")
    if c["critical_90d"] >= 2:
        fire("red", f"{c['critical_90d']} critical NCRs in the last 90 days")
    if c["overdue_responses"]:
        n = c["overdue_responses"]
        fire("red", f"{n} open NCR{'s' if n > 1 else ''} with an overdue 8D response "
                    f"({', '.join(c['overdue_ncr_ids'])})")
    if c["approval_status"] == "probation":
        fire("red", "Supplier is on probation")

    if c["critical_90d"] == 1:
        fire("amber", "1 critical NCR in the last 90 days")
    if c["ncr_90d"] >= 3:
        fire("amber", f"{c['ncr_90d']} NCRs in the last 90 days")
    for rep in c["repeat_defects"]:
        fire("amber", f"Defect {rep['defect_code']} repeated {rep['count']} times in 12 months")
    if c["avg_days_to_close"] is not None and c["avg_days_to_close"] > SLOW_CLOSE_DAYS:
        fire("amber", f"NCRs take {c['avg_days_to_close']} days on average to close")
    if c["on_time_response_rate"] is not None and c["on_time_response_rate"] < ON_TIME_TARGET:
        fire("amber", f"On-time 8D responses: {round(c['on_time_response_rate'] * 100)}%")
    if c["approval_status"] == "conditional":
        fire("amber", "Supplier approval is conditional")

    if any(r["level"] == "red" for r in reasons):
        return "red", reasons
    if reasons:
        return "amber", reasons
    return "green", [{"level": "green", "text": "No rule triggered"}]


def scorecards(records: list[dict], suppliers: list[dict], alerts: list[dict],
               as_of: date) -> list[dict]:
    master = {s["supplier_id"]: s for s in suppliers}
    ids = list(master) + sorted({r.get("supplier_id") for r in records if r.get("supplier_id")} - set(master))
    cards = [supplier_card(sid, master.get(sid, {}), records, alerts, as_of) for sid in ids]
    cards.sort(key=lambda c: (LEVEL_ORDER[c["risk"]], -c["critical_90d"], -c["ncr_90d"], c["supplier_name"]))
    return cards


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def detail(supplier_id: str, records: list[dict], suppliers: list[dict], alerts: list[dict],
           as_of: date) -> dict:
    master = {s["supplier_id"]: s for s in suppliers}
    card = supplier_card(supplier_id, master.get(supplier_id, {}), records, alerts, as_of)
    mine = sorted((r for r in records if r.get("supplier_id") == supplier_id),
                  key=lambda r: r.get("detected_at", ""), reverse=True)

    # Monthly trend for the 12 months ending with the as-of month.
    months = []
    y, m = as_of.year, as_of.month
    for _ in range(12):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    months.reverse()
    trend = {k: {"month": k, "total": 0, "critical": 0} for k in months}
    for r in mine:
        d = _d(r.get("detected_at"))
        if d and _month_key(d) in trend:
            trend[_month_key(d)]["total"] += 1
            trend[_month_key(d)]["critical"] += int(_is_critical(r))

    def pareto(key_fn, label_fn):
        counts = Counter(key_fn(r) for r in mine if key_fn(r))
        total = sum(counts.values()) or 1
        out, running = [], 0
        for key, n in counts.most_common():
            running += n
            out.append({"key": key, "label": label_fn(key), "count": n,
                        "cum_pct": round(running * 100 / total)})
        return out

    descriptions = {}
    for r in mine:
        descriptions.setdefault(r.get("defect_code"), Counter())[r.get("defect_description", "")] += 1

    def defect_label(code):
        text = descriptions[code].most_common(1)[0][0] if descriptions.get(code) else ""
        text = text.split(";")[0].split(" (")[0]
        return f"{code}: {text[:56]}{'…' if len(text) > 56 else ''}"

    keep = ["ncr_id", "detected_at", "part_number", "defect_code", "defect_description",
            "severity", "root_cause_category", "status", "response_received_at", "closed_at"]
    return {
        **card,
        "trend": list(trend.values()),
        "pareto_defects": pareto(lambda r: r.get("defect_code"), defect_label),
        "pareto_causes": pareto(lambda r: r.get("root_cause_category"), lambda k: k),
        "ncrs": [{k: r.get(k) for k in keep} for r in mine],
    }
