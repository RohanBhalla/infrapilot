"""Document search on AWS (plan C4, C6): docs bucket, S3 Vectors index, Bedrock Knowledge Base.

Chunks are made locally by data_collection (one .md file plus a .metadata.json sidecar each) and
uploaded with infra/scripts/sync_knowledge.sh. The Knowledge Base does no chunking of its own,
so cloud and local search see the same chunks.
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    aws_bedrock as bedrock,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3vectors as s3vectors,
)
from constructs import Construct

EMBED_MODEL = "amazon.titan-embed-text-v2:0"  # same model as the local LanceDB index
DIMENSIONS = 1024
PREFIX = "kb_export/"


class KnowledgeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # The chunks can be rebuilt from data_collection, so the bucket is not kept on destroy.
        docs = s3.Bucket(
            self, "DocsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        vectors = s3vectors.CfnVectorBucket(self, "VectorBucket")
        vectors.apply_removal_policy(RemovalPolicy.DESTROY)
        index = s3vectors.CfnIndex(
            self, "Index",
            vector_bucket_arn=vectors.attr_vector_bucket_arn,
            data_type="float32",
            dimension=DIMENSIONS,
            distance_metric="cosine",
            # Chunk text is larger than the 2 KB of filterable metadata S3 Vectors allows, so the
            # Knowledge Base's own fields are stored but not filterable. Our sidecar fields
            # (category, doc_type, ...) stay filterable.
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]),
        )
        index.apply_removal_policy(RemovalPolicy.DESTROY)

        model_arn = f"arn:aws:bedrock:{self.region}::foundation-model/{EMBED_MODEL}"
        role = iam.Role(self, "KnowledgeBaseRole",
                        assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com", conditions={
                            "StringEquals": {"aws:SourceAccount": self.account}}))
        role.add_to_policy(iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=[model_arn]))
        docs.grant_read(role, f"{PREFIX}*")
        role.add_to_policy(iam.PolicyStatement(actions=["s3:ListBucket"], resources=[docs.bucket_arn]))
        role.add_to_policy(iam.PolicyStatement(
            actions=["s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors",
                     "s3vectors:QueryVectors", "s3vectors:GetIndex"],
            resources=[index.attr_index_arn]))

        kb = bedrock.CfnKnowledgeBase(
            self, "KnowledgeBase",
            name="infrapilot-docs",
            description="InfraPilot DevOps documentation (chunks from data_collection)",
            role_arn=role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=model_arn,
                    embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                        bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase
                        .BedrockEmbeddingModelConfigurationProperty(dimensions=DIMENSIONS,
                                                                    embedding_data_type="FLOAT32")),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=index.attr_index_arn),
            ),
        )
        kb.node.add_dependency(role)  # the role's policy must exist before Bedrock checks it

        source = bedrock.CfnDataSource(
            self, "Docs",
            name="kb-export",
            knowledge_base_id=kb.attr_knowledge_base_id,
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=docs.bucket_arn, inclusion_prefixes=[PREFIX]),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="NONE")),
        )

        self.knowledge_base_id = kb.attr_knowledge_base_id
        self.knowledge_base_arn = kb.attr_knowledge_base_arn

        CfnOutput(self, "DocsBucketName", value=docs.bucket_name)
        CfnOutput(self, "KnowledgeBaseId", value=kb.attr_knowledge_base_id)
        CfnOutput(self, "DataSourceId", value=source.attr_data_source_id)
        CfnOutput(self, "VectorIndexArn", value=index.attr_index_arn)
