"""Infrastructure assertions on the synthesized CloudFormation template."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.ingestion_stack import IngestionStack


@pytest.fixture(scope="module")
def template():
    app = cdk.App(context={"extractor": "stub"})
    return Template.from_stack(IngestionStack(app, "Test"))


def test_workflow_is_standard_type(template):
    template.has_resource_properties("AWS::StepFunctions::StateMachine",
                                     {"StateMachineType": "STANDARD"})


def _methods_by_auth(template):
    out: dict[str, int] = {}
    for m in template.find_resources("AWS::ApiGateway::Method").values():
        auth = m["Properties"]["AuthorizationType"]
        out[auth] = out.get(auth, 0) + 1
    return out


def test_no_api_method_is_unauthenticated(template):
    # 2 machine routes (SigV4) and 5 reviewer routes (Cognito); nothing open.
    assert _methods_by_auth(template) == {"AWS_IAM": 2, "COGNITO_USER_POOLS": 5}


def test_reviewers_are_admin_created_and_use_pkce(template):
    template.has_resource_properties("AWS::Cognito::UserPool", {
        "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True},
    })
    template.has_resource_properties("AWS::Cognito::UserPoolClient", {
        "AllowedOAuthFlows": ["code"],
        "GenerateSecret": False,
    })
    template.has_resource_properties("AWS::Cognito::UserPoolGroup", {"GroupName": "reviewers"})


def test_review_queue_index_never_projects_the_task_token(template):
    tables = template.find_resources("AWS::DynamoDB::Table")
    gsis = [g for t in tables.values()
            for g in t["Properties"].get("GlobalSecondaryIndexes", [])]
    review = next(g for g in gsis if g["IndexName"] == "pending-review")
    assert review["Projection"]["ProjectionType"] == "INCLUDE"
    assert "review_task_token" not in review["Projection"]["NonKeyAttributes"]


def test_api_is_served_through_the_app_origin_uncached(template):
    template.has_resource_properties("AWS::CloudFront::Distribution", {
        "DistributionConfig": Match.object_like({
            "CacheBehaviors": [Match.object_like({
                "PathPattern": "/reviews*",
                "ViewerProtocolPolicy": "https-only",
                # Managed CachingDisabled + AllViewerExceptHostHeader, so the
                # Authorization header reaches the Cognito authorizer.
                "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
                "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac",
            })],
        }),
    })


def test_app_sends_security_headers(template):
    template.has_resource_properties("AWS::CloudFront::ResponseHeadersPolicy", {
        "ResponseHeadersPolicyConfig": Match.object_like({
            "SecurityHeadersConfig": Match.object_like({
                "ContentSecurityPolicy": Match.object_like({
                    "ContentSecurityPolicy": Match.string_like_regexp("frame-ancestors 'none'"),
                }),
                "FrameOptions": {"FrameOption": "DENY", "Override": True},
            }),
        }),
    })


def test_site_bucket_is_private(template):
    buckets = template.find_resources("AWS::S3::Bucket")
    for b in buckets.values():
        block = b["Properties"]["PublicAccessBlockConfiguration"]
        assert all(block.values())


def test_document_table_streams_old_and_new_images(template):
    template.has_resource_properties("AWS::DynamoDB::Table", {
        "StreamSpecification": {"StreamViewType": "NEW_AND_OLD_IMAGES"},
        "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
    })


def test_document_bucket_is_private_encrypted_and_versioned(template):
    template.has_resource_properties("AWS::S3::Bucket", {
        "VersioningConfiguration": {"Status": "Enabled"},
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "BlockPublicPolicy": True,
            "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
        "BucketEncryption": Match.any_value(),
    })


def test_data_is_retained_by_default(template):
    for res in template.find_resources("AWS::DynamoDB::Table").values():
        assert res["DeletionPolicy"] == "Retain"
    for res in template.find_resources("AWS::Cognito::UserPool").values():
        assert res["DeletionPolicy"] == "Retain"


def test_stream_consumer_has_dead_letter_queue(template):
    template.has_resource_properties("AWS::Lambda::EventSourceMapping", {
        "FunctionResponseTypes": ["ReportBatchItemFailures"],
        "DestinationConfig": {"OnFailure": Match.any_value()},
    })


def test_search_is_off_by_default(template):
    template.resource_count_is("AWS::OpenSearchServerless::Collection", 0)


def test_search_can_be_enabled():
    app = cdk.App(context={"enableSearch": "true"})
    Template.from_stack(IngestionStack(app, "WithSearch")).resource_count_is(
        "AWS::OpenSearchServerless::Collection", 1)
