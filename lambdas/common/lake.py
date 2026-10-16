"""Read helpers for the lake bucket."""
import json

EMBEDDINGS_PREFIX = "embeddings/"
ALERTS_KEY = "curated/alerts/latest.json"

# Warm-Lambda cache: {key: (etag, item)}
_cache: dict[str, tuple[str, dict]] = {}


def load_embeddings(s3, bucket: str) -> list[dict]:
    """Load every NCR signature vector, re-downloading only objects that changed."""
    items, seen = [], set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=EMBEDDINGS_PREFIX):
        for obj in page.get("Contents", []):
            key, etag = obj["Key"], obj["ETag"]
            seen.add(key)
            cached = _cache.get(key)
            if cached and cached[0] == etag:
                items.append(cached[1])
                continue
            item = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
            _cache[key] = (etag, item)
            items.append(item)
    for stale in set(_cache) - seen:
        _cache.pop(stale, None)
    return items


def load_alerts(s3, bucket: str) -> list[dict]:
    try:
        body = s3.get_object(Bucket=bucket, Key=ALERTS_KEY)["Body"].read().decode()
    except s3.exceptions.NoSuchKey:
        return []
    return [json.loads(line) for line in body.splitlines() if line.strip()]
