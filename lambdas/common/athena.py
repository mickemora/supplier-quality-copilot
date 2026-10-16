"""Run an Athena query and return rows as dicts."""
import time


class QueryFailed(Exception):
    pass


def run_query(athena, sql: str, workgroup: str, database: str,
              max_rows: int = 50, timeout_s: int = 90) -> dict:
    qid = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=workgroup,
        QueryExecutionContext={"Database": database},
    )["QueryExecutionId"]

    deadline = time.time() + timeout_s
    delay = 0.5
    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            break
        if state in {"FAILED", "CANCELLED"}:
            raise QueryFailed(status.get("StateChangeReason", state))
        if time.time() > deadline:
            athena.stop_query_execution(QueryExecutionId=qid)
            raise QueryFailed("Query timed out")
        time.sleep(delay)
        delay = min(delay * 1.5, 3)

    result = athena.get_query_results(QueryExecutionId=qid, MaxResults=max_rows + 1)
    rows = result["ResultSet"]["Rows"]
    columns = [c["Name"] for c in result["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]]
    data = [[cell.get("VarCharValue") for cell in row["Data"]] for row in rows[1:]]
    return {
        "columns": columns,
        "rows": [dict(zip(columns, r)) for r in data[:max_rows]],
        "truncated": len(data) > max_rows or "NextToken" in result,
    }
