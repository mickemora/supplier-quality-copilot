"""Knowledge layer: Bedrock Knowledge Base on Amazon S3 Vectors.

S3 Vectors keeps the demo cheap and fully destroyable (no always-on search
cluster). The corpus is ``s3://<lake>/kb/``; each NCR/8D text has a
``.metadata.json`` sidecar so retrieval can filter by supplier, part, or program.
"""
from aws_cdk import (
    Aws,
    CfnOutput,
    Stack,
    aws_bedrock as bedrock,
    aws_iam as iam,
    aws_s3vectors as s3vectors,
)
from constructs import Construct

from stacks.data_stack import DataStack


class KnowledgeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, prefix: str,
                 data: DataStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        embedding_model_id = self.node.try_get_context("embeddingModelId")
        dimensions = int(self.node.try_get_context("embeddingDimensions"))
        self.embedding_model_id = embedding_model_id
        embedding_model_arn = (
            f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}::foundation-model/{embedding_model_id}"
        )

        vector_bucket_name = f"{prefix}-vectors-{Aws.ACCOUNT_ID}-{Aws.REGION}"
        vector_bucket = s3vectors.CfnVectorBucket(
            self, "VectorBucket", vector_bucket_name=vector_bucket_name
        )

        index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            vector_bucket_name=vector_bucket_name,
            index_name=f"{prefix}-ncr-index",
            data_type="float32",
            dimension=dimensions,
            distance_metric="cosine",
            # Bedrock stores chunk text + metadata blobs in these keys; they must be
            # non-filterable or they count against the 2 KB filterable metadata limit.
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
            ),
        )
        index.node.add_dependency(vector_bucket)

        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}},
            ),
        )
        data.lake_bucket.grant_read(kb_role, "kb/*")
        kb_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[data.lake_bucket.bucket_arn],
        ))
        kb_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[embedding_model_arn],
        ))
        kb_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "s3vectors:PutVectors",
                "s3vectors:GetVectors",
                "s3vectors:DeleteVectors",
                "s3vectors:QueryVectors",
                "s3vectors:GetIndex",
            ],
            resources=[index.attr_index_arn],
        ))

        kb = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=f"{prefix}-supplier-quality-kb",
            description="NCR, SCAR and 8D narratives from suppliers (synthetic)",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embedding_model_arn,
                    embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                        bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase.BedrockEmbeddingModelConfigurationProperty(
                            dimensions=dimensions,
                            embedding_data_type="FLOAT32",
                        )
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=index.attr_index_arn,
                ),
            ),
        )
        kb.node.add_dependency(kb_role)
        kb.node.add_dependency(index)

        data_source = bedrock.CfnDataSource(
            self,
            "KbDataSource",
            knowledge_base_id=kb.attr_knowledge_base_id,
            name=f"{prefix}-ncr-documents",
            # RETAIN so `cdk destroy` doesn't fail trying to purge vectors from an
            # index that is being deleted in the same operation.
            data_deletion_policy="RETAIN",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=data.lake_bucket.bucket_arn,
                    inclusion_prefixes=["kb/"],
                ),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="FIXED_SIZE",
                    fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                        max_tokens=400, overlap_percentage=15
                    ),
                )
            ),
        )

        self.knowledge_base_id = kb.attr_knowledge_base_id
        self.knowledge_base_arn = kb.attr_knowledge_base_arn
        self.data_source_id = data_source.attr_data_source_id

        CfnOutput(self, "KnowledgeBaseId", value=self.knowledge_base_id)
        CfnOutput(self, "DataSourceId", value=self.data_source_id)
