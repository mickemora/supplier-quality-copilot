"""App layer: Cognito user pool and an authenticated chat API in front of the agent.

The web UI comes in a later iteration; until then use ``scripts/ask.py`` (calls the
agent directly with your AWS credentials) or call ``POST /chat`` with a Cognito ID token.
"""
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_cognito as cognito,
    aws_iam as iam,
)
from constructs import Construct

from stacks.agent_stack import AgentStack
from stacks.common import python_function


class AppStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, prefix: str,
                 agent: AgentStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

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
        client = user_pool.add_client(
            "WebClient",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            generate_secret=False,
            id_token_validity=Duration.hours(1),
        )

        chat_fn = python_function(self, "ChatFn", "chat.handler.handler", timeout=29, env={
            "AGENT_ID": agent.agent_id,
            "AGENT_ALIAS_ID": agent.agent_alias_id,
        })
        chat_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeAgent"],
            resources=[agent.agent_alias_arn],
        ))

        api = apigw.RestApi(
            self,
            "ChatApi",
            rest_api_name=f"{prefix}-chat-api",
            deploy_options=apigw.StageOptions(
                stage_name="v1", throttling_rate_limit=5, throttling_burst_limit=10,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type"],
            ),
        )
        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self, "Authorizer", cognito_user_pools=[user_pool]
        )
        api.root.add_resource("chat").add_method(
            "POST",
            apigw.LambdaIntegration(chat_fn, timeout=Duration.seconds(29)),
            authorizer=authorizer,
            authorization_type=apigw.AuthorizationType.COGNITO,
        )

        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        CfnOutput(self, "ChatApiUrl", value=f"{api.url}chat")
