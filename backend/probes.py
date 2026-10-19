"""Best-effort pipeline health probes.

Each probe tries one AWS call and reports success/failure with a
human-readable message. Nothing here creates, modifies, or deletes
resources - only read-only describe/list calls.
"""

from botocore.exceptions import ClientError, EndpointConnectionError

from .aws import client as aws_client
from .config import (
    AWS_MODE, AWS_REGION,
    HAA_S3_BUCKET, HAA_DYNAMODB_TABLE,
    HAA_PARSER_LAMBDA, HAA_CLOUDWATCH_LAMBDA,
)


def _client(service, **extra):
    return aws_client(service, **extra)


def _ok(message, detail=None):
    return {"status": "ok", "message": message, "detail": detail}


def _fail(message, detail=None):
    return {"status": "fail", "message": message, "detail": detail}


def _client_failure(e, resource, permission, missing_codes=()):
    error = e.response.get("Error", {})
    code = error.get("Code", "Unknown")
    status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    detail = error.get("Message", str(e))
    if code in missing_codes:
        return _fail(f"{resource} not found", detail)
    if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"} or status == 403:
        suffix = "; S3 may also return 403 for a missing bucket" if resource.startswith("S3 bucket") else ""
        return _fail(f"Access denied checking {resource}; requires {permission}{suffix}", detail)
    return _fail(f"Could not check {resource} ({code})", detail)


def _probe_s3():
    try:
        _client("s3").head_bucket(Bucket=HAA_S3_BUCKET)
        return _ok(f"S3 bucket {HAA_S3_BUCKET} reachable")
    except ClientError as e:
        return _client_failure(
            e, f"S3 bucket {HAA_S3_BUCKET}", "s3:ListBucket",
            {"NoSuchBucket", "404", "NotFound"},
        )
    except EndpointConnectionError as e:
        return _fail("S3 endpoint unreachable", str(e))


def _probe_dynamodb():
    try:
        c = _client("dynamodb")
        resp = c.describe_table(TableName=HAA_DYNAMODB_TABLE)
        streams = resp.get("Table", {}).get("LatestStreamArn")
        status = resp.get("Table", {}).get("TableStatus")
        return _ok(
            f"DynamoDB table {HAA_DYNAMODB_TABLE} is {status}",
            {"stream_arn": streams, "streams_enabled": bool(streams)},
        )
    except ClientError as e:
        return _client_failure(
            e, f"DynamoDB table {HAA_DYNAMODB_TABLE}", "dynamodb:DescribeTable",
            {"ResourceNotFoundException"},
        )
    except EndpointConnectionError as e:
        return _fail("DynamoDB endpoint unreachable", str(e))


def _probe_lambda(name):
    try:
        c = _client("lambda")
        resp = c.get_function(FunctionName=name)
        state = resp.get("Configuration", {}).get("State")
        runtime = resp.get("Configuration", {}).get("Runtime")
        if state == "Active":
            return _ok(f"Lambda {name} is Active", {"runtime": runtime})
        return _fail(f"Lambda {name} is {state}", {"runtime": runtime})
    except ClientError as e:
        return _client_failure(
            e, f"Lambda {name}", "lambda:GetFunction", {"ResourceNotFoundException"}
        )
    except EndpointConnectionError as e:
        return _fail("Lambda endpoint unreachable", str(e))


def _probe_event_source_mapping():
    """Check that LogDynamoDBToCloudWatch has a stream mapping."""
    try:
        c = _client("lambda")
        resp = c.list_event_source_mappings(
            FunctionName=HAA_CLOUDWATCH_LAMBDA
        )
        mappings = resp.get("EventSourceMappings", [])
        if mappings:
            return _ok(
                f"{len(mappings)} event source mapping(s) for {HAA_CLOUDWATCH_LAMBDA}",
                {"state": mappings[0].get("State")},
            )
        return _fail(
            f"No event source mapping for {HAA_CLOUDWATCH_LAMBDA}",
            "Stream trigger missing",
        )
    except ClientError as e:
        return _client_failure(
            e, f"event source mappings for {HAA_CLOUDWATCH_LAMBDA}",
            "lambda:ListEventSourceMappings",
        )
    except EndpointConnectionError as e:
        return _fail("Lambda endpoint unreachable", str(e))


def _probe_cloudwatch():
    """Best-effort: describe the log group the stream Lambda writes to."""
    try:
        c = _client("logs")
        # The stream Lambda prints to stdout, which Lambda ships to
        # CloudWatch Logs automatically. We just verify the client works.
        c.describe_log_groups(limit=1)
        return _ok("CloudWatch Logs client available")
    except ClientError as e:
        return _client_failure(e, "CloudWatch Logs", "logs:DescribeLogGroups")
    except EndpointConnectionError as e:
        return _fail("CloudWatch Logs endpoint unreachable", str(e))


def pipeline_status():
    """Return best-effort status of every pipeline stage."""
    return {
        "mode": AWS_MODE,
        "region": AWS_REGION,
        "s3": _probe_s3(),
        "dynamodb": _probe_dynamodb(),
        "parser_lambda": _probe_lambda(HAA_PARSER_LAMBDA),
        "cloudwatch_lambda": _probe_lambda(HAA_CLOUDWATCH_LAMBDA),
        "stream_mapping": _probe_event_source_mapping(),
        "cloudwatch": _probe_cloudwatch(),
    }
