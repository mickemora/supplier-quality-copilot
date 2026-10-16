"""Collect a Bedrock agent streaming response into answer, citations and trace steps."""
import re

NCR_FROM_URI = re.compile(r"/(NCR-[\w-]+?)\.txt$")
MAX_TEXT = 600


def _ncr_id(uri: str, meta: dict) -> str | None:
    if meta.get("ncr_id"):
        return meta["ncr_id"]
    m = NCR_FROM_URI.search(uri or "")
    return m.group(1) if m else None


def _short(text, limit: int = MAX_TEXT) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _trace_steps(trace: dict) -> list[dict]:
    """Turn one orchestration trace event into zero or more readable steps."""
    orch = trace.get("orchestrationTrace") or {}
    steps = []
    if "rationale" in orch:
        steps.append({"kind": "thinking", "text": _short(orch["rationale"].get("text"))})
    inv = orch.get("invocationInput") or {}
    if "actionGroupInvocationInput" in inv:
        ag = inv["actionGroupInvocationInput"]
        params = {p.get("name"): p.get("value") for p in ag.get("parameters", [])}
        steps.append({"kind": "tool", "name": ag.get("function"), "input": params})
    if "knowledgeBaseLookupInput" in inv:
        steps.append({"kind": "kb_search", "query": _short(inv["knowledgeBaseLookupInput"].get("text"))})
    obs = orch.get("observation") or {}
    if "actionGroupInvocationOutput" in obs:
        steps.append({"kind": "tool_result",
                      "text": _short(obs["actionGroupInvocationOutput"].get("text"))})
    if "knowledgeBaseLookupOutput" in obs:
        refs = obs["knowledgeBaseLookupOutput"].get("retrievedReferences", [])
        ids = []
        for ref in refs:
            nid = _ncr_id(ref.get("location", {}).get("s3Location", {}).get("uri"),
                          ref.get("metadata", {}))
            if nid and nid not in ids:
                ids.append(nid)
        steps.append({"kind": "kb_result", "count": len(refs), "ncr_ids": ids})
    guard = trace.get("guardrailTrace") or {}
    if guard.get("action") == "INTERVENED":
        steps.append({"kind": "guardrail", "text": "Guardrail intervened"})
    return steps


def collect(response) -> dict:
    answer_parts, citations, steps, seen = [], [], [], set()
    for event in response["completion"]:
        if "trace" in event:
            steps.extend(_trace_steps(event["trace"].get("trace", {})))
            continue
        chunk = event.get("chunk")
        if not chunk:
            continue
        answer_parts.append(chunk.get("bytes", b"").decode("utf-8"))
        for citation in chunk.get("attribution", {}).get("citations", []):
            for ref in citation.get("retrievedReferences", []):
                uri = ref.get("location", {}).get("s3Location", {}).get("uri")
                if not uri or uri in seen:
                    continue
                seen.add(uri)
                meta = ref.get("metadata", {})
                citations.append({
                    "uri": uri,
                    "ncr_id": _ncr_id(uri, meta),
                    "supplier_id": meta.get("supplier_id"),
                    "excerpt": (ref.get("content", {}).get("text") or "")[:300],
                })
    return {"answer": "".join(answer_parts).strip(), "citations": citations, "steps": steps}
