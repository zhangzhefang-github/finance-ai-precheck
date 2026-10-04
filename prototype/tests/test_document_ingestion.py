from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from prototype.document_ingestion import (
    DocumentParsingService,
    IngestionError,
    MinerUClient,
    Repository,
    Settings,
    extract_required_artifacts,
    safe_message,
)


def result_zip(markdown: bytes = b"# invoice\n", content: bytes = b"[]") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("job/full.md", markdown)
        archive.writestr("job/invoice_content_list.json", content)
    return output.getvalue()


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.file_uploads = 0
        self.presigns = 0

    def put_file(self, key: str, path: Path, media_type: str) -> None:
        self.file_uploads += 1
        self.objects[key] = path.read_bytes()

    def put_bytes(self, key: str, value: bytes, media_type: str) -> None:
        self.objects[key] = value

    def exists(self, key: str) -> bool:
        return key in self.objects

    def size_bytes(self, key: str) -> int | None:
        value = self.objects.get(key)
        return len(value) if value is not None else None

    def presign_get(self, key: str, expires_seconds: int) -> str:
        self.presigns += 1
        return f"https://private.example/{key}?secret-signature=never-log-this"

    def get_bytes(self, key: str, max_bytes: int) -> bytes:
        value = self.objects[key]
        if len(value) > max_bytes:
            raise IngestionError("ARTIFACT_TOO_LARGE", "too large")
        return value


class FakeMinerU:
    def __init__(self, zip_value: bytes | None = None) -> None:
        self.submissions: list[dict] = []
        self.queries: list[dict] = []
        self.zip_value = zip_value if zip_value is not None else result_zip()

    def submit(self, signed_url: str, config: dict, data_id: str) -> str:
        self.submissions.append({"config": config, "data_id": data_id})
        return f"task-{len(self.submissions)}"

    def query(self, task_id: str) -> dict:
        return self.queries.pop(0) if self.queries else {"state": "running"}

    def download_zip(self, url: str, max_bytes: int) -> bytes:
        if len(self.zip_value) > max_bytes:
            raise IngestionError("RESULT_ZIP_TOO_LARGE", "too large")
        return self.zip_value


class DocumentIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = Repository(self.root / "metadata.sqlite3")
        self.storage = FakeStorage()
        self.mineru = FakeMinerU()
        self.settings = Settings(cos_bucket="test-bucket", cos_region="test-region")
        self.service = DocumentParsingService(
            self.repository,
            self.storage,
            self.mineru,
            self.settings,
            url_verifier=lambda _url, _timeout: 1,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_environment_does_not_default_to_a_deployed_cos_resource(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertEqual("", settings.cos_bucket)
        self.assertEqual("", settings.cos_region)
        with self.assertRaisesRegex(IngestionError, "COS_BUCKET, COS_REGION"):
            settings.require_cos()

    def write_pdf(self, name: str, suffix: bytes = b"") -> Path:
        path = self.root / name
        path.write_bytes(b"%PDF-1.4\n% fake test document\n" + suffix)
        return path

    def test_same_content_with_different_name_reuses_document_and_upload(self) -> None:
        first = self.service.ingest(self.write_pdf("first.pdf"))
        second = self.service.ingest(self.write_pdf("renamed.pdf"))

        self.assertEqual(first["document_id"], second["document_id"])
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(1, self.storage.file_uploads)

    def test_content_must_match_extension(self) -> None:
        invalid = self.root / "not-really.pdf"
        invalid.write_bytes(b"\x89PNG\r\n\x1a\n")
        with self.assertRaisesRegex(IngestionError, "does not match"):
            self.service.ingest(invalid)

    def test_cos_upload_failure_leaves_document_failed(self) -> None:
        def fail_upload(_key: str, _path: Path, _media_type: str) -> None:
            raise RuntimeError("COS unavailable")

        self.storage.put_file = fail_upload  # type: ignore[method-assign]
        source = self.write_pdf("invoice.pdf")
        with self.assertRaisesRegex(IngestionError, "COS unavailable"):
            self.service.ingest(source)
        import hashlib

        document = self.repository.get_document_by_sha(hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertIsNotNone(document)
        self.assertEqual("failed", document["storage_status"])
        self.assertEqual("COS_UPLOAD_FAILED", document["error_code"])

        self.storage.put_file = (  # type: ignore[method-assign]
            lambda key, path, _media_type: self.storage.objects.__setitem__(key, path.read_bytes())
        )
        recovered = self.service.ingest(source)
        self.assertFalse(recovered["reused"])
        self.assertEqual("ready", recovered["storage_status"])

    def test_active_and_successful_same_config_are_reused(self) -> None:
        document = self.service.ingest(self.write_pdf("invoice.pdf"))
        first = self.service.start_parse(document["document_id"])
        active = self.service.start_parse(document["document_id"])
        self.assertEqual(first["parse_run_id"], active["parse_run_id"])
        self.assertTrue(active["reused"])
        self.assertEqual(1, len(self.mineru.submissions))

        self.mineru.queries.append({"state": "done", "full_zip_url": "https://results.example/job.zip"})
        succeeded = self.service.sync(first["parse_run_id"])
        cached = self.service.start_parse(document["document_id"])
        self.assertEqual("succeeded", succeeded["status"])
        self.assertEqual(first["parse_run_id"], cached["parse_run_id"])
        self.assertEqual(1, len(self.mineru.submissions))

    def test_force_reuses_same_config_while_run_is_active(self) -> None:
        document = self.service.ingest(self.write_pdf("invoice.pdf"))
        first = self.service.start_parse(document["document_id"])
        forced = self.service.start_parse(document["document_id"], force=True)

        self.assertEqual(first["parse_run_id"], forced["parse_run_id"])
        self.assertTrue(forced["reused"])
        self.assertEqual(1, len(self.mineru.submissions))

    def test_config_change_creates_new_run(self) -> None:
        document = self.service.ingest(self.write_pdf("invoice.pdf"))
        first = self.service.start_parse(document["document_id"])
        self.mineru.queries.append({"state": "done", "full_zip_url": "https://results.example/1.zip"})
        self.service.sync(first["parse_run_id"])

        changed = self.service.start_parse(document["document_id"], is_ocr=True)
        self.assertNotEqual(first["parse_run_id"], changed["parse_run_id"])

    def test_force_after_success_creates_new_run(self) -> None:
        document = self.service.ingest(self.write_pdf("invoice.pdf"))
        first = self.service.start_parse(document["document_id"])
        self.mineru.queries.append({"state": "done", "full_zip_url": "https://results.example/1.zip"})
        self.service.sync(first["parse_run_id"])

        forced = self.service.start_parse(document["document_id"], force=True)
        self.assertNotEqual(first["parse_run_id"], forced["parse_run_id"])
        self.assertEqual(2, len(self.mineru.submissions))

    def test_verify_cos_reports_head_size_and_connectivity_scope(self) -> None:
        document = self.service.ingest(self.write_pdf("invoice.pdf"))
        result = self.service.verify_cos_access(document["document_id"])

        self.assertTrue(result["head_size_matches"])
        self.assertTrue(result["signed_get_connectivity"])
        self.assertEqual("connectivity_only_not_full_file_integrity", result["integrity_scope"])

    def test_unknown_submission_is_not_automatically_retried(self) -> None:
        document = self.service.ingest(self.write_pdf("invoice.pdf"))

        def unknown_submit(_url: str, _config: dict, _data_id: str) -> str:
            raise IngestionError("MINERU_SUBMISSION_UNKNOWN", "timed out")

        self.mineru.submit = unknown_submit  # type: ignore[method-assign]
        with self.assertRaisesRegex(IngestionError, "submission_unknown"):
            self.service.start_parse(document["document_id"])
        with self.repository.connect() as connection:
            row = connection.execute("SELECT status, error_code FROM parse_runs").fetchone()
        self.assertEqual("submission_unknown", row["status"])
        self.assertEqual("MINERU_SUBMISSION_UNKNOWN", row["error_code"])

    def test_done_is_not_success_until_all_artifacts_are_persisted(self) -> None:
        document = self.service.ingest(self.write_pdf("invoice.pdf"))
        run = self.service.start_parse(document["document_id"])
        self.mineru.queries.append({"state": "done", "full_zip_url": "https://results.example/job.zip"})
        result = self.service.sync(run["parse_run_id"])

        self.assertEqual("succeeded", result["status"])
        for key in result["artifacts"].values():
            self.assertIn(key, self.storage.objects)
        self.assertEqual(b"# invoice\n", self.service.read_artifact(run["parse_run_id"], "markdown"))
        self.assertEqual([], json.loads(self.service.read_artifact(run["parse_run_id"], "content-list")))

    def test_corrupt_result_remains_result_pending(self) -> None:
        self.mineru.zip_value = b"not a zip"
        document = self.service.ingest(self.write_pdf("invoice.pdf"))
        run = self.service.start_parse(document["document_id"])
        self.mineru.queries.append({"state": "done", "full_zip_url": "https://results.example/bad.zip"})
        result = self.service.sync(run["parse_run_id"])

        self.assertEqual("result_pending", result["status"])
        self.assertEqual("RESULT_ZIP_INVALID", result["error_code"])

    def test_known_remote_fetch_failure_resigns_once(self) -> None:
        document = self.service.ingest(self.write_pdf("invoice.pdf"))
        run = self.service.start_parse(document["document_id"])
        self.mineru.queries.append({"state": "failed", "err_msg": "download URL expired"})
        retried = self.service.sync(run["parse_run_id"])

        self.assertEqual("submitted", retried["status"])
        self.assertEqual(2, retried["submission_attempts"])
        self.assertEqual(2, self.storage.presigns)
        self.assertEqual(2, len(self.mineru.submissions))


class ZipSafetyTests(unittest.TestCase):
    def test_rejects_path_traversal(self) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("../full.md", "unsafe")
            archive.writestr("content_list.json", "[]")
        with self.assertRaisesRegex(IngestionError, "unsafe"):
            extract_required_artifacts(output.getvalue(), 1024)

    def test_rejects_missing_or_ambiguous_required_artifacts(self) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("full.md", "one")
            archive.writestr("nested/full.md", "two")
            archive.writestr("content_list.json", "[]")
        with self.assertRaisesRegex(IngestionError, "exactly one full.md"):
            extract_required_artifacts(output.getvalue(), 1024)

    def test_rejects_excessive_uncompressed_size(self) -> None:
        value = result_zip(markdown=b"x" * 2048)
        with self.assertRaisesRegex(IngestionError, "expands beyond"):
            extract_required_artifacts(value, 1024)

    def test_rejects_missing_content_list(self) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("full.md", "markdown only")
        with self.assertRaisesRegex(IngestionError, "content_list.json"):
            extract_required_artifacts(output.getvalue(), 1024)

    def test_diagnostics_redact_urls(self) -> None:
        cleaned = safe_message("failed https://cos.example/a?sign=super-secret now")
        self.assertNotIn("super-secret", cleaned)
        self.assertIn("[redacted-url]", cleaned)


class SettingsTests(unittest.TestCase):
    def test_missing_credentials_are_reported_by_name_not_value(self) -> None:
        settings = Settings(cos_bucket="bucket", cos_region="region")
        with self.assertRaisesRegex(IngestionError, "COS_SECRET_ID, COS_SECRET_KEY"):
            settings.require_cos()
        with self.assertRaisesRegex(IngestionError, "MINERU_TOKEN"):
            settings.require_mineru()

    def test_mineru_post_timeout_is_attempted_once(self) -> None:
        settings = Settings(
            cos_bucket="bucket", cos_region="region", mineru_token="test-token"
        )
        client = MinerUClient(settings)
        config = {
            "model_version": "vlm",
            "language": "ch",
            "is_ocr": False,
            "enable_formula": True,
            "enable_table": True,
            "no_cache": False,
        }
        with patch(
            "prototype.document_ingestion.urllib.request.urlopen",
            side_effect=TimeoutError("response timeout"),
        ) as opener:
            with self.assertRaisesRegex(IngestionError, "response timeout") as raised:
                client.submit("https://private.example/invoice.pdf", config, "doc-1")
        self.assertEqual("MINERU_SUBMISSION_UNKNOWN", raised.exception.code)
        self.assertEqual(1, opener.call_count)


if __name__ == "__main__":
    unittest.main()
