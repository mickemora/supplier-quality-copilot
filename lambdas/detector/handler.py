"""Repeat root-cause detector.

Runs on a schedule (and on demand). Clusters NCR root-cause signatures across
suppliers, asks Claude to name and explain each pattern, and overwrites
``curated/alerts/latest.json`` (the Athena ``alerts`` table).

Invoke manually with an optional override payload:
    {"threshold": 0.75, "recent_window_days": 90, "today": "2026-10-15"}
"""
import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone

import boto3

from common.bedrock import converse_tool
from common.lake import ALERTS_KEY, load_embeddings
from common.similarity import cosine, find_patterns

log = logging.getLogger()
log.setLevel(logging.INFO)

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")

LAKE_BUCKET = os.environ["LAKE_BUCKET"]
MODEL_ID = os.environ["SUMMARY_MODEL_ID"]
THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.78"))
WINDOW = int(os.environ.get("RECENT_WINDOW_DAYS", "60"))
MANIFEST_KEY = "meta/manifest.json"

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern_label": {"type": "string", "description": "Under 8 words, e.g. 'Gold plating thickness from bath drift'"},
        "summary": {"type": "string", "description": (
            "3-5 sentences: the shared failure mechanism, which suppliers and NCRs, whether an "
            "earlier corrective action worked (cite the NCR), and the recommended next step for "
            "the supplier quality engineer.")},
    },
    "required": ["pattern_label", "summary"],
}

SYSTEM = (
    "You are a supplier quality analyst at an aerospace and defense manufacturer. You are given "
    "NCRs from different suppliers whose root causes look alike. Explain the common pattern "
    "precisely and only from the records provided. Name NCR ids as evidence."
)


def _summarize(pattern: dict) -> dict:
    records = json.dumps(pattern["records"], indent=1, default=str)
    try:
        return converse_tool(bedrock, MODEL_ID, SYSTEM,
                             f"<ncr_records>\n{records}\n</ncr_records>",
                             "record_pattern", "Record the pattern label and summary.",
                             SUMMARY_SCHEMA, max_tokens=800)
    except Exception:  # keep the alert even if the summary call fails
        log.exception("summary failed")
        return {"pattern_label": f"Repeat {pattern['dominant_root_cause_category']} root cause",
                "summary": "Summary unavailable; review the listed NCRs."}


def _similarity_stats(items: list[dict]) -> dict:
    """Distribution of cross-supplier similarities, logged to help tune the threshold."""
    scores = sorted(
        cosine(a["vector"], b["vector"])
        for i, a in enumerate(items) for b in items[i + 1:]
        if a.get("supplier_id") != b.get("supplier_id")
    )
    if not scores:
        return {}
    pick = lambda q: round(scores[min(len(scores) - 1, int(q * len(scores)))], 3)  # noqa: E731
    return {"pairs": len(scores), "p50": pick(0.5), "p90": pick(0.9), "p99": pick(0.99),
            "max": round(scores[-1], 3)}


def _as_of(event: dict) -> date:
    """Date the "recent window" is measured from.

    Payload ``today`` wins; otherwise the synthetic dataset's as-of date
    (``meta/manifest.json``, uploaded by load_data.py) so the demo storyline holds
    no matter when it runs; otherwise the real date.
    """
    if event.get("today"):
        return date.fromisoformat(event["today"])
    try:
        manifest = json.loads(s3.get_object(Bucket=LAKE_BUCKET, Key=MANIFEST_KEY)["Body"].read())
        return date.fromisoformat(manifest["as_of"])
    except Exception:
        return date.today()


def handler(event, _context):
    event = event if isinstance(event, dict) else {}
    threshold = float(event.get("threshold", THRESHOLD))
    window = int(event.get("recent_window_days", WINDOW))
    today = _as_of(event)

    items = load_embeddings(s3, LAKE_BUCKET)
    stats = _similarity_stats(items)
    log.info("ncrs=%d threshold=%.2f window=%d stats=%s", len(items), threshold, window, stats)

    patterns = find_patterns(items, threshold=threshold, recent_window_days=window, today=today)
    created_at = datetime.now(timezone.utc).isoformat()

    lines = []
    for pattern in patterns:
        text = _summarize(pattern)
        alert_id = "ALERT-" + hashlib.sha1(",".join(pattern["ncr_ids"]).encode()).hexdigest()[:10]
        lines.append(json.dumps({
            "alert_id": alert_id,
            "created_at": created_at,
            "pattern_label": text["pattern_label"],
            "summary": text["summary"],
            "ncr_ids": pattern["ncr_ids"],
            "supplier_ids": pattern["supplier_ids"],
            "supplier_count": pattern["supplier_count"],
            "ncr_count": pattern["ncr_count"],
            "avg_similarity": pattern["avg_similarity"],
            "dominant_root_cause_category": pattern["dominant_root_cause_category"],
        }))

    s3.put_object(Bucket=LAKE_BUCKET, Key=ALERTS_KEY,
                  Body=("\n".join(lines) + ("\n" if lines else "")).encode(),
                  ContentType="application/json")

    return {"ncrs_scanned": len(items), "patterns": len(lines), "threshold": threshold,
            "similarity_stats": stats,
            "labels": [json.loads(line)["pattern_label"] for line in lines]}
