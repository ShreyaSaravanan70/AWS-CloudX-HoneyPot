"""DynamoDB helpers for the HaaS backend.

The CowrieLogs table schema (created by 01_setup.sh / deploy_aws.sh):
  partition key: session  (String, HASH)
  sort key:      timestamp (String, RANGE)
  attributes:    session, timestamp, src_ip, eventid, input, command

All queries respect this schema. We never scan the whole table on every
request - recent events use a key-condition query on the sort key.
"""

from botocore.exceptions import ClientError
from datetime import datetime, timezone

from .config import AWS_MODE, HAA_DYNAMODB_TABLE


def _client():
    from .aws import resource as aws_resource
    return aws_resource("dynamodb")


def _table():
    return _client().Table(HAA_DYNAMODB_TABLE)


def table_exists():
    """Return True if the table can be described, False otherwise."""
    try:
        _table().table_status  # triggers a DescribeTable call
        return True
    except ClientError:
        return False


def scan_items(limit=200):
    """Full scan, bounded. Used only for aggregate stats (rare)."""
    items = []
    kwargs = {"Limit": limit}
    while True:
        resp = _table().scan(**kwargs)
        items.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        if len(items) >= limit:
            break
    return items[:limit]


def upload_events(object_key, expected_keys):
    """Find all expected rows written by the parser for one S3 object."""
    from boto3.dynamodb.conditions import Attr

    table = _table()
    found = set()
    kwargs = {
        "FilterExpression": Attr("source_key").eq(object_key),
        "ProjectionExpression": "#session, #timestamp, #source",
        "ExpressionAttributeNames": {
            "#session": "session",
            "#timestamp": "timestamp",
            "#source": "source_key",
        },
        "ConsistentRead": True,
    }
    while True:
        response = table.scan(**kwargs)
        found.update(
            (item["session"], item["timestamp"])
            for item in response.get("Items", [])
        )
        if expected_keys.issubset(found) or "LastEvaluatedKey" not in response:
            return found
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def query_recent(limit=50):
    """Recent events. DynamoDB has no global secondary index, so we scan
    the most recent items by scanning and sorting by timestamp. This is
    bounded to `limit` items."""
    items = scan_items(limit=max(limit * 4, 200))
    items.sort(key=lambda i: i.get("timestamp", ""), reverse=True)
    return items[:limit]


def query_by_session(session, limit=50):
    """All events for a session (uses the partition key - efficient)."""
    resp = _table().query(
        KeyConditionExpression="#s = :session",
        ExpressionAttributeNames={"#s": "session"},
        ExpressionAttributeValues={":session": session},
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


def query_by_ip(src_ip, limit=50):
    """Events for a source IP. Not a key attribute, so a bounded scan is
    used. This is acceptable because the table is small in local mode."""
    items = scan_items(limit=max(limit * 4, 200))
    return [i for i in items if i.get("src_ip") == src_ip][:limit]


def stats():
    """Aggregate statistics over the most recent bounded scan."""
    items = scan_items(limit=500)
    total = len(items)
    ips = {i.get("src_ip") for i in items if i.get("src_ip", "unknown") != "unknown"}
    sessions = {i.get("session") for i in items if i.get("session", "unknown") != "unknown"}
    creds = [i for i in items if i.get("input") and i.get("input") != "none"]
    return {
        "total_events": total,
        "unique_source_ips": len(ips),
        "unique_sessions": len(sessions),
        "credential_attempts": len(creds),
        "data_source": "mock/demo" if AWS_MODE == "local" else "real",
    }


def top_ips(limit=10):
    """Top source IPs by event count."""
    items = scan_items(limit=500)
    counts = {}
    for i in items:
        ip = i.get("src_ip", "unknown")
        counts[ip] = counts.get(ip, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"src_ip": ip, "events": n} for ip, n in ranked]


def _parse_ts(ts):
    """Parse an ISO timestamp and normalize it to a timezone-aware UTC value."""
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        # Fallback: strip trailing Z and microseconds if present
        try:
            parsed = datetime.strptime(ts.rstrip("Z"), "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bucket_key(dt, span_minutes):
    """Return a bucket key for a datetime, bucketed to a sensible interval.

    The interval adapts to the data span so the chart never shows a
    single giant bucket or dozens of near-empty ones:
      span <= 1 hour   -> 5-minute buckets
      span <= 6 hours  -> 30-minute buckets
      span <= 2 days   -> 2-hour buckets
      span <= 14 days  -> 1-day buckets
      otherwise        -> 6-hour buckets
    """
    if span_minutes <= 60:
        return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)
    if span_minutes <= 360:
        return dt.replace(minute=(dt.minute // 30) * 30, second=0, microsecond=0)
    if span_minutes <= 2880:
        return dt.replace(hour=(dt.hour // 2) * 2, minute=0, second=0, microsecond=0)
    if span_minutes <= 20160:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(hour=(dt.hour // 6) * 6, minute=0, second=0, microsecond=0)


def _format_label(dt, span_minutes):
    """Human-readable bucket label.

    Labels are aligned to the *same* bucket interval used by _bucket_key so
    that the label on the x-axis matches the bucket the event was counted
    into (e.g. a 12:07 event in a 5-minute bucket shows "12:05", not "12:07").
    Shorter spans show time only; wider spans show date or month.
    """
    aligned = _bucket_key(dt, span_minutes)
    if span_minutes <= 2880:  # <= 2 days -> HH:MM
        return aligned.strftime("%H:%M")
    if span_minutes <= 20160:  # <= 14 days -> Mon DD
        return aligned.strftime("%m-%d")
    return aligned.strftime("%Y-%m-%d")


def events_over_time(limit_buckets=24):
    """Event counts bucketed by timestamp.

    Buckets adapt to the data span so the activity chart shows useful,
    readable intervals. The API response format is unchanged:
        [{"timestamp": "<label>", "events": <count>}, ...]
    """
    items = scan_items(limit=500)
    parsed = []
    for i in items:
        dt = _parse_ts(i.get("timestamp", ""))
        if dt is not None:
            parsed.append(dt)

    if not parsed:
        return []

    earliest = min(parsed)
    latest = max(parsed)
    span_minutes = max(1, int((latest - earliest).total_seconds() // 60))

    buckets = {}
    for dt in parsed:
        key = _bucket_key(dt, span_minutes)
        buckets[key] = buckets.get(key, 0) + 1

    ranked = sorted(buckets.items())[-limit_buckets:]
    return [
        {"timestamp": _format_label(k, span_minutes), "events": v}
        for k, v in ranked
    ]
