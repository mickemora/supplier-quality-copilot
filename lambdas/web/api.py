"""Web API behind API Gateway (Cognito-authorized, proxy integration).

Routes
  GET  /me                    signed-in user and reviewer flag
  POST /chat                  submit a question -> {jobId, sessionId}   (async)
  GET  /chat/{jobId}          poll for the answer, citations and agent steps
  GET  /alerts                cross-supplier patterns + last update time
  POST /alerts/run            start the detector (async)
  POST /uploads               presigned PUT URL for a new quality document
  GET  /uploads/status?key=   ingest pipeline progress for an uploaded document
  GET  /ncrs/{ncrId}          structured record + source text
  GET  /scars                 SCAR drafts
  GET  /scars/item?key=       one draft
  PUT  /scars/item            edit a pending draft {key, markdown}
  POST /scars/decision        approve/reject {key, decision, comment}   (reviewers group)
"""
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from common import scar_doc
from common.lake import ALERTS_KEY, load_alerts

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
s3 = boto3.client("s3", region_name=REGION,
                  config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
sfn = boto3.client("stepfunctions")
lam = boto3.client("lambda")
jobs = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])

LAKE = os.environ["LAKE_BUCKET"]
DOCS = os.environ["DOCS_BUCKET"]
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
WORKER_FN = os.environ["CHAT_WORKER_FUNCTION"]
DETECTOR_FN = os.environ["DETECTOR_FUNCTION"]
REVIEWER_GROUP = os.environ.get("REVIEWER_GROUP", "reviewers")

UPLOAD_TYPES = {
    "txt": "text/plain", "md": "text/markdown", "pdf": "application/pdf",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
}
NCR_ID = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
SCAR_KEY = re.compile(r"^scar-drafts/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.md$")
PIPELINE_STEPS = ["ExtractText", "StructureRecord", "PersistRecord", "SyncKnowledgeBase"]

HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def reply(status: int, body) -> dict:
    return {"statusCode": status, "headers": HEADERS, "body": json.dumps(body, default=_json_default)}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- identity
class User:
    def __init__(self, event: dict):
        claims = (event.get("requestContext", {}).get("authorizer") or {}).get("claims") or {}
        self.sub = claims.get("sub", "anonymous")
        self.email = claims.get("email") or claims.get("cognito:username") or self.sub
        groups = claims.get("cognito:groups") or ""
        if isinstance(groups, list):
            names = groups
        else:
            names = re.split(r"[\s,]+", groups.strip("[]"))
        self.is_reviewer = REVIEWER_GROUP in names


def _body(event) -> dict:
    try:
        return json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        raise HttpError(400, "Body must be JSON")


def _query(event, name: str) -> str:
    return ((event.get("queryStringParameters") or {}).get(name) or "").strip()


# ---------------------------------------------------------------- chat
def me(event, user):
    return reply(200, {"email": user.email, "isReviewer": user.is_reviewer})


def submit_chat(event, user):
    body = _body(event)
    question = (body.get("question") or "").strip()
    if not question:
        raise HttpError(400, "question is required")
    if len(question) > 4000:
        raise HttpError(400, "question is too long")
    prefix = user.sub[:8]
    session_id = (body.get("sessionId") or "").strip() or f"{prefix}-{uuid.uuid4()}"
    if not session_id.startswith(prefix):  # sessions are scoped to the signed-in user
        session_id = f"{prefix}-{session_id}"
    session_id = re.sub(r"[^A-Za-z0-9_.:-]", "-", session_id)[:100]

    job_id = str(uuid.uuid4())
    jobs.put_item(Item={
        "job_id": job_id, "user_sub": user.sub, "status": "PENDING", "question": question,
        "session_id": session_id, "created_at": now_iso(), "expires_at": int(time.time()) + 86400,
    })
    lam.invoke(FunctionName=WORKER_FN, InvocationType="Event",
               Payload=json.dumps({"job_id": job_id, "session_id": session_id,
                                   "question": question}).encode())
    return reply(202, {"jobId": job_id, "sessionId": session_id})


