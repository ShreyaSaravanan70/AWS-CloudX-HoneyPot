import pathlib, sys

src = pathlib.Path(r"C:\Users\Dell\Documents\AWS-HaaS\backend\dynamo.py").read_text(encoding="utf-8")
src = src.replace("import boto3", "boto3 = None")
src = src.replace("from botocore.exceptions import ClientError", "class ClientError(Exception): pass")
src = src.replace("from .config import", "import sys; sys.path.insert(0, r'C:/Users/Dell/Documents/AWS-HaaS/backend'); from config import")
ns = {}
exec(compile(src, "dynamo.py", "exec"), ns)

parse_ts = ns["_parse_ts"]
bucket_key = ns["_bucket_key"]
format_label = ns["_format_label"]
events_over_time = ns["events_over_time"]

def fake_scan(limit=500):
    return ns["_TEST_ITEMS"][:limit]
ns["scan_items"] = fake_scan

print("=== _parse_ts tests ===")
from datetime import datetime, timezone
naive_ts = parse_ts("2026-09-11T12:00:00.123456")
offset_ts = parse_ts("2026-09-11T17:30:00.123456+05:30")
assert naive_ts == datetime(2026, 9, 11, 12, 0, 0, 123456, tzinfo=timezone.utc)
assert offset_ts == naive_ts
assert naive_ts.tzinfo is timezone.utc and offset_ts.tzinfo is timezone.utc
assert parse_ts("") is None
assert parse_ts(None) is None
print("  PASS: naive and offset timestamps normalize to aware UTC")

print("=== _bucket_key span adaptation ===")
dt = datetime(2026, 9, 11, 12, 7, 30)
assert bucket_key(dt, 60) == datetime(2026, 9, 11, 12, 5, 0), bucket_key(dt, 60)
assert bucket_key(dt, 360) == datetime(2026, 9, 11, 12, 0, 0), bucket_key(dt, 360)
assert bucket_key(dt, 2880) == datetime(2026, 9, 11, 12, 0, 0), bucket_key(dt, 2880)
assert bucket_key(dt, 20160) == datetime(2026, 9, 11, 0, 0, 0), bucket_key(dt, 20160)
assert bucket_key(dt, 99999) == datetime(2026, 9, 11, 12, 0, 0), bucket_key(dt, 99999)
print("  PASS: 5-min / 30-min / 2-hour / daily / 6-hour intervals")

print("=== _format_label alignment ===")
assert format_label(datetime(2026, 9, 11, 12, 7), 60) == "12:05"
assert format_label(datetime(2026, 9, 11, 12, 7), 360) == "12:00"
assert format_label(datetime(2026, 9, 11, 12, 7), 2880) == "12:00"
assert format_label(datetime(2026, 9, 11, 12, 7), 20160) == "09-11"
assert format_label(datetime(2026, 9, 11, 12, 7), 99999) == "2026-09-11"
print("  PASS: labels align to bucket interval")

print("=== events_over_time correctness ===")
ns["_TEST_ITEMS"] = [
    {"timestamp": "2026-09-11T12:00:00.123456"},
    {"timestamp": "2026-09-11T12:01:00.000000"},
    {"timestamp": "2026-09-11T12:05:00.000000"},
    {"timestamp": "2026-09-11T12:05:30.000000"},
    {"timestamp": "2026-09-11T12:10:00.000000"},
    {"timestamp": "2026-09-11T12:10:30.000000"},
    {"timestamp": "2026-09-11T12:10:45.000000"},
]
result = events_over_time(limit_buckets=24)
assert len(result) > 0
labels = [r["timestamp"] for r in result]
assert "12:00" in labels, labels
assert "12:05" in labels, labels
assert "12:10" in labels, labels
counts = {r["timestamp"]: r["events"] for r in result}
assert counts.get("12:00") == 2, counts
assert counts.get("12:05") == 2, counts
assert counts.get("12:10") == 3, counts
print("  PASS: 5-min buckets, counts match (2/2/3)")

print("=== API schema preserved ===")
assert all("timestamp" in r and "events" in r for r in result), "schema changed"
assert all(isinstance(r["events"], int) for r in result)
print("  PASS: response is [{timestamp, events}]")

print("=== empty data ===")
ns["_TEST_ITEMS"] = []
assert events_over_time() == []
print("  PASS: empty data returns []")

print("=== mixed timezone chart regression ===")
ns["_TEST_ITEMS"] = [
    {"timestamp": "2026-09-11T12:00:00"},
    {"timestamp": "2026-09-11T17:30:00+05:30"},
    {"timestamp": "2026-09-11T12:10:00Z"},
]
mixed_result = events_over_time()
assert {row["timestamp"]: row["events"] for row in mixed_result} == {
    "12:00": 2,
    "12:10": 1,
}
print("  PASS: mixed naive/aware values compare and bucket in UTC")

print("\nALL CHART TESTS PASSED")
