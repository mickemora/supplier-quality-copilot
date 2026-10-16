"""App layer: web UI, sign-in, and the API behind it.

- S3 + CloudFront host the no-build web app in ``web/``; ``config.json`` is written at
  deploy time with the API URL and Cognito settings.
- Cognito managed login (authorization code + PKCE) for the browser, a ``reviewers``
  group for SCAR approval, and a password client for scripts/curl.
- REST API with a Cognito authorizer on ``/{proxy+}`` -> one router Lambda.
- Chat is asynchronous: the API stores a job in DynamoDB and invokes a worker that runs
  the agent, so long multi-tool questions don't hit API Gateway's 29 second limit.
"""
from pathlib import Path

from aws_cdk import (
    ArnFormat,
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
)
from constructs import Construct

from stacks.agent_stack import AgentStack
from stacks.common import python_function
from stacks.data_stack import DataStack
from stacks.ingest_stack import IngestStack

WEB_DIR = str(Path(__file__).resolve().parent.parent / "web")
REVIEWER_GROUP = "reviewers"


class AppStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, prefix: str,
                 data: DataStack, ingest: IngestStack, agent: AgentStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------- sign-in ----------------
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{prefix}-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12, require_symbols=True, require_digits=True,
                require_uppercase=True, require_lowercase=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )
        cli_client = user_pool.add_client(
            "WebClient",  # Day 1 id kept so the client isn't replaced; used by scripts/curl
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            generate_secret=False,
            id_token_validity=Duration.hours(1),
        )
        domain = user_pool.add_domain(
            "LoginDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"{prefix}-copilot-{Aws.ACCOUNT_ID}"),
        )
        cognito.CfnUserPoolGroup(
            self, "ReviewersGroup",
            user_pool_id=user_pool.user_pool_id,
            group_name=REVIEWER_GROUP,
            description="Supplier quality engineers who can approve or reject SCAR drafts",
        )

        # ---------------- web hosting ----------------
        site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self,
            "SiteDistribution",
            comment=f"{prefix} supplier quality copilot",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )
        site_url = f"https://{distribution.distribution_domain_name}/"

        spa_client = user_pool.add_client(
            "SpaClient",
            generate_secret=False,
            prevent_user_existence_errors=True,
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            id_token_validity=Duration.hours(8),
            access_token_validity=Duration.hours(8),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL,
                        cognito.OAuthScope.PROFILE],
                callback_urls=[site_url, "http://localhost:8080/"],
                logout_urls=[site_url, "http://localhost:8080/"],
            ),
        )

        # ---------------- async chat ----------------
        jobs_table = dynamodb.Table(
            self,
            "ChatJobsTable",
            partition_key=dynamodb.Attribute(name="job_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )

        worker_fn = python_function(self, "ChatWorkerFn", "chat.worker.handler", timeout=300, env={
            "JOBS_TABLE": jobs_table.table_name,
            "AGENT_ID": agent.agent_id,
            "AGENT_ALIAS_ID": agent.agent_alias_id,
        })
        jobs_table.grant_read_write_data(worker_fn)
        worker_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeAgent"], resources=[agent.agent_alias_arn],
        ))

        # ---------------- web API ----------------
        api_fn = python_function(self, "WebApiFn", "web.api.handler", timeout=29, env={
            "JOBS_TABLE": jobs_table.table_name,
            "LAKE_BUCKET": data.lake_bucket.bucket_name,
            "DOCS_BUCKET": data.docs_bucket.bucket_name,
            "STATE_MACHINE_ARN": ingest.state_machine.state_machine_arn,
            "CHAT_WORKER_FUNCTION": worker_fn.function_name,
            "DETECTOR_FUNCTION": agent.detector_fn.function_name,
            "REVIEWER_GROUP": REVIEWER_GROUP,
        })
        jobs_table.grant_read_write_data(api_fn)
        worker_fn.grant_invoke(api_fn)
        agent.detector_fn.grant_invoke(api_fn)
        data.lake_bucket.grant_read(api_fn, "curated/*")
        data.lake_bucket.grant_read(api_fn, "kb/*")
        data.lake_bucket.grant_read_write(api_fn, "scar-drafts/*")
        data.docs_bucket.grant_put(api_fn, "incoming/*")
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["states:ListExecutions"],
            resources=[ingest.state_machine.state_machine_arn],
        ))
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["states:DescribeExecution", "states:GetExecutionHistory"],
            resources=[self.format_arn(service="states", resource="execution",
                                       resource_name=f"{prefix}-ingest:*",
                                       arn_format=ArnFormat.COLON_RESOURCE_NAME)],
        ))

        api = apigw.RestApi(
            self,
            "ChatApi",  # Day 1 id kept so the API isn't replaced
            rest_api_name=f"{prefix}-api",
            deploy_options=apigw.StageOptions(
                stage_name="v1", throttling_rate_limit=20, throttling_burst_limit=40,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["GET", "POST", "PUT", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type"],
            ),
        )
        # Let the browser read error bodies from auth failures and throttling.
        for rid, rtype in (("Default4xx", apigw.ResponseType.DEFAULT_4XX),
                           ("Default5xx", apigw.ResponseType.DEFAULT_5XX)):
            api.add_gateway_response(rid, type=rtype, response_headers={
                "Access-Control-Allow-Origin": "'*'",
                "Access-Control-Allow-Headers": "'Authorization,Content-Type'",
            })
        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self, "Authorizer", cognito_user_pools=[user_pool]
        )
        proxy = api.root.add_proxy(any_method=False)
        proxy.add_method(
            "ANY",
            apigw.LambdaIntegration(api_fn, timeout=Duration.seconds(29)),
            authorizer=authorizer,
            authorization_type=apigw.AuthorizationType.COGNITO,
        )

        # ---------------- publish the web app ----------------
        s3deploy.BucketDeployment(
            self,
            "DeploySite",
            sources=[
                s3deploy.Source.asset(WEB_DIR, exclude=["config.json"]),
                s3deploy.Source.json_data("config.json", {
                    "region": Aws.REGION,
                    "apiUrl": api.url,
                    "userPoolClientId": spa_client.user_pool_client_id,
                    "authDomain": domain.base_url(),
                    "redirectUri": site_url,
                }),
            ],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            prune=True,
        )

        CfnOutput(self, "WebUrl", value=site_url)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=cli_client.user_pool_client_id)
        CfnOutput(self, "ApiUrl", value=api.url)