def get_chat(event, user, job_id: str):
    item = jobs.get_item(Key={"job_id": job_id}).get("Item")
    if not item or item.get("user_sub") != user.sub:
        raise HttpError(404, "Unknown job")
    out = {"jobId": job_id, "status": item["status"], "elapsedMs": item.get("elapsed_ms")}
    if item["status"] == "DONE":
        out.update(json.loads(item["result"]))
    if item["status"] == "ERROR":
        out["error"] = item.get("error")
    return reply(200, out)


# ---------------------------------------------------------------- alerts
def get_alerts(event, user):
    try:
        updated = s3.head_object(Bucket=LAKE, Key=ALERTS_KEY)["LastModified"].isoformat()
    except ClientError:
        updated = None
    return reply(200, {"alerts": load_alerts(s3, LAKE), "updatedAt": updated})


def run_detector(event, user):
    lam.invoke(FunctionName=DETECTOR_FN, InvocationType="Event", Payload=b"{}")
    return reply(202, {"started": True, "startedAt": now_iso()})


# ---------------------------------------------------------------- uploads
def create_upload(event, user):
    name = (_body(event).get("fileName") or "").strip()
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in UPLOAD_TYPES:
        raise HttpError(400, f"Supported types: {', '.join(sorted(UPLOAD_TYPES))}")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", name)[-80:]
    key = f"incoming/{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{safe}"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": DOCS, "Key": key, "ContentType": UPLOAD_TYPES[ext]},
        ExpiresIn=300,
    )
    log.info("upload by %s -> %s", user.email, key)
    return reply(200, {"url": url, "key": key, "contentType": UPLOAD_TYPES[ext]})


def upload_status(event, user):
    key = _query(event, "key")
    if not key.startswith("incoming/"):
        raise HttpError(400, "key is required")
    executions = sfn.list_executions(stateMachineArn=STATE_MACHINE_ARN, maxResults=30)["executions"]
    for ex in executions:
        detail = sfn.describe_execution(executionArn=ex["executionArn"])
        if json.loads(detail.get("input") or "{}").get("key") != key:
            continue
        status = detail["status"]
        out = {"found": True, "status": status, "steps": PIPELINE_STEPS, "step": None}
        if status == "RUNNING":
            history = sfn.get_execution_history(executionArn=ex["executionArn"],
                                                reverseOrder=True, maxResults=30)["events"]
            for e in history:
                name = (e.get("stateEnteredEventDetails") or {}).get("name")
                if name:
                    out["step"] = name
                    break
        elif status == "SUCCEEDED":
            output = json.loads(detail.get("output") or "{}")
            out["step"] = "Done"
            out["ncrId"] = (output.get("record") or {}).get("ncr_id")
            out["kbSync"] = (output.get("sync") or {}).get("status")
        else:
            out["error"] = detail.get("error") or detail.get("cause") or status
        return reply(200, out)
    return reply(200, {"found": False, "status": "QUEUED", "steps": PIPELINE_STEPS, "step": None})


# ---------------------------------------------------------------- NCRs
def get_ncr(event, user, ncr_id: str):
    if not NCR_ID.match(ncr_id):
        raise HttpError(400, "Bad NCR id")
    try:
        record = json.loads(s3.get_object(Bucket=LAKE, Key=f"curated/ncr/{ncr_id}.json")["Body"].read())
    except s3.exceptions.NoSuchKey:
        raise HttpError(404, f"{ncr_id} not found")
    try:
        text = s3.get_object(Bucket=LAKE, Key=f"kb/{ncr_id}.txt")["Body"].read().decode()
    except s3.exceptions.NoSuchKey:
        text = ""
    return reply(200, {"record": record, "text": text})


# ---------------------------------------------------------------- SCARs
def _load_scar(key: str) -> tuple[dict, str, str]:
    if not SCAR_KEY.match(key) or ".." in key:
        raise HttpError(400, "Bad SCAR key")
    try:
        obj = s3.get_object(Bucket=LAKE, Key=key)
    except s3.exceptions.NoSuchKey:
        raise HttpError(404, "SCAR not found")
    meta, body = scar_doc.parse(obj["Body"].read().decode())
    return meta, body, obj["ETag"]


