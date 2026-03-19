# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pytest configuration — mock boto3 so tests run without AWS SDK installed."""

import sys
from unittest import mock

# Create mock boto3 and botocore before any s3arc modules are imported
mock_boto3 = mock.MagicMock()
mock_botocore = mock.MagicMock()
mock_botocore_exceptions = mock.MagicMock()

# Make BotoCoreError and ClientError behave as exception classes
mock_botocore_exceptions.BotoCoreError = type("BotoCoreError", (Exception,), {})
mock_botocore_exceptions.ClientError = type("ClientError", (Exception,), {
    "__init__": lambda self, error_response=None, operation_name=None: Exception.__init__(self)
})

sys.modules.setdefault("boto3", mock_boto3)
sys.modules.setdefault("botocore", mock_botocore)
sys.modules.setdefault("botocore.exceptions", mock_botocore_exceptions)
