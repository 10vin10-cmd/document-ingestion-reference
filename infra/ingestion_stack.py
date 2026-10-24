"""CDK stack: serverless document ingestion.

Context flags (cdk.json or -c on the command line):
  extractor     "textract" (default) or "stub" for a zero-cost demo
  enableSearch  true to provision an OpenSearch Serverless collection
  ephemeral     true to let `cdk destroy` delete data (demo accounts only)
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_apigateway as apigw,
)
from aws_cdk import (
    aws_cloudwatch as cw,
)
from aws_cdk import (
    aws_dynamodb as ddb,
)
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_lambda_event_sources as sources,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_opensearchserverless as aoss,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_s3_notifications as s3n,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from aws_cdk import (
    aws_stepfunctions as sfn,
)
from aws_cdk import (
    aws_stepfunctions_tasks as tasks,
)
from constructs import Construct

SRC = str(Path(__file__).resolve().parent.parent / "src")
SEARCH_COLLECTION = "doc-ingestion"


class IngestionStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        extractor = self.node.try_get_context("extractor") or "textract"
        enable_search = str(self.node.try_get_context("enableSearch")).lower() == "true"
        ephemeral = str(self.node.try_get_context("ephemeral")).lower() == "true"
        removal = RemovalPolicy.DESTROY if ephemeral else RemovalPolicy.RETAIN

        # -- storage ---------------------------------------------------------
        bucket = s3.Bucket(
            self, "Documents",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,  # protects against overwrite of a received document
            lifecycle_rules=[s3.LifecycleRule(
                noncurrent_version_expiration=Duration.days(90),
                abort_incomplete_multipart_upload_after=Duration.days(1),
            )],
            removal_policy=removal,
            auto_delete_objects=ephemeral,
        )

        table = ddb.Table(
            self, "DocumentTable",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=removal,
        )
        table.add_global_secondary_index(
            index_name="by-content-hash",
            partition_key=ddb.Attribute(name="content_sha256", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.INCLUDE,
            non_key_attributes=["document_id"],
        )

        audit_table = ddb.Table(
            self, "AuditTable",
            partition_key=ddb.Attribute(name="document_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="seq", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=removal,
        )

        # -- functions -------------------------------------------------------
        code = lambda_.Code.from_asset(SRC, exclude=["**/__pycache__"])
        base_env = {"TABLE_NAME": table.table_name}

        def fn(name: str, handler: str, timeout: int = 30, memory: int = 256,
               env: dict | None = None) -> lambda_.Function:
            return lambda_.Function(
                self, name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                code=code,
                handler=handler,
                timeout=Duration.seconds(timeout),
                memory_size=memory,
                environment={**base_env, **(env or {})},
                tracing=lambda_.Tracing.ACTIVE,
                log_group=logs.LogGroup(self, f"{name}Logs",
                                        retention=logs.RetentionDays.ONE_MONTH,
                                        removal_policy=removal),
            )

        create_fn = fn("CreateDocumentFn", "handlers.create_document.handler",
                       env={"BUCKET_NAME": bucket.bucket_name})
        get_fn = fn("GetDocumentFn", "handlers.get_document.handler")
        review_fn = fn("SubmitReviewFn", "handlers.submit_review.handler")
        scan_fn = fn("ScanFn", "handlers.workflow.scan")
        extract_fn = fn("ExtractFn", "handlers.workflow.extract", timeout=120, memory=512,
                        env={"EXTRACTOR": extractor})
        validate_fn = fn("ValidateFn", "handlers.workflow.validate")
        request_review_fn = fn("RequestReviewFn", "handlers.workflow.request_review")
        finalize_fn = fn("FinalizeFn", "handlers.workflow.finalize")
        mark_failed_fn = fn("MarkFailedFn", "handlers.workflow.mark_failed")
        on_created_fn = fn("OnObjectCreatedFn", "handlers.on_object_created.handler")

        for f in (create_fn, review_fn, scan_fn, extract_fn, validate_fn,
                  request_review_fn, finalize_fn, mark_failed_fn, on_created_fn):
            table.grant_read_write_data(f)
        table.grant_read_data(get_fn)

        bucket.grant_put(create_fn)  # needed to presign the upload URL
        bucket.grant_read(scan_fn)
        bucket.grant_read(extract_fn)  # Textract reads S3 with the caller's role
        extract_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["textract:AnalyzeExpense"], resources=["*"]))
        events.EventBus.from_event_bus_name(self, "DefaultBus", "default") \
            .grant_put_events_to(finalize_fn)

        # -- workflow --------------------------------------------------------
        retryable = ["ThrottlingException", "ProvisionedThroughputExceededException",
                     "ConcurrencyError"]

        def step(name: str, f: lambda_.IFunction, **kw) -> tasks.LambdaInvoke:
            task = tasks.LambdaInvoke(self, name, lambda_function=f,
                                      payload_response_only=True, **kw)
            # ConcurrencyError means another writer won the race; a retry
            # re-reads the record and acts on its current state.
            task.add_retry(errors=retryable, interval=Duration.seconds(2),
                           max_attempts=5, backoff_rate=2, jitter_strategy=sfn.JitterType.FULL)
            return task

        mark_failed = tasks.LambdaInvoke(
            self, "Mark failed", lambda_function=mark_failed_fn,
            payload_response_only=True, result_path=sfn.JsonPath.DISCARD,
        ).next(sfn.Fail(self, "Processing failed"))

        scan = step("Scan", scan_fn)
        extract = step("Extract fields", extract_fn)
        validate = step("Validate", validate_fn)
        request_review = tasks.LambdaInvoke(
            self, "Wait for human review",
            lambda_function=request_review_fn,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object({
                "document_id": sfn.JsonPath.string_at("$.document_id"),
                "issues": sfn.JsonPath.list_at("$.issues"),
                "task_token": sfn.JsonPath.task_token,
            }),
            result_path="$.review",
            task_timeout=sfn.Timeout.duration(Duration.days(7)),
        )
        finalize = step("Finalize", finalize_fn)

        for task in (scan, extract, validate, request_review, finalize):
            task.add_catch(mark_failed, result_path="$.error")

        definition = scan.next(
            sfn.Choice(self, "Clean?")
            .when(sfn.Condition.boolean_equals("$.clean", False),
                  sfn.Succeed(self, "Quarantined"))
            .otherwise(extract.next(validate).next(
                sfn.Choice(self, "Needs review?")
                .when(sfn.Condition.boolean_equals("$.needs_review", True),
                      request_review.next(
                          sfn.Choice(self, "Approved?")
                          .when(sfn.Condition.boolean_equals("$.review.approved", True),
                                finalize)
                          .otherwise(sfn.Succeed(self, "Rejected"))))
                .otherwise(finalize)))
        )

        # Standard, not Express: review can take days (Express caps at five
        # minutes) and ~80 TPS is far below Standard's transition limits.
        state_machine = sfn.StateMachine(
            self, "DocumentWorkflow",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=logs.LogGroup(self, "WorkflowLogs",
                                          retention=logs.RetentionDays.ONE_MONTH,
                                          removal_policy=removal),
                level=sfn.LogLevel.ERROR,
            ),
        )
        state_machine.grant_task_response(review_fn)
        state_machine.grant_start_execution(on_created_fn)
        on_created_fn.add_environment("STATE_MACHINE_ARN", state_machine.state_machine_arn)

        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(on_created_fn),
            s3.NotificationKeyFilter(prefix="incoming/"),
        )

        # -- change stream: audit + search -----------------------------------
        stream_env = {"AUDIT_TABLE_NAME": audit_table.table_name}
        stream_fn = fn("StreamProcessorFn", "handlers.stream_processor.handler",
                       timeout=60, env=stream_env)
        audit_table.grant_write_data(stream_fn)
        stream_dlq = sqs.Queue(self, "StreamDLQ", retention_period=Duration.days(14),
                               enforce_ssl=True)
        stream_fn.add_event_source(sources.DynamoEventSource(
            table,
            starting_position=lambda_.StartingPosition.TRIM_HORIZON,
            batch_size=100,
            bisect_batch_on_error=True,
            retry_attempts=5,
            report_batch_item_failures=True,
            on_failure=sources.SqsDlq(stream_dlq),
        ))

        if enable_search:
            self._add_search(stream_fn)

        # -- API -------------------------------------------------------------
        access_logs = logs.LogGroup(self, "ApiAccessLogs",
                                    retention=logs.RetentionDays.ONE_MONTH,
                                    removal_policy=removal)
        api = apigw.RestApi(
            self, "IngestionApi",
            rest_api_name="document-ingestion",
            # Machine-to-machine callers sign requests with SigV4.
            default_method_options=apigw.MethodOptions(
                authorization_type=apigw.AuthorizationType.IAM),
            deploy_options=apigw.StageOptions(
                stage_name="v1",
                throttling_rate_limit=100,  # design target 80 TPS + headroom
                throttling_burst_limit=200,
                tracing_enabled=True,
                access_log_destination=apigw.LogGroupLogDestination(access_logs),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=True, http_method=True, ip=True, protocol=True,
                    request_time=True, resource_path=True, response_length=True,
                    status=True, user=True),
            ),
        )
        documents = api.root.add_resource("documents")
        documents.add_method("POST", apigw.LambdaIntegration(create_fn))
        doc = documents.add_resource("{id}")
        doc.add_method("GET", apigw.LambdaIntegration(get_fn))
        doc.add_resource("review").add_method("POST", apigw.LambdaIntegration(review_fn))

        # -- alarms ----------------------------------------------------------
        cw.Alarm(self, "WorkflowFailures",
                 metric=state_machine.metric_failed(period=Duration.minutes(5)),
                 threshold=5, evaluation_periods=1,
                 alarm_description="Documents failing processing")
        cw.Alarm(self, "StreamDLQNotEmpty",
                 metric=stream_dlq.metric_approximate_number_of_messages_visible(),
                 threshold=1, evaluation_periods=1,
                 alarm_description="Audit/search records could not be written")
        cw.Alarm(self, "Api5xx",
                 metric=api.metric_server_error(period=Duration.minutes(5)),
                 threshold=10, evaluation_periods=1,
                 alarm_description="API returning server errors")

        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "AuditTableName", value=audit_table.table_name)
        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)

    def _add_search(self, stream_fn: lambda_.Function) -> None:
        enc = aoss.CfnSecurityPolicy(
            self, "SearchEncryption", name=f"{SEARCH_COLLECTION}-enc", type="encryption",
            policy=json.dumps({
                "Rules": [{"ResourceType": "collection",
                           "Resource": [f"collection/{SEARCH_COLLECTION}"]}],
                "AWSOwnedKey": True,
            }))
        net = aoss.CfnSecurityPolicy(
            self, "SearchNetwork", name=f"{SEARCH_COLLECTION}-net", type="network",
            policy=json.dumps([{
                "Rules": [{"ResourceType": "collection",
                           "Resource": [f"collection/{SEARCH_COLLECTION}"]}],
                "AllowFromPublic": True,  # requests are still IAM-signed
            }]))
        collection = aoss.CfnCollection(self, "SearchCollection",
                                        name=SEARCH_COLLECTION, type="SEARCH")
        collection.node.add_dependency(enc)
        collection.node.add_dependency(net)
        access = aoss.CfnAccessPolicy(
            self, "SearchAccess", name=f"{SEARCH_COLLECTION}-access", type="data",
            policy=json.dumps([{
                "Rules": [{
                    "ResourceType": "index",
                    "Resource": [f"index/{SEARCH_COLLECTION}/*"],
                    "Permission": ["aoss:CreateIndex", "aoss:UpdateIndex",
                                   "aoss:DescribeIndex", "aoss:WriteDocument"],
                }],
                "Principal": [stream_fn.role.role_arn],
            }]))
        collection.node.add_dependency(access)
        stream_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["aoss:APIAccessAll"], resources=[collection.attr_arn]))
        stream_fn.add_environment("SEARCH_ENDPOINT", collection.attr_collection_endpoint)
        CfnOutput(self, "SearchEndpoint", value=collection.attr_collection_endpoint)
