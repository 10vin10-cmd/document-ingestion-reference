#!/usr/bin/env python3
import aws_cdk as cdk

from infra.ingestion_stack import IngestionStack

app = cdk.App()
IngestionStack(app, "DocumentIngestion")
app.synth()