def _save_scar(key: str, meta: dict, body: str) -> None:
    s3.put_object(Bucket=LAKE, Key=key, Body=scar_doc.render(meta, body).encode(),
                  ContentType="text/markdown")


def list_scars(event, user):
    items = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=LAKE, Prefix="scar-drafts/"):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".md"):
                continue
            meta, _ = scar_doc.parse(s3.get_object(Bucket=LAKE, Key=obj["Key"])["Body"].read().decode())
            items.append({"key": obj["Key"], **meta})
    items.sort(key=lambda i: i.get("drafted_at", ""), reverse=True)
    return reply(200, {"scars": items, "isReviewer": user.is_reviewer})


def get_scar(event, user):
    key = _query(event, "key")
    meta, body, _ = _load_scar(key)
    return reply(200, {"key": key, "meta": meta, "markdown": body, "isReviewer": user.is_reviewer})


def edit_scar(event, user):
    payload = _body(event)
    key, markdown = payload.get("key", ""), payload.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise HttpError(400, "markdown is required")
    meta, _, _ = _load_scar(key)
    if meta.get("status") != scar_doc.PENDING:
        raise HttpError(409, f"SCAR is {meta.get('status')}; only pending drafts can be edited")
    meta["last_edited_by"] = user.email
    meta["last_edited_at"] = now_iso()
    _save_scar(key, meta, markdown[:50_000])
    return reply(200, {"key": key, "meta": meta, "markdown": markdown})


def decide_scar(event, user):
    if not user.is_reviewer:
        raise HttpError(403, f"Only members of the '{REVIEWER_GROUP}' group can approve or reject")
    payload = _body(event)
    decision = (payload.get("decision") or "").lower()
    if decision not in {"approve", "reject"}:
        raise HttpError(400, "decision must be approve or reject")
    key = payload.get("key", "")
    meta, body, _ = _load_scar(key)
    if meta.get("status") != scar_doc.PENDING:
        raise HttpError(409, f"SCAR is already {meta.get('status')}")
    meta["status"] = scar_doc.APPROVED if decision == "approve" else scar_doc.REJECTED
    meta["reviewed_by"] = user.email
    meta["reviewed_at"] = now_iso()
    comment = (payload.get("comment") or "").strip()
    if comment:
        meta["review_comment"] = comment[:1000]
    _save_scar(key, meta, body)
    return reply(200, {"key": key, "meta": meta, "markdown": body})


# ---------------------------------------------------------------- router
ROUTES = [
    ("GET", re.compile(r"^/me$"), me),
    ("POST", re.compile(r"^/chat$"), submit_chat),
    ("GET", re.compile(r"^/chat/([A-Za-z0-9-]+)$"), get_chat),
    ("GET", re.compile(r"^/alerts$"), get_alerts),
    ("POST", re.compile(r"^/alerts/run$"), run_detector),
    ("POST", re.compile(r"^/uploads$"), create_upload),
    ("GET", re.compile(r"^/uploads/status$"), upload_status),
    ("GET", re.compile(r"^/ncrs/([^/]+)$"), get_ncr),
    ("GET", re.compile(r"^/scars$"), list_scars),
    ("GET", re.compile(r"^/scars/item$"), get_scar),
    ("PUT", re.compile(r"^/scars/item$"), edit_scar),
    ("POST", re.compile(r"^/scars/decision$"), decide_scar),
]


def handler(event, _context):
    method = event.get("httpMethod", "GET")
    path = (event.get("path") or "/").rstrip("/") or "/"
    user = User(event)
    try:
        for m, pattern, fn in ROUTES:
            match = pattern.match(path)
            if match and m == method:
                return fn(event, user, *match.groups())
        raise HttpError(404, f"No route for {method} {path}")
    except HttpError as err:
        return reply(err.status, {"error": str(err)})
    except Exception:
        log.exception("unhandled error on %s %s", method, path)
        return reply(500, {"error": "Internal error. Check the API Lambda logs."})
