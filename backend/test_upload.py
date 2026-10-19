"""Mocked API tests for log upload and DynamoDB processing confirmation."""

import io
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import backend.app as app_module
from backend import config, probes
from backend.cowrie import expected_events

MAX_UPLOAD_BYTES = app_module.MAX_UPLOAD_BYTES


class UploadApiTests(unittest.TestCase):
    def setUp(self):
        self.s3 = MagicMock()
        self.s3_context = patch("backend.app.aws_client", return_value=self.s3)
        self.s3_context.start()
        self.addCleanup(self.s3_context.stop)
        self.client = TestClient(app_module.app)

    def test_upload_uses_unique_key_and_existing_bucket(self):
        payload = (Path(__file__).resolve().parents[1] / "test_cowrie_upload.txt").read_bytes()
        self.assertEqual(len(expected_events(payload.decode("utf-8"))), 3)
        response = self.client.post(
            "/api/uploads",
            files={"file": ("test_cowrie_upload.log", io.BytesIO(payload), "text/plain")},
        )

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "uploaded")
        self.assertEqual(len(body["upload_id"]), 32)
        self.s3.put_object.assert_called_once()
        kwargs = self.s3.put_object.call_args.kwargs
        self.assertEqual(kwargs["Bucket"], app_module.HAA_S3_BUCKET)
        self.assertEqual(kwargs["Key"], f"uploads/{body['upload_id']}.log")
        self.assertEqual(kwargs["Body"], payload)
        second = self.client.post(
            "/api/uploads",
            files={"file": ("test_cowrie_upload.log", io.BytesIO(payload), "text/plain")},
        )
        self.assertEqual(second.status_code, 202)
        self.assertNotEqual(body["upload_id"], second.json()["upload_id"])

    def test_checked_in_fixture_is_accepted_as_selected_log_file(self):
        payload = (Path(__file__).resolve().parents[1] / "test_cowrie_upload.txt").read_bytes()
        response = self.client.post(
            "/api/uploads",
            files={"file": ("test_cowrie_upload.log", io.BytesIO(payload), "text/plain")},
        )
        self.assertEqual(response.status_code, 202, response.json())
        self.assertEqual(response.json()["status"], "uploaded")
        self.s3.put_object.assert_called_once()
        self.assertEqual(self.s3.put_object.call_args.kwargs["Body"], payload)

    def test_rejects_empty_invalid_and_oversized_files(self):
        empty = self.client.post("/api/uploads", files={"file": ("empty.log", b"")})
        invalid_name = self.client.post("/api/uploads", files={"file": ("../bad.log", b"x")})
        invalid_type = self.client.post("/api/uploads", files={"file": ("notes.txt", b"x")})
        invalid_text = self.client.post("/api/uploads", files={"file": ("binary.log", b"\xff")})
        no_events = self.client.post("/api/uploads", files={"file": ("plain.log", b"not a Cowrie event")})
        malformed_cowrie = self.client.post(
            "/api/uploads",
            files={"file": ("bad.log", b"2026-09-11T12:00:00Z New connection: 203.0.113.8")},
        )
        oversized = self.client.post(
            "/api/uploads", files={"file": ("large.log", b"x" * (MAX_UPLOAD_BYTES + 1))}
        )

        self.assertEqual(empty.status_code, 400)
        self.assertEqual(invalid_name.status_code, 400)
        self.assertEqual(invalid_type.status_code, 400)
        self.assertEqual(invalid_text.status_code, 400)
        self.assertEqual(no_events.status_code, 400)
        self.assertIn("connection line matching", no_events.json()["detail"])
        self.assertEqual(malformed_cowrie.status_code, 400)
        self.assertEqual(oversized.status_code, 413)
        self.s3.put_object.assert_not_called()

    def _connection_records(self, count):
        return "\n".join(
            f"2026-09-11T12:00:{i:02d}.123456Z [HoneyPotSSHTransport] New connection: "
            f"203.0.113.{i + 1}:44444 (10.0.0.1:22) [session: s{i}]"
            for i in range(count)
        ).encode("utf-8")

    def test_accepts_one_batch_of_25_events_and_rejects_26(self):
        accepted = self._connection_records(app_module.MAX_UPLOAD_EVENTS)
        self.assertLessEqual(len(accepted), MAX_UPLOAD_BYTES)
        self.assertEqual(
            len(expected_events(accepted.decode("utf-8"))), app_module.MAX_UPLOAD_EVENTS
        )
        response = self.client.post(
            "/api/uploads", files={"file": ("25-events.log", accepted, "text/plain")}
        )
        self.assertEqual(response.status_code, 202)
        self.s3.put_object.assert_called_once()

        self.s3.put_object.reset_mock()
        too_many = self._connection_records(app_module.MAX_UPLOAD_EVENTS + 1)
        rejected = self.client.post(
            "/api/uploads", files={"file": ("26-events.log", too_many, "text/plain")}
        )
        self.assertEqual(rejected.status_code, 413)
        self.assertIn("25-event limit", rejected.json()["detail"])
        self.s3.put_object.assert_not_called()

    def test_reports_s3_upload_failure_without_success_status(self):
        payload = (
            b"2026-09-11T12:00:00.123456Z [HoneyPotSSHTransport] New connection: "
            b"203.0.113.8:44444 (10.0.0.1:22) [session: abc123]\n"
        )
        self.s3.put_object.side_effect = RuntimeError("unavailable")
        response = self.client.post(
            "/api/uploads", files={"file": ("cowrie.log", payload, "text/plain")}
        )
        self.assertEqual(response.status_code, 502)
        self.assertNotEqual(response.json().get("status"), "uploaded")

    def test_status_requires_dynamodb_confirmation(self):
        upload_id = "a" * 32
        first = ("abc123", "2026-09-11T12:00:00.123456")
        second = ("abc123", "2026-09-11T12:01:00.123456")
        app_module.UPLOAD_EXPECTED[upload_id] = (time.monotonic(), {first, second})
        with patch("backend.app.dynamo.upload_events", return_value={first}):
            pending = self.client.get(f"/api/uploads/{upload_id}")
        with patch(
            "backend.app.dynamo.upload_events",
            return_value={first, second},
        ):
            processed = self.client.get(f"/api/uploads/{upload_id}")
        invalid = self.client.get("/api/uploads/not-an-id")

        self.assertEqual(pending.json()["status"], "processing")
        self.assertEqual(pending.json()["events_detected"], 1)
        self.assertEqual(processed.json()["status"], "processed")
        self.assertEqual(processed.json()["events_detected"], 2)
        self.assertEqual(invalid.status_code, 404)
        app_module.UPLOAD_EXPECTED.pop(upload_id, None)

    def test_dynamodb_check_paginates_until_every_expected_key_is_found(self):
        first = {"session": "s1", "timestamp": "t1"}
        second = {"session": "s2", "timestamp": "t2"}
        table = MagicMock()
        table.scan.side_effect = [
            {"Items": [first], "LastEvaluatedKey": {"session": "s1"}},
            {"Items": [second]},
        ]
        with patch("backend.dynamo._table", return_value=table):
            found = app_module.dynamo.upload_events("uploads/id.log", {("s1", "t1"), ("s2", "t2")})

        self.assertEqual(found, {("s1", "t1"), ("s2", "t2")})
        self.assertEqual(table.scan.call_count, 2)
        self.assertTrue(table.scan.call_args_list[0].kwargs["ConsistentRead"])

    def test_pipeline_endpoint_uses_configured_region_without_aws(self):
        with patch.multiple(
            probes,
            _probe_s3=MagicMock(return_value={"status": "ok"}),
            _probe_dynamodb=MagicMock(return_value={"status": "ok"}),
            _probe_lambda=MagicMock(return_value={"status": "ok"}),
            _probe_event_source_mapping=MagicMock(return_value={"status": "ok"}),
            _probe_cloudwatch=MagicMock(return_value={"status": "ok"}),
        ):
            response = self.client.get("/api/pipeline")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["region"], config.AWS_REGION)

    def test_access_denied_probes_name_missing_permissions(self):
        cases = [
            (probes._probe_s3, "s3:ListBucket", "AccessDenied", 403),
            (probes._probe_dynamodb, "dynamodb:DescribeTable", "AccessDeniedException", 403),
            (lambda: probes._probe_lambda("CowrieLogParser"), "lambda:GetFunction", "AccessDeniedException", 403),
            (probes._probe_event_source_mapping, "lambda:ListEventSourceMappings", "AccessDenied", 403),
            (probes._probe_cloudwatch, "logs:DescribeLogGroups", "AccessDenied", 403),
        ]
        for probe, permission, code, status in cases:
            with self.subTest(permission=permission):
                aws_client = MagicMock()
                aws_client.head_bucket.side_effect = ClientError(
                    {"Error": {"Code": code, "Message": "denied"}, "ResponseMetadata": {"HTTPStatusCode": status}},
                    "probe",
                )
                aws_client.describe_table.side_effect = aws_client.head_bucket.side_effect
                aws_client.get_function.side_effect = aws_client.head_bucket.side_effect
                aws_client.list_event_source_mappings.side_effect = aws_client.head_bucket.side_effect
                aws_client.describe_log_groups.side_effect = aws_client.head_bucket.side_effect
                with patch("backend.probes._client", return_value=aws_client):
                    result = probe()
                self.assertIn("Access denied", result["message"])
                self.assertIn(permission, result["message"])
                self.assertNotIn("not found", result["message"])

    def test_lambda_not_found_is_distinct_from_access_denied(self):
        aws_client = MagicMock()
        aws_client.get_function.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "GetFunction",
        )
        with patch("backend.probes._client", return_value=aws_client):
            result = probes._probe_lambda("CowrieLogParser")
        self.assertIn("not found", result["message"])
        self.assertNotIn("Access denied", result["message"])

    def test_s3_missing_bucket_is_distinct_from_access_denied(self):
        aws_client = MagicMock()
        aws_client.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "missing"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadBucket",
        )
        with patch("backend.probes._client", return_value=aws_client):
            result = probes._probe_s3()
        self.assertIn("not found", result["message"])
        self.assertNotIn("Access denied", result["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
