#!/usr/bin/env python3
"""Chat with the deployed copilot from your terminal (uses your AWS credentials).

    python scripts/ask.py                       # interactive
    python scripts/ask.py "Which suppliers have the most critical NCRs this year?"
"""
import argparse
import sys
import uuid
from pathlib import Path

import boto3

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lambdas"))
from common.agent_stream import collect  # noqa: E402
from stack_outputs import outputs  # noqa: E402


def ask(runtime, agent_id: str, alias_id: str, session_id: str, question: str) -> None:
    resp = runtime.invoke_agent(agentId=agent_id, agentAliasId=alias_id,
                                sessionId=session_id, inputText=question)
    result = collect(resp)
    print("\n" + result["answer"] + "\n")
    if result["citations"]:
        print("Sources:")
        for c in result["citations"]:
            label = c.get("ncr_id") or c["uri"].rsplit("/", 1)[-1]
            print(f"  - {label} ({c.get('supplier_id') or 'n/a'})  {c['uri']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="*")
    parser.add_argument("--region")
    args = parser.parse_args()

    out = outputs(args.region)
    if "AgentId" not in out:
        sys.exit("Agent not deployed. Run `cdk deploy --all` first.")
    runtime = boto3.client("bedrock-agent-runtime", region_name=args.region)
    session_id = str(uuid.uuid4())

    if args.question:
        ask(runtime, out["AgentId"], out["AgentAliasId"], session_id, " ".join(args.question))
        return

    print("Supplier Quality Copilot. Ctrl-D to exit.")
    while True:
        try:
            q = input("you> ").strip()
        except EOFError:
            print()
            break
        if q:
            ask(runtime, out["AgentId"], out["AgentAliasId"], session_id, q)


if __name__ == "__main__":
    main()
