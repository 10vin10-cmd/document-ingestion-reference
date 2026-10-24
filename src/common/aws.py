"""Lazily created AWS clients, so handlers import cleanly in tests."""

import functools
import os

import boto3

from common.repository import DocumentRepository


@functools.cache
def documents() -> DocumentRepository:
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return DocumentRepository(table)


@functools.cache
def client(service: str):
    return boto3.client(service)
