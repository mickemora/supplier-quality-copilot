"""POST /chat  {"question": "...", "sessionId": "optional"}  ->  answer + citations."""
import json
import os
import uuid

import boto3

from common.agent_stream import collect

runtime = boto3.client("bedrock-agent-runtime")
AGENT_ID = os.environ["AGENT_ID"]
AGENT_ALIAS_ID = os.environ["AGENT_ALIAS_ID"]

HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}


def _reply(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": HEADERS, "body": json.dumps(body)}


def handler(event, _context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _reply(400, {"error": "Body must be JSON"})

    question = (body.get("question") or "").strip()
    if not question:
        return _reply(400, {"error": "question is required"})
    if len(question) > 4000:
        return _reply(400, {"error": "question is too long"})

    claims = (event.get("requestContext", {}).get("authorizer", {}) or {}).get("claims", {})
    # Scope sessions to the signed-in user so one user can't resume another's session.
    user = claims.get("sub", "anonymous")
    session_id = body.get("sessionId") or str(uuid.uuid4())
    if not session_id.startswith(user[:8]):
        session_id = f"{user[:8]}-{session_id}"[:100]

    response = runtime.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=AGENT_ALIAS_ID,
        sessionId=session_id,
        inputText=question,
        enableTrace=False,
    )
    return _reply(200, {"sessionId": session_id, **collect(response)})
