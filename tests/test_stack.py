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


def test_every_api_method_requires_iam_auth(template):
    methods = template.find_resources("AWS::ApiGateway::Method")
    assert methods
    for m in methods.values():
        assert m["Properties"]["AuthorizationType"] == "AWS_IAM"


def test_document_table_streams_old_and_new_images(template):
    template.has_resource_properties("AWS::DynamoDB::Table", {
        "StreamSpecification": {"StreamViewType": "NEW_AND_OLD_IMAGES"},
        "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
    })


def test_bucket_is_private_encrypted_and_versioned(template):
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
