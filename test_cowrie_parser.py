"""Tests for the Cowrie log parser and its AWS Lambda handler."""

import importlib.util
import io
import sys
import types
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, call
from backend.cowrie import expected_events


PARSER_PATH = Path(__file__).parent / "Lambda Functions" / "CowrieLogParser.py"


class FakeStreamingBody:
    def __init__(self, data):
        self.stream = io.BytesIO(data)

    def iter_lines(self):
        while line := self.stream.readline():
            yield line.rstrip(b"\r\n")


def load_parser(monkeypatch):
    """Load the parser with boto3 replaced, so no AWS SDK client can escape."""
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = MagicMock(name="boto3.client")
    fake_boto3.resource = MagicMock(name="boto3.resource")
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    spec = importlib.util.spec_from_file_location("cowrie_parser_under_test", PARSER_PATH)
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    return parser, fake_boto3


def test_parse_valid_connection_and_events(monkeypatch):
    parser, _ = load_parser(monkeypatch)
    sessions = {}

    connection = parser.parse_log_line(
        "2026-09-11T12:00:00.123456Z [HoneyPotSSHTransport] New connection: "
        "203.0.113.8:44444 (10.0.0.1:22) [session: abc123]",
        sessions,
    )
    auth = parser.parse_log_line(
        "2026-09-11T12:00:01.123456Z [SSHService ssh-userauth,203.0.113.8] [abc123] "
        "login attempt [b'root'/b'password'] failed",
        sessions,
    )
    command = parser.parse_log_line(
        "2026-09-11T12:00:02.123456Z [cowrie.command.input,203.0.113.8] [abc123] "
        "CMD: id",
        sessions,
    )

    assert connection["session"] == "abc123"
    assert connection["timestamp"] == "2026-09-11T12:00:00.123456"
    assert auth["session"] == "abc123"
    assert auth["timestamp"] == "2026-09-11T12:00:01.123456"
    assert auth["input"] == "root:password"
    assert command["session"] == "abc123"
    assert command["timestamp"] == "2026-09-11T12:00:02.123456"
    assert command["command"] == "id"


def test_malformed_and_system_lines(monkeypatch):
    parser, _ = load_parser(monkeypatch)
    sessions = {}

    malformed = parser.parse_log_line("not a timestamp or Cowrie event", sessions)
    ignored = parser.parse_log_line(
        "2026-09-11T12:00:00.000000Z Failed to import optional module", sessions
    )

    assert malformed["timestamp"] is None
    assert malformed["session"] == "unknown"
    assert malformed["eventid"] == "unknown"
    with TestCase().assertRaises(ValueError):
        parser.parse_log_line("2026-99-99T99:99:99.1Z [event]", sessions)
    assert ignored is None


def test_lambda_handler_reads_s3_and_writes_expected_dynamodb_items(monkeypatch):
    parser, fake_boto3 = load_parser(monkeypatch)
    fixture = (Path(__file__).parent / "test_cowrie_upload.txt").read_text(encoding="utf-8")
    fixture_lines = fixture.splitlines()
    log_text = [
        "\n".join(fixture_lines[:2]).encode("utf-8"),
        fixture_lines[2].encode("utf-8"),
    ]
    s3 = MagicMock(name="s3 client")
    s3.get_object.side_effect = [
        {"Body": FakeStreamingBody(body)} for body in log_text
    ]
    fake_boto3.client.return_value = s3

    batch = MagicMock(name="DynamoDB batch writer")
    batch_context = MagicMock()
    batch_context.__enter__.return_value = batch
    table = MagicMock(name="DynamoDB table")
    table.batch_writer.return_value = batch_context
    dynamodb = MagicMock(name="DynamoDB resource")
    dynamodb.Table.return_value = table
    fake_boto3.resource.return_value = dynamodb

    event = {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "cowrie-logs"},
                    "object": {"key": "incoming%2Fcowrie+log.json"},
                }
            },
            {
                "s3": {
                    "bucket": {"name": "cowrie-logs-2"},
                    "object": {"key": "incoming%2Fsecond.log"},
                }
            },
        ]
    }

    assert parser.lambda_handler(event, None) == {"statusCode": 200}
    assert s3.get_object.call_args_list == [
        call(Bucket="cowrie-logs", Key="incoming/cowrie log.json"),
        call(Bucket="cowrie-logs-2", Key="incoming/second.log"),
    ]
    dynamodb.Table.assert_called_once_with("CowrieLogs")
    fake_boto3.client.assert_called_once_with("s3", region_name=parser.AWS_REGION)
    fake_boto3.resource.assert_called_once_with("dynamodb", region_name=parser.AWS_REGION)

    written = [call.kwargs["Item"] for call in batch.put_item.call_args_list]
    assert len(written) == 3
    assert [item["timestamp"] for item in written] == [
        "2026-09-11T12:00:00.123456",
        "2026-09-11T12:00:01.123456",
        "2026-09-11T12:00:02.123456",
    ]
    assert all(item["session"] == "abc123" for item in written)
    assert all("timestamp" in item and "session" in item for item in written)
    assert all(isinstance(item["session"], str) for item in written)
    assert all(isinstance(item["timestamp"], str) for item in written)
    assert [item["source_key"] for item in written] == [
        "incoming/cowrie log.json",
        "incoming/cowrie log.json",
        "incoming/second.log",
    ]
    assert written[1]["input"] == "root:password"
    assert written[2]["command"] == "uname -a"
    assert {(item["session"], item["timestamp"]) for item in written} == expected_events(fixture)


