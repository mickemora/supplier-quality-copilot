"""Step 1: get plain text out of an uploaded quality document."""
import urllib.parse

import boto3

s3 = boto3.client("s3")
textract = boto3.client("textract")

MAX_CHARS = 60_000
TEXT_TYPES = {"txt", "md"}
TEXTRACT_TYPES = {"pdf", "png", "jpg", "jpeg", "tif", "tiff"}


class UnsupportedDocument(Exception):
    pass


def _get_object(bucket: str, key: str):
    try:
        return key, s3.get_object(Bucket=bucket, Key=key)
    except s3.exceptions.NoSuchKey:
        decoded = urllib.parse.unquote_plus(key)
        return decoded, s3.get_object(Bucket=bucket, Key=decoded)


def handler(event, _context):
    bucket, key = event["bucket"], event["key"]
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""

    if ext in TEXT_TYPES:
        key, obj = _get_object(bucket, key)
        text = obj["Body"].read().decode("utf-8", errors="replace")
    elif ext in TEXTRACT_TYPES:
        # Synchronous Textract handles images and single-page PDFs. Multi-page
        # scans would use StartDocumentTextDetection (async) instead.
        resp = textract.detect_document_text(
            Document={"S3Object": {"Bucket": bucket, "Name": key}}
        )
        text = "\n".join(b["Text"] for b in resp["Blocks"] if b["BlockType"] == "LINE")
    else:
        raise UnsupportedDocument(f"Unsupported file type: {key}")

    return {
        "text": text[:MAX_CHARS],
        "truncated": len(text) > MAX_CHARS,
        "source_uri": f"s3://{bucket}/{key}",
        "file_name": key.rsplit("/", 1)[-1],
    }
