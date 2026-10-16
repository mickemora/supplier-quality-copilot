"""Validate model-written SQL before it reaches Athena.

Defense in depth: the Lambda role can only read the supplier_quality tables anyway,
but we also reject anything that is not a single SELECT over allowed tables.
"""
import re

ALLOWED_TABLES = {"ncr", "suppliers", "alerts"}
DATABASE = "supplier_quality"
DEFAULT_LIMIT = 100

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|msck|unload|grant|revoke|call|prepare|"
    r"execute|merge|truncate|vacuum|optimize|repair|use|set)\b",
    re.IGNORECASE,
)
TABLE_REF = re.compile(r"\b(?:from|join)\s+([A-Za-z_][\w.\"]*)", re.IGNORECASE)
CTE_NAME = re.compile(r"(?:\bwith|,)\s*([A-Za-z_]\w*)\s+as\s*\(", re.IGNORECASE)
LIMIT_AT_END = re.compile(r"\blimit\s+\d+\s*$", re.IGNORECASE)


class UnsafeQuery(ValueError):
    pass


def _strip_literals_and_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def validate(sql: str) -> str:
    """Return a normalized, safe query or raise ``UnsafeQuery``."""
    if not sql or not sql.strip():
        raise UnsafeQuery("Empty query")
    query = sql.strip().rstrip(";").strip()
    skeleton = _strip_literals_and_comments(query)

    if ";" in skeleton:
        raise UnsafeQuery("Only one statement is allowed")
    if not re.match(r"^\s*(select|with)\b", skeleton, re.IGNORECASE):
        raise UnsafeQuery("Query must start with SELECT or WITH")
    bad = FORBIDDEN.search(skeleton)
    if bad:
        raise UnsafeQuery(f"Keyword not allowed: {bad.group(1).upper()}")

    ctes = {m.lower() for m in CTE_NAME.findall(skeleton)}
    for ref in TABLE_REF.findall(skeleton):
        name = ref.replace('"', "").lower()
        if name.startswith(DATABASE + "."):
            name = name[len(DATABASE) + 1:]
        if name in ALLOWED_TABLES or name in ctes or name == "unnest":
            continue
        raise UnsafeQuery(
            f"Table not allowed: {ref}. Use one of: {', '.join(sorted(ALLOWED_TABLES))}"
        )

    if not LIMIT_AT_END.search(skeleton):
        query = f"{query}\nLIMIT {DEFAULT_LIMIT}"
    return query