def test_parser_error_logs_do_not_include_cowrie_payloads(monkeypatch):
    parser, fake_boto3 = load_parser(monkeypatch)
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": FakeStreamingBody(
        b"2026-99-99T12:00:00.123456Z [SSHService] login attempt "
        b"[b'root'/b'secret-password'] failed"
    )}
    fake_boto3.client.return_value = s3
    batch = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = batch
    table = MagicMock()
    table.batch_writer.return_value = context
    fake_boto3.resource.return_value.Table.return_value = table

    from unittest.mock import patch
    with patch("builtins.print") as output:
        parser.lambda_handler({"Records": [{"s3": {
            "bucket": {"name": "logs"}, "object": {"key": "uploads/test.log"}
        }}]}, None)
    text = " ".join(str(call) for call in output.call_args_list)
    assert "secret-password" not in text
    assert "login attempt" not in text
    assert "ValueError" in text


def test_stream_processor_handles_insert_records_only(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "stream_processor", Path(__file__).parent / "Lambda Functions" / "LogDynamoDBToCloudWatch.py"
    )
    processor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(processor)
    event = {"Records": [
        {"eventName": "INSERT", "dynamodb": {"NewImage": {
            "session": {"S": "s1"}, "timestamp": {"S": "t1"},
            "src_ip": {"S": "203.0.113.8"}, "eventid": {"S": "CowrieSSHFactory"},
            "source_key": {"S": "uploads/test.log"},
            "input": {"S": "root:very-secret-password"},
            "command": {"S": "curl --token=secret-token"},
        }}},
        {"eventName": "MODIFY", "dynamodb": {"NewImage": {"session": {"S": "s2"}}}},
        {"eventName": "REMOVE", "dynamodb": {"OldImage": {"session": {"S": "s3"}}}},
    ]}
    from unittest.mock import patch
    with patch("builtins.print") as output:
        assert processor.lambda_handler(event, None) == {"statusCode": 200}
    text = " ".join(str(call) for call in output.call_args_list)
    assert "s1" in text and "t1" in text
    assert "203.0.113.8" in text and "CowrieSSHFactory" in text
    assert "uploads/test.log" in text and "[REDACTED]" in text
    assert "very-secret-password" not in text
    assert "secret-token" not in text
    assert "s2" not in text and "s3" not in text


class _MonkeyPatch:
    def __init__(self):
        self._patches = []

    def setitem(self, mapping, key, value):
        from unittest.mock import patch
        patcher = patch.dict(mapping, {key: value})
        patcher.start()
        self._patches.append(patcher)

    def undo(self):
        for patcher in reversed(self._patches):
            patcher.stop()


if __name__ == "__main__":
    import traceback

    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failures = 0
    for test in tests:
        monkeypatch = _MonkeyPatch()
        try:
            test(monkeypatch)
            print(f"PASS {test.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
        finally:
            monkeypatch.undo()
    print(f"Ran {len(tests)} tests; {failures} failures")
    raise SystemExit(bool(failures))
