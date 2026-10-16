"""Bedrock agent action group (function-details schema) for the copilot."""
import json
import logging
import os
import re
from datetime import datetime, timezone

import boto3

from agent_tools.sql_guard import UnsafeQuery, validate
from common.athena import QueryFailed, run_query
from common.lake import load_alerts, load_embeddings
from common.similarity import top_k_similar

log = logging.getLogger()
log.setLevel(logging.INFO)

s3 = boto3.client("s3")
athena = boto3.client("athena")

LAKE_BUCKET = os.environ["LAKE_BUCKET"]
WORKGROUP = os.environ["ATHENA_WORKGROUP"]
DATABASE = os.environ["GLUE_DATABASE"]
MAX_BODY_CHARS = 20_000  # agent action responses are capped at 25 KB


def _params(event) -> dict:
    return {p["name"]: p.get("value") for p in event.get("parameters", [])}


def query_quality_data(p: dict) -> dict:
    try:
        sql = validate(p.get("sql", ""))
    except UnsafeQuery as err:
        return {"error": f"Query rejected: {err}"}
    try:
        result = run_query(athena, sql, WORKGROUP, DATABASE)
    except QueryFailed as err:
        return {"error": f"Athena error: {err}", "sql": sql}
    return {"sql": sql, **result}


def get_repeat_root_cause_alerts(p: dict) -> dict:
    alerts = load_alerts(s3, LAKE_BUCKET)
    supplier = (p.get("supplier_id") or "").strip()
    if supplier:
        alerts = [a for a in alerts if supplier in a.get("supplier_ids", [])]
    alerts.sort(key=lambda a: (a.get("supplier_count", 0), a.get("ncr_count", 0)), reverse=True)
    if not alerts:
        return {"alerts": [], "note": "No active cross-supplier patterns. The detector may not have run yet."}
    return {"alerts": alerts[:10]}


def find_similar_ncrs(p: dict) -> dict:
    ncr_id = (p.get("ncr_id") or "").strip()
    top_k = int(p.get("top_k") or 5)
    items = load_embeddings(s3, LAKE_BUCKET)
    target = next((i for i in items if i["ncr_id"].lower() == ncr_id.lower()), None)
    if not target:
        return {"error": f"{ncr_id} not found. Check the id with query_quality_data."}
    matches = top_k_similar(target, items, k=max(1, min(top_k, 10)))
    for m in matches:
        m.pop("signature", None)
    return {"ncr": {k: v for k, v in target.items() if k not in {"vector", "signature"}},
            "similar_at_other_suppliers": matches}


def save_scar_draft(p: dict) -> dict:
    supplier = re.sub(r"[^A-Za-z0-9_-]", "-", p.get("supplier_id") or "unknown")
    title = p.get("title") or "SCAR draft"
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
    now = datetime.now(timezone.utc)
    key = f"scar-drafts/{supplier}/{now:%Y%m%dT%H%M%SZ}-{slug}.md"
    front_matter = (
        "---\n"
        f"title: {json.dumps(title)}\n"
        f"supplier_id: {supplier}\n"
        f"ncr_ids: {json.dumps([n.strip() for n in (p.get('ncr_ids') or '').split(',') if n.strip()])}\n"
        "status: PENDING_REVIEW\n"
        f"drafted_at: {now.isoformat()}\n"
        "drafted_by: supplier-quality-copilot (AI draft - requires SQE approval)\n"
        "---\n\n"
    )
    s3.put_object(Bucket=LAKE_BUCKET, Key=key,
                  Body=(front_matter + (p.get("markdown") or "")).encode(),
                  ContentType="text/markdown")
    return {"saved": f"s3://{LAKE_BUCKET}/{key}", "status": "PENDING_REVIEW",
            "note": "Draft saved for supplier quality engineer review. Nothing was sent."}


TOOLS = {
    "query_quality_data": query_quality_data,
    "get_repeat_root_cause_alerts": get_repeat_root_cause_alerts,
    "find_similar_ncrs": find_similar_ncrs,
    "save_scar_draft": save_scar_draft,
}


def handler(event, _context):
    function = event.get("function")
    params = _params(event)
    log.info("tool=%s params=%s", function, {k: (v[:200] if isinstance(v, str) else v)
                                             for k, v in params.items()})
    tool = TOOLS.get(function)
    try:
        result = tool(params) if tool else {"error": f"Unknown function {function}"}
    except Exception as err:  # surface errors to the agent instead of failing the turn
        log.exception("tool failed")
        result = {"error": f"{type(err).__name__}: {err}"}

    body = json.dumps(result, default=str)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + ' ... [truncated; narrow the query]'

    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup"),
            "function": function,
            "functionResponse": {"responseBody": {"TEXT": {"body": body}}},
        },
        "sessionAttributes": event.get("sessionAttributes", {}),
        "promptSessionAttributes": event.get("promptSessionAttributes", {}),
    }
