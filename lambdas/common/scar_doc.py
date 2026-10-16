"""Read and write SCAR draft markdown files with a simple front-matter header.

    ---
    title: "Thin gold plating on contacts"
    supplier_id: SUP-0112
    ncr_ids: ["NCR-2026-0039"]
    status: PENDING_REVIEW
    ---

    # SCAR body ...
"""
import json

PENDING = "PENDING_REVIEW"
APPROVED = "APPROVED"
REJECTED = "REJECTED"


def parse(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        raw = raw.strip()
        if raw[:1] in ('"', "["):
            try:
                meta[key.strip()] = json.loads(raw)
                continue
            except json.JSONDecodeError:
                pass
        meta[key.strip()] = raw
    return meta, text[end + 5:].lstrip("\n")


def _value(v) -> str:
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    s = str(v)
    if s != s.strip() or any(c in s for c in ':"#[\n') or s == "":
        return json.dumps(s)
    return s


def render(meta: dict, body: str) -> str:
    header = "\n".join(f"{k}: {_value(v)}" for k, v in meta.items())
    return f"---\n{header}\n---\n\n{body.rstrip()}\n"
