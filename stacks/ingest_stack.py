"""Ingest pipeline: document lands in S3 -> Step Functions -> structured NCR record.

    incoming/*  --EventBridge-->  ExtractText        plain text, or Textract for PDF/images
                                 StructureRecord    Claude on Bedrock, forced JSON tool schema
                                 PersistRecord      curated/ncr JSON + kb/ text + metadata
                                                    + root-cause signature embedding
                                 SyncKnowledgeBase  debounced KB ingestion job
"""
from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    Stack,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
)
from constructs import Construct

from stacks.common import bedrock_model_resources, python_function
from stacks.data_stack import DataStack
from stacks.knowledge_stack import KnowledgeStack


class IngestStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, prefix: str,
                 data: DataStack, knowledge: KnowledgeStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        extraction_model = self.node.try_get_context("extractionModelId")
        embedding_model = self.node.try_get_context("embeddingModelId")
        base_env = {"LAKE_BUCKET": data.lake_bucket.bucket_name}

        extract_fn = python_function(self, "ExtractTextFn", "ingest.extract_text.handler",
                                     timeout=120, env=base_env)
        data.docs_bucket.grant_read(extract_fn)
        extract_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["textract:DetectDocumentText"], resources=["*"]
        ))

        structure_fn = python_function(
            self, "StructureRecordFn", "ingest.structure_record.handler", timeout=180,
            env={**base_env, "EXTRACTION_MODEL_ID": extraction_model},
        )
        structure_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=bedrock_model_resources(extraction_model),
        ))

        persist_fn = python_function(
            self, "PersistRecordFn", "ingest.persist_record.handler", timeout=60,
            env={**base_env, "EMBEDDING_MODEL_ID": embedding_model},
        )
        data.lake_bucket.grant_put(persist_fn)
        persist_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=bedrock_model_resources(embedding_model),
        ))

        sync_fn = python_function(self, "SyncKnowledgeBaseFn", "ingest.sync_kb.handler",
                                  timeout=30, env={
                                      "KNOWLEDGE_BASE_ID": knowledge.knowledge_base_id,
                                      "DATA_SOURCE_ID": knowledge.data_source_id,
                                  })
        sync_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:StartIngestionJob", "bedrock:ListIngestionJobs"],
            resources=[knowledge.knowledge_base_arn],
        ))

        bedrock_retry = dict(
            errors=["ThrottlingException", "Lambda.TooManyRequestsException",
                    "Lambda.ServiceException"],
            interval=Duration.seconds(5),
            backoff_rate=2.0,
            max_attempts=6,
            jitter_strategy=sfn.JitterType.FULL,
        )

        extract = tasks.LambdaInvoke(self, "ExtractText", lambda_function=extract_fn,
                                     payload_response_only=True, result_path="$.extracted")
        structure = tasks.LambdaInvoke(self, "StructureRecord", lambda_function=structure_fn,
                                       payload_response_only=True, result_path="$.record")
        structure.add_retry(**bedrock_retry)
        persist = tasks.LambdaInvoke(self, "PersistRecord", lambda_function=persist_fn,
                                     payload_response_only=True, result_path="$.persisted")
        persist.add_retry(**bedrock_retry)
        sync = tasks.LambdaInvoke(self, "SyncKnowledgeBase", lambda_function=sync_fn,
                                  payload_response_only=True, result_path="$.sync")
        # The sync Lambda raises IngestionJobBusy while another job is running and
        # hasn't picked up this document yet; wait it out instead of failing.
        sync.add_retry(errors=["IngestionJobBusy"], interval=Duration.seconds(30),
                       backoff_rate=1.0, max_attempts=40)

        definition = (extract.next(structure).next(persist).next(sync)
                      .next(sfn.Succeed(self, "Done")))

        self.state_machine = sfn.StateMachine(
            self,
            "IngestStateMachine",
            state_machine_name=f"{prefix}-ingest",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.minutes(45),
            tracing_enabled=True,
        )

        events.Rule(
            self,
            "DocumentUploadedRule",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [data.docs_bucket.bucket_name]},
                    "object": {"key": [{"prefix": "incoming/"}]},
                },
            ),
            targets=[targets.SfnStateMachine(
                self.state_machine,
                input=events.RuleTargetInput.from_object({
                    "bucket": events.EventField.from_path("$.detail.bucket.name"),
                    "key": events.EventField.from_path("$.detail.object.key"),
                }),
            )],
        )

        CfnOutput(self, "IngestStateMachineArn", value=self.state_machine.state_machine_arn)
        CfnOutput(self, "Region", value=Aws.REGION)
