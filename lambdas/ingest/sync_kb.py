"""Step 4: make sure a knowledge base ingestion job covers this document.

Ingestion jobs are incremental and only one can run per data source. When many
documents arrive together (e.g. the seed load), each execution checks whether a
job started after its document was written; if so it is covered. Otherwise it
starts one, or raises ``IngestionJobBusy`` so Step Functions retries later.
"""
import os
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

agent = boto3.client("bedrock-agent")
KB_ID = os.environ["KNOWLEDGE_BASE_ID"]
DS_ID = os.environ["DATA_SOURCE_ID"]

ACTIVE = {"STARTING", "IN_PROGRESS"}


class IngestionJobBusy(Exception):
    pass


def handler(event, _context):
    persisted_at = datetime.fromisoformat(event["persisted"]["persisted_at"])

    jobs = agent.list_ingestion_jobs(
        knowledgeBaseId=KB_ID,
        dataSourceId=DS_ID,
        sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        maxResults=10,
    ).get("ingestionJobSummaries", [])

    for job in jobs:
        if job["startedAt"] > persisted_at and job["status"] in ACTIVE | {"COMPLETE"}:
            return {"status": "covered", "ingestion_job_id": job["ingestionJobId"]}

    if any(job["status"] in ACTIVE for job in jobs):
        raise IngestionJobBusy("An ingestion job is running; will retry")

    try:
        job = agent.start_ingestion_job(knowledgeBaseId=KB_ID, dataSourceId=DS_ID)
    except ClientError as err:
        if err.response["Error"]["Code"] in {"ConflictException", "ThrottlingException"}:
            raise IngestionJobBusy(str(err)) from err
        raise
    return {"status": "started", "ingestion_job_id": job["ingestionJob"]["ingestionJobId"]}
