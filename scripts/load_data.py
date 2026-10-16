#!/usr/bin/env python3
"""Load synthetic data into the deployed stacks.

    python scripts/load_data.py seed        # supplier master + all seed NCRs, then run detector
    python scripts/load_data.py demo        # upload the live-demo NCR, then run detector
    python scripts/load_data.py detect      # just run the repeat root-cause detector

Each NCR uploaded to ``incoming/`` triggers the ingest state machine. The script waits
for the executions to finish before running the detector.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stack_outputs import outputs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def upload_ncrs(s3, bucket: str, files: list[Path]) -> None:
    for i, path in enumerate(files, 1):
        s3.upload_file(str(path), bucket, f"incoming/{path.name}")
        if i % 10 == 0 or i == len(files):
            print(f"  uploaded {i}/{len(files)}")
        time.sleep(0.2)  # gentle on Bedrock on-demand quotas


def wait_for_ingest(sfn, state_machine_arn: str, timeout_s: int = 3600) -> None:
    print("Waiting for ingest executions to finish", end="", flush=True)
    time.sleep(10)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        running = sfn.list_executions(stateMachineArn=state_machine_arn,
                                      statusFilter="RUNNING", maxResults=100)["executions"]
        if not running:
            break
        print(".", end="", flush=True)
        time.sleep(15)
    print()
    failed = sfn.list_executions(stateMachineArn=state_machine_arn,
                                 statusFilter="FAILED", maxResults=100)["executions"]
    if failed:
        print(f"WARNING: {len(failed)} failed execution(s). Check the Step Functions console.")


def run_detector(lam, function_name: str) -> None:
    # Evaluate the "recent window" as of the date the synthetic data was generated,
    # so the storyline still works if you deploy weeks later.
    manifest = json.loads((DATA / "seed" / "manifest.json").read_text())
    payload = {"today": manifest["as_of"]}
    print(f"Running repeat root-cause detector as of {payload['today']}...")
    resp = lam.invoke(FunctionName=function_name, Payload=json.dumps(payload).encode())
    print(json.dumps(json.loads(resp["Payload"].read()), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["seed", "demo", "detect"])
    parser.add_argument("--region")
    args = parser.parse_args()

    out = outputs(args.region)
    needed = ["DocsBucketName", "LakeBucketName", "IngestStateMachineArn", "DetectorFunctionName"]
    missing = [k for k in needed if k not in out]
    if missing:
        sys.exit(f"Missing stack outputs {missing}. Deploy with `cdk deploy --all` first.")

    session = boto3.session.Session(region_name=args.region)
    s3, sfn, lam = session.client("s3"), session.client("stepfunctions"), session.client("lambda")

    if args.action == "seed":
        s3.upload_file(str(DATA / "seed" / "suppliers.csv"), out["LakeBucketName"],
                       "curated/suppliers/suppliers.csv")
        print("Uploaded supplier master")
        files = sorted((DATA / "seed" / "ncr").glob("*.txt"))
        print(f"Uploading {len(files)} seed NCRs")
        upload_ncrs(s3, out["DocsBucketName"], files)
        wait_for_ingest(sfn, out["IngestStateMachineArn"])
    elif args.action == "demo":
        files = sorted((DATA / "demo").glob("*.txt"))
        print(f"Uploading demo NCR {files[0].name}")
        upload_ncrs(s3, out["DocsBucketName"], files)
        wait_for_ingest(sfn, out["IngestStateMachineArn"])

    run_detector(lam, out["DetectorFunctionName"])


if __name__ == "__main__":
    main()
