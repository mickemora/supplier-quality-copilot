"""Reasoning layer: Bedrock agent, its tools, guardrail, and the repeat root-cause detector."""
import hashlib
import json
from pathlib import Path

from aws_cdk import (
    Aws,
    CfnOutput,
    Stack,
    aws_bedrock as bedrock,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
)
from constructs import Construct

from stacks.common import bedrock_model_resources, python_function
from stacks.data_stack import DataStack
from stacks.knowledge_stack import KnowledgeStack

INSTRUCTION_PATH = Path(__file__).resolve().parent.parent / "prompts" / "agent_instruction.md"

# (name, description, {param: (type, description, required)})
TOOL_FUNCTIONS = [
    ("query_quality_data",
     "Run one read-only Athena SQL SELECT over the supplier_quality tables (ncr, suppliers, alerts).",
     {"sql": ("string", "A single SELECT statement with a LIMIT.", True)}),
    ("get_repeat_root_cause_alerts",
     "List cross-supplier repeat root-cause patterns found by the detector, newest first.",
     {"supplier_id": ("string", "Optional supplier id to filter to patterns involving it.", False)}),
    ("find_similar_ncrs",
     "Find NCRs at other suppliers whose defect and root cause are most similar to the given NCR.",
     {"ncr_id": ("string", "The NCR id, e.g. NCR-2026-0042.", True),
      "top_k": ("integer", "How many matches to return (default 5).", False)}),
    ("save_scar_draft",
     "Save a SCAR draft in markdown for human review. Nothing is sent to the supplier.",
     {"supplier_id": ("string", "Supplier the SCAR is addressed to.", True),
      "ncr_ids": ("string", "Comma-separated NCR ids the SCAR covers.", True),
      "title": ("string", "Short SCAR title.", True),
      "markdown": ("string", "Full SCAR body in markdown.", True)}),
]


class AgentStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, prefix: str,
                 data: DataStack, knowledge: KnowledgeStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        agent_model = self.node.try_get_context("agentModelId")
        summary_model = self.node.try_get_context("extractionModelId")
        instruction = INSTRUCTION_PATH.read_text().strip()

        athena_env = {
            "LAKE_BUCKET": data.lake_bucket.bucket_name,
            "ATHENA_WORKGROUP": data.athena_workgroup_name,
            "GLUE_DATABASE": data.glue_database_name,
        }

        # ---------------- tools Lambda ----------------
        tools_fn = python_function(self, "AgentToolsFn", "agent_tools.handler.handler",
                                   timeout=120, env=athena_env)
        self._grant_athena(tools_fn, data)
        data.lake_bucket.grant_read(tools_fn, "curated/*")
        data.lake_bucket.grant_read(tools_fn, "embeddings/*")
        data.lake_bucket.grant_put(tools_fn, "scar-drafts/*")

        # ---------------- guardrail ----------------
        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=f"{prefix}-guardrail",
            description="Supplier quality copilot guardrail",
            blocked_input_messaging="I can only help with supplier quality questions in this workspace.",
            blocked_outputs_messaging="I can't provide that response. Try rephrasing your supplier quality question.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=t, input_strength="HIGH", output_strength="HIGH")
                    for t in ["HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT"]
                ] + [
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE")
                ],
            ),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[bedrock.CfnGuardrail.TopicConfigProperty(
                    name="WeaponSystemTechnicalData",
                    type="DENY",
                    definition=("Requests for weapon system design details, performance "
                                "parameters, or technical data unrelated to supplier quality records."),
                    examples=[
                        "What is the maximum range of the interceptor?",
                        "Explain the guidance algorithm used in the missile seeker.",
                    ],
                )],
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type=t, action="ANONYMIZE")
                    for t in ["EMAIL", "PHONE", "US_SOCIAL_SECURITY_NUMBER"]
                ],
            ),
        )
        guardrail_version = bedrock.CfnGuardrailVersion(
            self, "GuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description="v1",
        )

        # ---------------- agent ----------------
        agent_role = iam.Role(
            self,
            "AgentRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}},
            ),
        )
        agent_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                     "bedrock:GetInferenceProfile", "bedrock:GetFoundationModel"],
            resources=bedrock_model_resources(agent_model),
        ))
        agent_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:Retrieve"], resources=[knowledge.knowledge_base_arn],
        ))
        agent_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:ApplyGuardrail"], resources=[guardrail.attr_guardrail_arn],
        ))

        functions = [
            bedrock.CfnAgent.FunctionProperty(
                name=name,
                description=desc,
                parameters={
                    p: bedrock.CfnAgent.ParameterDetailProperty(
                        type=t, description=d, required=req)
                    for p, (t, d, req) in params.items()
                },
            )
            for name, desc, params in TOOL_FUNCTIONS
        ]

        agent = bedrock.CfnAgent(
            self,
            "Agent",
            agent_name=f"{prefix}-supplier-quality-copilot",
            description="Supplier quality copilot (synthetic data)",
            agent_resource_role_arn=agent_role.role_arn,
            foundation_model=agent_model,
            instruction=instruction,
            idle_session_ttl_in_seconds=900,
            auto_prepare=True,
            action_groups=[
                bedrock.CfnAgent.AgentActionGroupProperty(
                    action_group_name="quality_tools",
                    description="Structured data, pattern alerts, similarity search, SCAR drafts",
                    action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(
                        lambda_=tools_fn.function_arn),
                    function_schema=bedrock.CfnAgent.FunctionSchemaProperty(functions=functions),
                ),
                bedrock.CfnAgent.AgentActionGroupProperty(
                    action_group_name="UserInputAction",
                    parent_action_group_signature="AMAZON.UserInput",
                ),
            ],
            knowledge_bases=[bedrock.CfnAgent.AgentKnowledgeBaseProperty(
                knowledge_base_id=knowledge.knowledge_base_id,
                description=("Narrative NCR, 8D and SCAR documents: what happened, root cause "
                             "investigations, and how suppliers fixed problems."),
                knowledge_base_state="ENABLED",
            )],
            guardrail_configuration=bedrock.CfnAgent.GuardrailConfigurationProperty(
                guardrail_identifier=guardrail.attr_guardrail_id,
                guardrail_version=guardrail_version.attr_version,
            ),
        )
        agent.node.add_dependency(agent_role)

        tools_fn.add_permission(
            "AllowBedrockAgent",
            principal=iam.ServicePrincipal("bedrock.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_account=Aws.ACCOUNT_ID,
            source_arn=agent.attr_agent_arn,
        )

        # An alias pins an agent version. Changing the description whenever the
        # instruction or tool schema changes makes CloudFormation update the alias,
        # which snapshots a fresh version from the prepared draft.
        fingerprint = hashlib.sha256(
            (instruction + json.dumps(TOOL_FUNCTIONS) + agent_model).encode()
        ).hexdigest()[:12]
        alias = bedrock.CfnAgentAlias(
            self, "AgentAlias",
            agent_id=agent.attr_agent_id,
            agent_alias_name="live",
            description=f"config {fingerprint}",
        )

        self.agent_id = agent.attr_agent_id
        self.agent_arn = agent.attr_agent_arn
        self.agent_alias_id = alias.attr_agent_alias_id
        self.agent_alias_arn = alias.attr_agent_alias_arn

        # ---------------- repeat root-cause detector ----------------
        detector_fn = python_function(
            self, "RootCauseDetectorFn", "detector.handler.handler",
            timeout=600, memory=1024,
            env={
                **athena_env,
                "SUMMARY_MODEL_ID": summary_model,
                "SIMILARITY_THRESHOLD": str(self.node.try_get_context("similarityThreshold")),
                "RECENT_WINDOW_DAYS": str(self.node.try_get_context("recentWindowDays")),
            },
        )
        data.lake_bucket.grant_read(detector_fn, "embeddings/*")
        data.lake_bucket.grant_read_write(detector_fn, "curated/alerts/*")
        detector_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=bedrock_model_resources(summary_model),
        ))

        events.Rule(
            self,
            "DetectorSchedule",
            schedule=events.Schedule.expression(self.node.try_get_context("detectorSchedule")),
            targets=[targets.LambdaFunction(detector_fn)],
        )
        self.detector_fn: _lambda.Function = detector_fn

        CfnOutput(self, "AgentId", value=self.agent_id)
        CfnOutput(self, "AgentAliasId", value=self.agent_alias_id)
        CfnOutput(self, "DetectorFunctionName", value=detector_fn.function_name)
        CfnOutput(self, "AgentToolsFunctionName", value=tools_fn.function_name)

    @staticmethod
    def _grant_athena(fn: _lambda.Function, data: DataStack) -> None:
        stack = Stack.of(fn)
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["athena:StartQueryExecution", "athena:GetQueryExecution",
                     "athena:GetQueryResults", "athena:StopQueryExecution"],
            resources=[stack.format_arn(service="athena", resource="workgroup",
                                        resource_name=data.athena_workgroup_name)],
        ))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["glue:GetDatabase", "glue:GetTable", "glue:GetTables",
                     "glue:GetPartitions"],
            resources=[
                stack.format_arn(service="glue", resource="catalog"),
                stack.format_arn(service="glue", resource="database",
                                 resource_name=data.glue_database_name),
                stack.format_arn(service="glue", resource="table",
                                 resource_name=f"{data.glue_database_name}/*"),
            ],
        ))
        data.lake_bucket.grant_read(fn, "curated/*")
        data.athena_results_bucket.grant_read_write(fn)
