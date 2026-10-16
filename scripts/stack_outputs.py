"""Read CloudFormation outputs of the deployed Sqc* stacks."""
import boto3

STACKS = ["SqcData", "SqcKnowledge", "SqcIngest", "SqcAgent", "SqcApp"]


def outputs(region: str | None = None) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=region)
    result = {}
    for stack in STACKS:
        try:
            desc = cfn.describe_stacks(StackName=stack)["Stacks"][0]
        except cfn.exceptions.ClientError:
            continue
        for out in desc.get("Outputs", []):
            result[out["OutputKey"]] = out["OutputValue"]
    return result
