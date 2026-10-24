import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.update({
    "AWS_DEFAULT_REGION": "ap-south-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "TABLE_NAME": "documents",
    "AUDIT_TABLE_NAME": "audit",
    "BUCKET_NAME": "documents-bucket",
    "STATE_MACHINE_ARN": "arn:aws:states:ap-south-1:123456789012:stateMachine:wf",
    "EXTRACTOR": "stub",
})


class FakeStepFunctions:
    """Records workflow calls; real Step Functions behaviour is out of scope
    for unit tests."""

    class exceptions:
        class ExecutionAlreadyExists(Exception):
            pass

    def __init__(self):
        self.started: list[dict] = []
        self.task_success: list[dict] = []

    def start_execution(self, **kwargs):
        if any(s["name"] == kwargs["name"] for s in self.started):
            raise self.exceptions.ExecutionAlreadyExists(kwargs["name"])
        self.started.append(kwargs)

    def send_task_success(self, **kwargs):
        self.task_success.append(kwargs)


@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        ddb.create_table(
            TableName="documents",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "content_sha256", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "by-content-hash",
                "KeySchema": [{"AttributeName": "content_sha256", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "INCLUDE",
                               "NonKeyAttributes": ["document_id"]},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="audit",
            KeySchema=[{"AttributeName": "document_id", "KeyType": "HASH"},
                       {"AttributeName": "seq", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "document_id", "AttributeType": "S"},
                {"AttributeName": "seq", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3").create_bucket(
            Bucket="documents-bucket",
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )

        from common import aws as aws_mod
        aws_mod.documents.cache_clear()
        aws_mod.client.cache_clear()
        sfn = FakeStepFunctions()
        real_client = aws_mod.client

        def client(service):
            return sfn if service == "stepfunctions" else real_client(service)

        monkeypatch.setattr(aws_mod, "client", client)
        yield {"sfn": sfn, "repo": aws_mod.documents(), "ddb": ddb}
        aws_mod.documents.cache_clear()
