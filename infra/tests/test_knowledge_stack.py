import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infrapilot_infra.knowledge_stack import KnowledgeStack


@pytest.fixture(scope="module")
def t():
    app = cdk.App()
    stack = KnowledgeStack(app, "Test", env=cdk.Environment(account="123456789012", region="us-east-1"))
    return Template.from_stack(stack)


def test_index_matches_titan_v2_and_keeps_text_unfilterable(t):
    t.has_resource_properties("AWS::S3Vectors::Index", {
        "DataType": "float32", "Dimension": 1024, "DistanceMetric": "cosine",
        "MetadataConfiguration": {"NonFilterableMetadataKeys": ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]},
    })


def test_knowledge_base_uses_s3_vectors_and_titan(t):
    t.has_resource_properties("AWS::Bedrock::KnowledgeBase", {
        "StorageConfiguration": Match.object_like({"Type": "S3_VECTORS"}),
        "KnowledgeBaseConfiguration": {"Type": "VECTOR", "VectorKnowledgeBaseConfiguration": Match.object_like({
            "EmbeddingModelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"})},
    })


def test_data_source_keeps_our_chunks(t):
    t.has_resource_properties("AWS::Bedrock::DataSource", {
        "VectorIngestionConfiguration": {"ChunkingConfiguration": {"ChunkingStrategy": "NONE"}},
        "DataSourceConfiguration": Match.object_like({"S3Configuration": Match.object_like({
            "InclusionPrefixes": ["kb_export/"]})}),
    })


def test_no_opensearch(t):
    # OpenSearch Serverless has a large always-on minimum cost (plan §8).
    t.resource_count_is("AWS::OpenSearchServerless::Collection", 0)
