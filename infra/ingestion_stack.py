"""CDK stack: serverless document ingestion.

Context flags (cdk.json or -c on the command line):
  extractor     "textract" (default) or "stub" for a zero-cost demo
  enableSearch  true to provision an OpenSearch Serverless collection
  ephemeral     true to let `cdk destroy` delete data (demo accounts only)
  authDomainPrefix  Cognito sign-in domain prefix (default docreview-<account>)

Build the reviewer app (`npm run build` in frontend/) before deploying; the
stack publishes frontend/dist to the site bucket.
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import (
    Annotations,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cloudwatch as cw,
    aws_cognito as cognito,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as sources,
    aws_logs as logs,
    aws_opensearchserverless as aoss,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_s3_notifications as s3n,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent
SRC = str(ROOT / "src")
FRONTEND_DIST = ROOT / "frontend" / "dist"
SEARCH_COLLECTION = "doc-ingestion"
REVIEWER_GROUP = "reviewers"
LOCAL_DEV_URL = "http://localhost:5173/"

# Same-origin app and API, so the policy can stay tight. Wildcards cover the
# regional S3 endpoint used for document previews and the Cognito endpoints.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https://*.amazonaws.com",
    "frame-src https://*.amazonaws.com",
    "connect-src 'self' https://*.amazonaws.com https://*.amazoncognito.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


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
        # Sparse index: only documents carrying review_queue (set while they
        # wait for a human) are in it, so listing the queue never scans.
        table.add_global_secondary_index(
            index_name="pending-review",
            partition_key=ddb.Attribute(name="review_queue", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="created_at", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.INCLUDE,
            # Never project review_task_token: it can resume the workflow.
            non_key_attributes=["document_id", "business_queue", "review_issues",
                                "possible_duplicate_of", "claimed_by", "claim_expires_at"],
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
        reviews_fn = fn("ReviewsFn", "handlers.reviews.handler",
                        env={"BUCKET_NAME": bucket.bucket_name})
        scan_fn = fn("ScanFn", "handlers.workflow.scan")
        extract_fn = fn("ExtractFn", "handlers.workflow.extract", timeout=120, memory=512,
                        env={"EXTRACTOR": extractor})
        validate_fn = fn("ValidateFn", "handlers.workflow.validate")
        request_review_fn = fn("RequestReviewFn", "handlers.workflow.request_review")
        finalize_fn = fn("FinalizeFn", "handlers.workflow.finalize")
        mark_failed_fn = fn("MarkFailedFn", "handlers.workflow.mark_failed")
        on_created_fn = fn("OnObjectCreatedFn", "handlers.on_object_created.handler")

        for f in (create_fn, reviews_fn, scan_fn, extract_fn, validate_fn,
                  request_review_fn, finalize_fn, mark_failed_fn, on_created_fn):
            table.grant_read_write_data(f)
        table.grant_read_data(get_fn)

        bucket.grant_put(create_fn)  # needed to presign the upload URL
        bucket.grant_read(scan_fn)
        bucket.grant_read(extract_fn)  # Textract reads S3 with the caller's role
        bucket.grant_read(reviews_fn)  # presigned preview URLs
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
        state_machine.grant_task_response(reviews_fn)
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
            # Regional: CloudFront sits in front for the reviewer app, and
            # system callers reach it directly.
            endpoint_types=[apigw.EndpointType.REGIONAL],
            # Machine-to-machine callers sign requests with SigV4. Human
            # routes override this with the Cognito authorizer below.
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

        # -- reviewers: web app, sign-in and the review API ------------------
        site, distribution = self._add_reviewer_site(api)
        app_url = f"https://{distribution.distribution_domain_name}/"
        user_pool, user_pool_client, auth_domain = self._add_reviewer_auth(removal, app_url)
        reviewer_auth = apigw.MethodOptions(
            authorization_type=apigw.AuthorizationType.COGNITO,
            authorizer=apigw.CognitoUserPoolsAuthorizer(
                self, "ReviewerAuthorizer", cognito_user_pools=[user_pool]),
        )
        reviews_integration = apigw.LambdaIntegration(reviews_fn)
        reviews_res = api.root.add_resource("reviews")
        reviews_res.add_method("GET", reviews_integration, **_opts(reviewer_auth))
        review = reviews_res.add_resource("{id}")
        review.add_method("GET", reviews_integration, **_opts(reviewer_auth))
        review.add_method("POST", reviews_integration, **_opts(reviewer_auth))
        review.add_resource("claim").add_method("POST", reviews_integration, **_opts(reviewer_auth))
        review.add_resource("release").add_method("POST", reviews_integration,
                                                  **_opts(reviewer_auth))

        self._publish_reviewer_app(site, distribution, user_pool, user_pool_client, auth_domain)

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
        CfnOutput(self, "ReviewAppUrl", value=app_url)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "AuthDomain", value=auth_domain.base_url())

    # -- reviewer sign-in ----------------------------------------------------

    def _add_reviewer_auth(self, removal: RemovalPolicy, app_url: str):
        """Cognito user pool for people. Accounts are created by an admin (no
        self sign-up); only members of the reviewers group can use the API."""
        user_pool = cognito.UserPool(
            self, "Reviewers",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False)),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            password_policy=cognito.PasswordPolicy(min_length=12),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=removal,
        )
        cognito.CfnUserPoolGroup(
            self, "ReviewerGroup", user_pool_id=user_pool.user_pool_id,
            group_name=REVIEWER_GROUP,
            description="Can work the document review queue")

        prefix = self.node.try_get_context("authDomainPrefix") or f"docreview-{self.account}"
        auth_domain = user_pool.add_domain(
            "SignInDomain", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=prefix))

        callback_urls = [app_url, LOCAL_DEV_URL]

        client = user_pool.add_client(
            "ReviewApp",
            generate_secret=False,  # browser app: authorization code + PKCE
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL,
                        cognito.OAuthScope.PROFILE],
                callback_urls=callback_urls,
                logout_urls=callback_urls,
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            prevent_user_existence_errors=True,
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.hours(12),  # one working day
        )
        return user_pool, client, auth_domain

    # -- reviewer web app ----------------------------------------------------

    def _add_reviewer_site(self, api: apigw.RestApi):
        """S3 + CloudFront for the React app, with the review API routed
        through the same distribution: one origin, so no CORS, and the API
        stays behind the same TLS and security headers."""
        site = s3.Bucket(
            self, "ReviewAppSite",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # Build output only: always safe to recreate.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        headers = cloudfront.ResponseHeadersPolicy(
            self, "SecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=CONTENT_SECURITY_POLICY, override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=True,
                    override=True),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True),
            ),
        )

        distribution = cloudfront.Distribution(
            self, "ReviewApp",
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,  # includes India
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=headers,
                compress=True,
            ),
            additional_behaviors={
                "/reviews*": cloudfront.BehaviorOptions(
                    origin=origins.RestApiOrigin(api),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    # Never cache API responses; forward the Authorization
                    # header so the Cognito authorizer sees the token.
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=(
                        cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER),
                    response_headers_policy=headers,
                ),
            },
        )
        return site, distribution

    def _publish_reviewer_app(self, site: s3.Bucket, distribution: cloudfront.Distribution,
                              user_pool: cognito.UserPool, client: cognito.UserPoolClient,
                              auth_domain: cognito.UserPoolDomain) -> None:
        if not (FRONTEND_DIST / "index.html").exists():
            Annotations.of(self).add_warning(
                "frontend/dist not found: the reviewer app will not be published. "
                "Run `npm ci && npm run build` in frontend/ before `cdk deploy`.")
            return

        # Hashed assets are immutable and cached for a year. index.html and
        # config.json must always be fresh, so they go in a second deployment
        # with no-cache. prune=False on both so they don't delete each
        # other's files (old hashed assets also keep open tabs working).
        assets = s3deploy.BucketDeployment(
            self, "ReviewAppAssets",
            destination_bucket=site,
            sources=[s3deploy.Source.asset(
                str(FRONTEND_DIST), exclude=["index.html", "config*.json"])],
            cache_control=[s3deploy.CacheControl.from_string(
                "public, max-age=31536000, immutable")],
            prune=False,
        )
        entry = s3deploy.BucketDeployment(
            self, "ReviewAppEntry",
            destination_bucket=site,
            sources=[
                s3deploy.Source.data("index.html", (FRONTEND_DIST / "index.html").read_text()),
                s3deploy.Source.json_data("config.json", {
                    "region": self.region,
                    "userPoolId": user_pool.user_pool_id,
                    "userPoolClientId": client.user_pool_client_id,
                    "authDomain": auth_domain.base_url(),
                    "apiBaseUrl": "",
                }),
            ],
            cache_control=[s3deploy.CacheControl.no_cache()],
            prune=False,
            distribution=distribution,
            distribution_paths=["/index.html", "/config.json"],
        )
        entry.node.add_dependency(assets)

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


def _opts(options: apigw.MethodOptions) -> dict:
    return {"authorization_type": options.authorization_type, "authorizer": options.authorizer}
