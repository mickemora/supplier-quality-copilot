"""Step 3: write the record to the lake, the KB corpus, and the signature index."""
import json
import os
import re
from datetime import datetime, timezone

import boto3

from common.bedrock import embed
from common.similarity import signature_text

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")

LAKE_BUCKET = os.environ["LAKE_BUCKET"]
EMBEDDING_MODEL_ID = os.environ["EMBEDDING_MODEL_ID"]

KB_METADATA_FIELDS = [
    "ncr_id", "supplier_id", "supplier_name", "part_number", "commodity", "program",
    "root_cause_category", "severity", "status", "detected_at",
]
SIGNATURE_FIELDS = [
    "ncr_id", "supplier_id", "supplier_name", "part_number", "commodity", "defect_code",
    "defect_description", "root_cause_category", "root_cause_detail", "detected_at", "status",
]


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", value or "").strip("-")
    return cleaned or "unknown"


def handler(event, _context):
    record = event["record"]
    text = event["extracted"]["text"]
    ncr_key = safe_id(record["ncr_id"])

    # Curated row for Athena (one JSON object per line).
    s3.put_object(
        Bucket=LAKE_BUCKET,
        Key=f"curated/ncr/{ncr_key}.json",
        Body=(json.dumps(record) + "\n").encode(),
        ContentType="application/json",
    )

    # Knowledge base document + metadata sidecar for filtered retrieval.
    kb_key = f"kb/{ncr_key}.txt"
    s3.put_object(Bucket=LAKE_BUCKET, Key=kb_key, Body=text.encode(),
                  ContentType="text/plain")
    metadata = {k: str(record.get(k, "")) for k in KB_METADATA_FIELDS}
    s3.put_object(
        Bucket=LAKE_BUCKET,
        Key=f"{kb_key}.metadata.json",
        Body=json.dumps({"metadataAttributes": metadata}).encode(),
        ContentType="application/json",
    )

    # Root-cause signature vector for cross-supplier pattern detection.
    signature = signature_text(record)
    vector = embed(bedrock, EMBEDDING_MODEL_ID, signature)
    s3.put_object(
        Bucket=LAKE_BUCKET,
        Key=f"embeddings/{ncr_key}.json",
        Body=json.dumps({**{k: record.get(k) for k in SIGNATURE_FIELDS},
                         "signature": signature, "vector": vector}).encode(),
        ContentType="application/json",
    )

    return {"ncr_id": record["ncr_id"],
            "persisted_at": datetime.now(timezone.utc).isoformat()}
