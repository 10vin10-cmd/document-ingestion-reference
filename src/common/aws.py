"""Lazily created AWS clients, so handlers import cleanly in tests."""

import functools
import os

import boto3
from botocore.config import Config

from common.repository import DocumentRepository


@functools.cache
def documents() -> DocumentRepository:
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return DocumentRepository(table)


@functools.cache
def client(service: str):
    if service == "s3":
        # Presigned URLs must use the bucket's regional, virtual-hosted
        # endpoint; the global endpoint redirects, which breaks SigV4 URLs.
        region = boto3.session.Session().region_name
        return boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return boto3.client(service)
