"""Thin Bedrock helpers shared by the Lambdas.

Retryable service errors are re-raised as ``ThrottlingException`` so Step Functions
can retry on the exception name.
"""
import json

from botocore.exceptions import ClientError

RETRYABLE_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "ModelNotReadyException",
    "InternalServerException",
    "ModelTimeoutException",
}


class ThrottlingException(Exception):
    """Raised for Bedrock errors worth retrying."""


def _call(fn, **kwargs):
    try:
        return fn(**kwargs)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in RETRYABLE_CODES:
            raise ThrottlingException(str(err)) from err
        raise


def converse_tool(client, model_id: str, system: str, user_text: str, tool_name: str,
                  tool_description: str, schema: dict, max_tokens: int = 2000) -> dict:
    """Call a model with one tool and force it, returning the tool input as a dict."""
    response = _call(
        client.converse,
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
        toolConfig={
            "tools": [{"toolSpec": {
                "name": tool_name,
                "description": tool_description,
                "inputSchema": {"json": schema},
            }}],
            "toolChoice": {"tool": {"name": tool_name}},
        },
    )
    for block in response["output"]["message"]["content"]:
        if "toolUse" in block:
            return block["toolUse"]["input"]
    raise ValueError(f"Model did not call {tool_name}")


def embed(client, model_id: str, text: str, dimensions: int = 1024) -> list[float]:
    """Titan Text Embeddings V2, normalized."""
    response = _call(
        client.invoke_model,
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({"inputText": text[:8000], "dimensions": dimensions, "normalize": True}),
    )
    return json.loads(response["body"].read())["embedding"]
