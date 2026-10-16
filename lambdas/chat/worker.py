"""Async chat worker: runs one agent turn and stores the result in DynamoDB.

Invoked asynchronously by the web API with {job_id, session_id, question}.
"""
import json
import logging
import os
import time

import boto3

from common.agent_stream import collect

log = logging.getLogger()
log.setLevel(logging.INFO)

runtime = boto3.client("bedrock-agent-runtime")
table = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
AGENT_ID = os.environ["AGENT_ID"]
AGENT_ALIAS_ID = os.environ["AGENT_ALIAS_ID"]


def _update(job_id: str, **fields) -> None:
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def handler(event, _context):
    job_id = event["job_id"]
    started = time.time()
    _update(job_id, status="RUNNING")
    try:
        response = runtime.invoke_agent(
            agentId=AGENT_ID,
            agentAliasId=AGENT_ALIAS_ID,
            sessionId=event["session_id"],
            inputText=event["question"],
            enableTrace=True,
        )
        result = collect(response)
        _update(job_id, status="DONE", result=json.dumps(result),
                elapsed_ms=int((time.time() - started) * 1000))
    except Exception as err:
        log.exception("agent call failed")
        _update(job_id, status="ERROR", error=f"{type(err).__name__}: {err}"[:1000],
                elapsed_ms=int((time.time() - started) * 1000))
