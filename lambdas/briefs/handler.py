"""Supplier risk brief: a one-page, cited summary for the next supplier review.

Invoked asynchronously by the web API with {"supplier_id": "...", "requested_by": "..."}.
The rule-based risk tag is computed first and handed to Claude to explain; the model
never sets the tag. The brief is saved to briefs/<supplier_id>/latest.json.
"""
import json
import logging
import os
from datetime import datetime, timezone

import boto3

from common import scorecard
from common.bedrock import converse_text
from common.lake import load_alerts, load_as_of, load_records, load_suppliers

log = logging.getLogger()
log.setLevel(logging.INFO)

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")
LAKE = os.environ["LAKE_BUCKET"]
MODEL_ID = os.environ["BRIEF_MODEL_ID"]
MAX_NCRS = 40

SYSTEM = """You are a supplier quality analyst at an aerospace and defense manufacturer.
Write a one-page risk brief about one supplier for the supplier quality manager's next
supplier review, using ONLY the scorecard and NCR records provided.

Format: markdown with exactly these sections:
## Summary
Two or three sentences. State the risk tag exactly as given and explain which rules set it.
Do not change or second-guess the tag.
## Top issues
Bullets, most important first.
## Did corrective actions hold?
Compare closed NCRs' corrective actions with later NCRs of the same defect or root cause.
## Shared and sub-tier risks
Cross-supplier patterns and any sub-tier sources named in root causes. Say "None identified" if none.
## Recommended actions for the next review
Three to five concrete actions with owners (supplier or SQE).

Rules: cite NCR ids in square brackets after each claim, e.g. [NCR-2026-0031]. Never invent
facts, dates or numbers. Keep it under 400 words. Plain, direct language."""


def handler(event, _context):
    sid = event["supplier_id"]
    started = datetime.now(timezone.utc)
    key = f"briefs/{sid}/latest.json"
    try:
        records = load_records(s3, LAKE)
        d = scorecard.detail(sid, records, load_suppliers(s3, LAKE), load_alerts(s3, LAKE),
                             load_as_of(s3, LAKE))
        full = sorted((r for r in records if r.get("supplier_id") == sid),
                      key=lambda r: r.get("detected_at", ""), reverse=True)[:MAX_NCRS]
        keep = ["ncr_id", "detected_at", "part_number", "program", "defect_code",
                "defect_description", "severity", "root_cause_category", "root_cause_detail",
                "containment", "corrective_action", "status", "response_due_at",
                "response_received_at", "closed_at"]
        card = {k: v for k, v in d.items() if k not in ("ncrs", "trend")}
        prompt = (
            f"<scorecard>\n{json.dumps(card, indent=1, default=str)}\n</scorecard>\n\n"
            f"<ncr_records count=\"{len(full)}\">\n"
            f"{json.dumps([{k: r.get(k) for k in keep} for r in full], indent=1)}\n</ncr_records>\n\n"
            f"Write the risk brief for {d['supplier_name']} ({sid})."
        )
        markdown = converse_text(bedrock, MODEL_ID, SYSTEM, prompt, max_tokens=1800)
        brief = {
            "supplier_id": sid,
            "markdown": markdown,
            "risk": d["risk"],
            "reasons": d["reasons"],
            "ncr_count": len(full),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "requested_by": event.get("requested_by"),
            "model": MODEL_ID,
            "elapsed_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        }
    except Exception as err:
        log.exception("brief failed for %s", sid)
        brief = {"supplier_id": sid, "error": f"{type(err).__name__}: {err}"[:500],
                 "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    s3.put_object(Bucket=LAKE, Key=key, Body=json.dumps(brief).encode(),
                  ContentType="application/json")
    return {k: brief.get(k) for k in ("supplier_id", "generated_at", "error")}
