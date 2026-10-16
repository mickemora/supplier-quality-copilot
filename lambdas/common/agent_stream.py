"""Collect a Bedrock agent streaming response into answer text plus citations."""


def collect(response) -> dict:
    answer_parts, citations, seen = [], [], set()
    for event in response["completion"]:
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
                    "ncr_id": meta.get("ncr_id"),
                    "supplier_id": meta.get("supplier_id"),
                    "excerpt": (ref.get("content", {}).get("text") or "")[:300],
                })
    return {"answer": "".join(answer_parts).strip(), "citations": citations}
