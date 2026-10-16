#!/usr/bin/env python3
"""CDK entry point for the Supplier Quality Copilot demo."""
import os

import aws_cdk as cdk

from stacks.data_stack import DataStack
from stacks.ingest_stack import IngestStack
from stacks.knowledge_stack import KnowledgeStack
from stacks.agent_stack import AgentStack
from stacks.app_stack import AppStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)
prefix = app.node.try_get_context("prefix") or "sqc"

data = DataStack(app, "SqcData", prefix=prefix, env=env)

knowledge = KnowledgeStack(app, "SqcKnowledge", prefix=prefix, data=data, env=env)

ingest = IngestStack(
    app, "SqcIngest", prefix=prefix, data=data, knowledge=knowledge, env=env
)

agent = AgentStack(
    app, "SqcAgent", prefix=prefix, data=data, knowledge=knowledge, env=env
)

AppStack(app, "SqcApp", prefix=prefix, agent=agent, env=env)

cdk.Tags.of(app).add("Project", "supplier-quality-copilot")
cdk.Tags.of(app).add("DataClassification", "synthetic")

app.synth()
