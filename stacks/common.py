"""Shared CDK helpers."""
from pathlib import Path

from aws_cdk import Aws, Duration, RemovalPolicy, aws_lambda as _lambda, aws_logs as logs
from constructs import Construct

LAMBDA_DIR = str(Path(__file__).resolve().parent.parent / "lambdas")


def lambda_code() -> _lambda.Code:
    """All handlers ship from one small asset so they can share ``lambdas/common``."""
    return _lambda.Code.from_asset(LAMBDA_DIR, exclude=["**/__pycache__", "**/*.pyc"])


def python_function(scope: Construct, name: str, handler: str, *, timeout: int = 60,
                    memory: int = 512, env: dict | None = None) -> _lambda.Function:
    return _lambda.Function(
        scope,
        name,
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler=handler,
        code=lambda_code(),
        timeout=Duration.seconds(timeout),
        memory_size=memory,
        environment=env or {},
        log_group=logs.LogGroup(
            scope,
            f"{name}Logs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        ),
    )


def bedrock_model_resources(model_id: str) -> list[str]:
    """IAM resources for an on-demand model or a cross-region inference profile.

    ``us.anthropic.claude-...`` is a cross-region inference profile: callers need the
    profile ARN plus the underlying foundation model in every region it routes to.
    """
    is_profile = model_id.count(".") > 1
    base = model_id.split(".", 1)[1] if is_profile else model_id
    resources = [f"arn:{Aws.PARTITION}:bedrock:*::foundation-model/{base}"]
    if is_profile:
        resources.append(
            f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}:{Aws.ACCOUNT_ID}:inference-profile/{model_id}"
        )
    return resources
