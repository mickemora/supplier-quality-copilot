"""Step 2: turn free-text NCR / 8D / SCAR into a structured record with Claude."""
import os

import boto3

from common.bedrock import converse_tool
from ingest.schema import NCR_SCHEMA

bedrock = boto3.client("bedrock-runtime")
MODEL_ID = os.environ["EXTRACTION_MODEL_ID"]

SYSTEM = (
    "You extract supplier quality records for an aerospace and defense supply chain team. "
    "Read the document and fill every field you can from what it states. Do not invent facts: "
    "use 'unknown' for text fields and 0 for quantities that are not stated. If the root cause "
    "investigation is not finished, give the suspected cause and say it is suspected. "
    "Pick the root_cause_category using the 6M framework (Material, Method, Machine, "
    "Measurement, Man, Environment) or Design."
)


def handler(event, _context):
    extracted = event["extracted"]
    record = converse_tool(
        bedrock,
        MODEL_ID,
        SYSTEM,
        f"Document file name: {extracted['file_name']}\n\n<document>\n{extracted['text']}\n</document>",
        tool_name="record_nonconformance",
        tool_description="Record the structured nonconformance extracted from the document.",
        schema=NCR_SCHEMA,
    )
    record["quantity_affected"] = int(record.get("quantity_affected") or 0)
    for key in ("response_due_at", "response_received_at", "closed_at"):
        value = str(record.get(key) or "").strip()
        record[key] = value if len(value) == 10 and value[4] == "-" else ""
    record["source_document"] = extracted["source_uri"]
    return record
