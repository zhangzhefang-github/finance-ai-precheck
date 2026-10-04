#!/usr/bin/env python3
"""Persist documents in private COS and parse them with MinerU's precision API."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Protocol


PARSER = "mineru"
ADAPTER_VERSION = "mineru-v4-1"
ACTIVE_STATUSES = ("queued", "submitted", "running", "result_pending")
TERMINAL_STATUSES = ("succeeded", "failed", "submission_unknown")
MIGRATION_PATH = Path(__file__).with_name("migrations") / "001_document_ingestion.sql"
SUBMISSION_UNKNOWN_MIGRATION_PATH = (
    Path(__file__).with_name("migrations") / "002_add_submission_unknown.sql"
)
INVOICE_EXTRACTION_MIGRATION_PATH = (
    Path(__file__).with_name("migrations") / "003_invoice_extraction.sql"
)
PRECHECK_MIGRATION_PATH = (
    Path(__file__).with_name("migrations") / "004_precheck_runs.sql"
)
PRECHECK_GATEWAY_MIGRATION_PATH = (
    Path(__file__).with_name("migrations") / "005_add_precheck_model_gateway.sql"
)
REVIEW_FEEDBACK_MIGRATION_PATH = (
    Path(__file__).with_name("migrations") / "006_review_feedback.sql"
)
URL_PATTERN = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
PAGE_RANGES_PATTERN = re.compile(r"^[0-9,-]+$")


class IngestionError(RuntimeError):
    """A safe, user-facing ingestion failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_message(value: object, limit: int = 500) -> str:
    """Remove URLs (including query signatures) and bound persisted diagnostics."""

    cleaned = URL_PATTERN.sub("[redacted-url]", str(value)).replace("\x00", "")
    return cleaned[:limit]


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise IngestionError("CONFIG_INVALID", f"{name} must be an integer") from exc
    if value < minimum:
        raise IngestionError("CONFIG_INVALID", f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True, repr=False)
class Settings:
    cos_bucket: str
    cos_region: str
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_session_token: str = ""
    mineru_token: str = ""
    mineru_base_url: str = "https://mineru.net"
    max_document_bytes: int = 200 * 1024 * 1024
    max_zip_bytes: int = 500 * 1024 * 1024
    max_zip_uncompressed_bytes: int = 1024 * 1024 * 1024
    signed_url_ttl_seconds: int = 7200
    http_timeout_seconds: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            cos_bucket=os.environ.get("COS_BUCKET", ""),
            cos_region=os.environ.get("COS_REGION", ""),
            cos_secret_id=os.environ.get("COS_SECRET_ID", ""),
            cos_secret_key=os.environ.get("COS_SECRET_KEY", ""),
            cos_session_token=os.environ.get("COS_SESSION_TOKEN", ""),
            mineru_token=os.environ.get("MINERU_TOKEN", ""),
            mineru_base_url=os.environ.get("MINERU_BASE_URL", "https://mineru.net").rstrip("/"),
            max_document_bytes=env_int("DOCUMENT_MAX_BYTES", 200 * 1024 * 1024),
            max_zip_bytes=env_int("MINERU_ZIP_MAX_BYTES", 500 * 1024 * 1024),
            max_zip_uncompressed_bytes=env_int(
                "MINERU_ZIP_MAX_UNCOMPRESSED_BYTES", 1024 * 1024 * 1024
            ),
            signed_url_ttl_seconds=env_int("COS_SIGNED_URL_TTL_SECONDS", 7200, 300),
            http_timeout_seconds=env_int("HTTP_TIMEOUT_SECONDS", 30),
        )

    def require_cos(self) -> None:
        missing = [
            name
            for name, value in (
                ("COS_SECRET_ID", self.cos_secret_id),
                ("COS_SECRET_KEY", self.cos_secret_key),
                ("COS_BUCKET", self.cos_bucket),
                ("COS_REGION", self.cos_region),
            )
            if not value
        ]
        if missing:
            raise IngestionError("CONFIG_MISSING", f"missing configuration: {', '.join(missing)}")

    def require_mineru(self) -> None:
        if not self.mineru_token:
            raise IngestionError("CONFIG_MISSING", "missing configuration: MINERU_TOKEN")


@dataclass(frozen=True)
class FileMetadata:
    filename: str
    extension: str
    media_type: str
    size_bytes: int
    sha256: str


MAGIC_TYPES = {
    ".pdf": ("application/pdf", lambda head: head.startswith(b"%PDF-")),
    ".png": ("image/png", lambda head: head.startswith(b"\x89PNG\r\n\x1a\n")),
    ".jpg": ("image/jpeg", lambda head: head.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image/jpeg", lambda head: head.startswith(b"\xff\xd8\xff")),
}


def inspect_file(path: str | Path, max_bytes: int) -> FileMetadata:
    source = Path(path)
    extension = source.suffix.lower()
    if extension not in MAGIC_TYPES:
        raise IngestionError("FILE_TYPE_UNSUPPORTED", "only PDF, PNG, JPG, and JPEG are supported")
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise IngestionError("FILE_UNREADABLE", safe_message(exc)) from exc
    if size <= 0:
        raise IngestionError("FILE_EMPTY", "the input file is empty")
    if size > max_bytes:
        raise IngestionError("FILE_TOO_LARGE", f"file exceeds the configured {max_bytes}-byte limit")

    digest = hashlib.sha256()
    head = b""
    try:
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if not head:
                    head = chunk[:16]
                digest.update(chunk)
    except OSError as exc:
        raise IngestionError("FILE_UNREADABLE", safe_message(exc)) from exc

    media_type, matches = MAGIC_TYPES[extension]
    if not matches(head):
        raise IngestionError(
            "FILE_CONTENT_MISMATCH", f"file content does not match the {extension} extension"
        )
    return FileMetadata(source.name, extension, media_type, size, digest.hexdigest())


class Repository:
    """Small SQLite repository; each operation owns its connection and transaction."""

    def __init__(self, db_path: str | Path, *, migrate: bool = True):
        self.db_path = Path(db_path)
        if migrate:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        script = MIGRATION_PATH.read_text(encoding="utf-8")
        with self.connect() as connection:
            connection.executescript(script)
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'parse_runs'"
            ).fetchone()[0]
            if "submission_unknown" not in table_sql:
                connection.executescript(
                    SUBMISSION_UNKNOWN_MIGRATION_PATH.read_text(encoding="utf-8")
                )
            connection.executescript(
                INVOICE_EXTRACTION_MIGRATION_PATH.read_text(encoding="utf-8")
            )
            connection.executescript(PRECHECK_MIGRATION_PATH.read_text(encoding="utf-8"))
            precheck_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(precheck_runs)").fetchall()
            }
            if "model_gateway" not in precheck_columns:
                connection.executescript(
                    PRECHECK_GATEWAY_MIGRATION_PATH.read_text(encoding="utf-8")
                )
            connection.executescript(
                REVIEW_FEEDBACK_MIGRATION_PATH.read_text(encoding="utf-8")
            )

    @staticmethod
    def row(value: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(value) if value is not None else None

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self.row(connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone())

    def get_document_by_sha(self, sha256: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self.row(connection.execute("SELECT * FROM documents WHERE sha256 = ?", (sha256,)).fetchone())

    def create_or_get_document(self, meta: FileMetadata, object_key: str) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        document_id = uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM documents WHERE sha256 = ?", (meta.sha256,)).fetchone()
            if existing is not None:
                connection.commit()
                return dict(existing), False
            connection.execute(
                """
                INSERT INTO documents (
                    id, sha256, original_filename, media_type, size_bytes, cos_object_key,
                    storage_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'uploading', ?, ?)
                """,
                (document_id, meta.sha256, meta.filename, meta.media_type, meta.size_bytes, object_key, now, now),
            )
            created = connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
            connection.commit()
            return dict(created), True

    def update_document_storage(
        self, document_id: str, status: str, error_code: str | None = None, error_message: str | None = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE documents SET storage_status = ?, error_code = ?, error_message = ?, updated_at = ?
                   WHERE id = ?""",
                (status, error_code, safe_message(error_message) if error_message else None, utc_now(), document_id),
            )

    def get_parse_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self.row(connection.execute("SELECT * FROM parse_runs WHERE id = ?", (run_id,)).fetchone())

    def find_reusable_run(self, document_id: str, fingerprint: str, include_success: bool = True) -> dict[str, Any] | None:
        statuses = (*ACTIVE_STATUSES, "succeeded") if include_success else ACTIVE_STATUSES
        placeholders = ",".join("?" for _ in statuses)
        sql = f"""
            SELECT * FROM parse_runs
            WHERE document_id = ? AND parser = ? AND config_fingerprint = ?
              AND status IN ({placeholders})
            ORDER BY CASE WHEN status = 'succeeded' THEN 0 ELSE 1 END, created_at DESC
            LIMIT 1
        """
        with self.connect() as connection:
            return self.row(connection.execute(sql, (document_id, PARSER, fingerprint, *statuses)).fetchone())

    def create_parse_run(self, document_id: str, config_json: str, fingerprint: str) -> tuple[dict[str, Any], bool]:
        run_id = uuid.uuid4().hex
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """SELECT * FROM parse_runs WHERE document_id = ? AND parser = ?
                   AND config_fingerprint = ? AND status IN ('queued','submitted','running','result_pending')
                   ORDER BY created_at DESC LIMIT 1""",
                (document_id, PARSER, fingerprint),
            ).fetchone()
            if active is not None:
                connection.commit()
                return dict(active), False
            attempt = connection.execute(
                """SELECT COALESCE(MAX(attempt), 0) + 1 FROM parse_runs
                   WHERE document_id = ? AND parser = ? AND config_fingerprint = ?""",
                (document_id, PARSER, fingerprint),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO parse_runs (
                       id, document_id, parser, config_json, config_fingerprint, status,
                       attempt, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                (run_id, document_id, PARSER, config_json, fingerprint, attempt, now, now),
            )
            result = connection.execute("SELECT * FROM parse_runs WHERE id = ?", (run_id,)).fetchone()
            connection.commit()
            return dict(result), True

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "task_id", "status", "result_zip_key", "markdown_key", "content_list_key",
            "error_code", "error_message", "submission_attempts", "completed_at",
        }
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("unsupported parse run update")
        if "error_message" in fields and fields["error_message"] is not None:
            fields["error_message"] = safe_message(fields["error_message"])
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE parse_runs SET {assignments} WHERE id = ?",
                (*fields.values(), run_id),
            )
            row = connection.execute("SELECT * FROM parse_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise IngestionError("PARSE_RUN_NOT_FOUND", "parse run not found")
        return dict(row)


class ObjectStorage(Protocol):
    def put_file(self, key: str, path: Path, media_type: str) -> None: ...

    def put_bytes(self, key: str, value: bytes, media_type: str) -> None: ...

    def exists(self, key: str) -> bool: ...

    def size_bytes(self, key: str) -> int | None: ...

    def presign_get(self, key: str, expires_seconds: int) -> str: ...

    def get_bytes(self, key: str, max_bytes: int) -> bytes: ...


class CosObjectStorage:
    """Thin adapter over Tencent Cloud's official COS XML Python SDK."""

    def __init__(self, settings: Settings):
        settings.require_cos()
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as exc:
            raise IngestionError(
                "DEPENDENCY_MISSING", "install dependencies with: python -m pip install -r requirements.txt"
            ) from exc
        config = CosConfig(
            Region=settings.cos_region,
            SecretId=settings.cos_secret_id,
            SecretKey=settings.cos_secret_key,
            Token=settings.cos_session_token or None,
            Scheme="https",
        )
        self.client = CosS3Client(config)
        self.bucket = settings.cos_bucket

    def put_file(self, key: str, path: Path, media_type: str) -> None:
        with path.open("rb") as handle:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=handle, ContentType=media_type)

    def put_bytes(self, key: str, value: bytes, media_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=io.BytesIO(value), ContentType=media_type
        )

    def exists(self, key: str) -> bool:
        return self.size_bytes(key) is not None

    def size_bytes(self, key: str) -> int | None:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
            return int(response["Content-Length"])
        except Exception as exc:  # SDK exceptions are optional until runtime.
            status_getter = getattr(exc, "get_status_code", None)
            status = status_getter() if callable(status_getter) else None
            if status == 404:
                return None
            raise IngestionError("COS_HEAD_FAILED", safe_message(exc)) from exc

    def presign_get(self, key: str, expires_seconds: int) -> str:
        try:
            return self.client.get_presigned_download_url(
                Bucket=self.bucket, Key=key, Expired=expires_seconds
            )
        except Exception as exc:
            raise IngestionError("COS_PRESIGN_FAILED", safe_message(exc)) from exc

    def get_bytes(self, key: str, max_bytes: int) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"].get_raw_stream()
            try:
                value = body.read(max_bytes + 1)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
            if len(value) > max_bytes:
                raise IngestionError("ARTIFACT_TOO_LARGE", "stored artifact exceeds read limit")
            return value
        except IngestionError:
            raise
        except Exception as exc:
            raise IngestionError("COS_READ_FAILED", safe_message(exc)) from exc


def verify_signed_get_url(url: str, timeout_seconds: int) -> int:
    """Perform a small GET using the exact URL MinerU will receive."""

    if not url.lower().startswith("https://"):
        raise IngestionError("SIGNED_URL_INVALID", "COS signed URL must use HTTPS")
    request = urllib.request.Request(url, headers={"Range": "bytes=0-0"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if not response.geturl().lower().startswith("https://"):
                raise IngestionError("SIGNED_URL_INVALID", "COS signed URL redirected away from HTTPS")
            return len(response.read(1))
    except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise IngestionError("SIGNED_URL_UNREACHABLE", safe_message(exc)) from exc


class MinerUClient:
    """MinerU v4 precision API adapter with bounded HTTP responses and retries."""

    def __init__(self, settings: Settings):
        settings.require_mineru()
        self.token = settings.mineru_token
        self.base_url = settings.mineru_base_url
        if not self.base_url.startswith("https://"):
            raise IngestionError("CONFIG_INVALID", "MINERU_BASE_URL must use HTTPS")
        self.timeout_seconds = settings.http_timeout_seconds

    def _json_request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        last_error: Exception | None = None
        max_attempts = 3 if method == "GET" else 1
        for attempt in range(max_attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    if not response.geturl().lower().startswith("https://"):
                        raise IngestionError("MINERU_HTTP_ERROR", "MinerU API redirected away from HTTPS")
                    raw = response.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise IngestionError("MINERU_RESPONSE_TOO_LARGE", "MinerU JSON response is too large")
                    value = json.loads(raw.decode("utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError("response is not a JSON object")
                    if value.get("code") != 0:
                        raise IngestionError(
                            "MINERU_BUSINESS_ERROR", safe_message(value.get("msg", "unknown MinerU error"))
                        )
                    data = value.get("data")
                    if not isinstance(data, dict):
                        raise IngestionError("MINERU_RESPONSE_INVALID", "MinerU response has no data object")
                    return data
            except IngestionError:
                raise
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504} or attempt == max_attempts - 1:
                    break
            except (OSError, urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                if attempt == max_attempts - 1:
                    break
            time.sleep(2**attempt)
        error_code = "MINERU_SUBMISSION_UNKNOWN" if method == "POST" else "MINERU_HTTP_ERROR"
        raise IngestionError(error_code, safe_message(last_error or "request failed"))

    def submit(self, signed_url: str, config: dict[str, Any], data_id: str) -> str:
        payload = {
            "url": signed_url,
            "model_version": config["model_version"],
            "language": config["language"],
            "is_ocr": config["is_ocr"],
            "enable_formula": config["enable_formula"],
            "enable_table": config["enable_table"],
            "no_cache": config["no_cache"],
            "data_id": data_id,
        }
        if config.get("page_ranges"):
            payload["page_ranges"] = config["page_ranges"]
        data = self._json_request("POST", "/api/v4/extract/task", payload)
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise IngestionError("MINERU_RESPONSE_INVALID", "MinerU response has no task_id")
        return task_id

    def query(self, task_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", task_id):
            raise IngestionError("TASK_ID_INVALID", "persisted MinerU task_id is invalid")
        return self._json_request("GET", f"/api/v4/extract/task/{task_id}")

    def download_zip(self, url: str, max_bytes: int) -> bytes:
        if not isinstance(url, str) or not url.lower().startswith("https://"):
            raise IngestionError("MINERU_RESULT_URL_INVALID", "MinerU result URL must use HTTPS")
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if not response.geturl().lower().startswith("https://"):
                    raise IngestionError("MINERU_RESULT_URL_INVALID", "result URL redirected away from HTTPS")
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    raise IngestionError("RESULT_ZIP_TOO_LARGE", "MinerU result ZIP exceeds limit")
                value = response.read(max_bytes + 1)
        except IngestionError:
            raise
        except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise IngestionError("RESULT_DOWNLOAD_FAILED", safe_message(exc)) from exc
        if len(value) > max_bytes:
            raise IngestionError("RESULT_ZIP_TOO_LARGE", "MinerU result ZIP exceeds limit")
        return value


def normalized_parse_config(
    *,
    is_ocr: bool,
    model_version: str = "vlm",
    language: str = "ch",
    enable_formula: bool = True,
    enable_table: bool = True,
    page_ranges: str | None = None,
    no_cache: bool = False,
) -> tuple[dict[str, Any], str, str]:
    if model_version not in {"pipeline", "vlm"}:
        raise IngestionError("PARSE_CONFIG_INVALID", "model_version must be pipeline or vlm")
    if not isinstance(language, str) or not language or len(language) > 32:
        raise IngestionError("PARSE_CONFIG_INVALID", "language is invalid")
    if page_ranges is not None and (
        len(page_ranges) > 128 or not PAGE_RANGES_PATTERN.fullmatch(page_ranges)
    ):
        raise IngestionError("PARSE_CONFIG_INVALID", "page_ranges has an invalid format")
    config = {
        "adapter_version": ADAPTER_VERSION,
        "enable_formula": bool(enable_formula),
        "enable_table": bool(enable_table),
        "is_ocr": bool(is_ocr),
        "language": language,
        "model_version": model_version,
        "no_cache": bool(no_cache),
        "page_ranges": page_ranges,
    }
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return config, canonical, fingerprint


def extract_required_artifacts(zip_bytes: bytes, max_uncompressed_bytes: int) -> tuple[bytes, bytes]:
    """Validate the whole archive and read only Markdown and content-list artifacts."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except (zipfile.BadZipFile, OSError) as exc:
        raise IngestionError("RESULT_ZIP_INVALID", "MinerU result is not a valid ZIP") from exc

    with archive:
        members = archive.infolist()
        if not members or len(members) > 10_000:
            raise IngestionError("RESULT_ZIP_INVALID", "ZIP member count is invalid")
        total_size = 0
        markdown: list[zipfile.ZipInfo] = []
        content_lists: list[zipfile.ZipInfo] = []
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            mode = member.external_attr >> 16
            if (
                not name
                or "\\" in name
                or path.is_absolute()
                or ".." in path.parts
                or (mode & 0o170000) == 0o120000
                or member.flag_bits & 0x1
            ):
                raise IngestionError("RESULT_ZIP_UNSAFE", "ZIP contains an unsafe member")
            if member.is_dir():
                continue
            total_size += member.file_size
            if member.file_size > max_uncompressed_bytes or total_size > max_uncompressed_bytes:
                raise IngestionError("RESULT_ZIP_TOO_LARGE", "ZIP expands beyond the configured limit")
            basename = path.name.lower()
            if basename == "full.md":
                markdown.append(member)
            if basename == "content_list.json" or basename.endswith("_content_list.json"):
                content_lists.append(member)

        if len(markdown) != 1:
            raise IngestionError("RESULT_MARKDOWN_MISSING", "ZIP must contain exactly one full.md")
        if len(content_lists) != 1:
            raise IngestionError(
                "RESULT_CONTENT_LIST_MISSING", "ZIP must contain exactly one content_list.json artifact"
            )
        try:
            markdown_bytes = archive.read(markdown[0])
            content_bytes = archive.read(content_lists[0])
            markdown_bytes.decode("utf-8")
            content_value = json.loads(content_bytes.decode("utf-8"))
        except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            raise IngestionError("RESULT_ARTIFACT_INVALID", "required ZIP artifact is unreadable") from exc
        if not isinstance(content_value, (list, dict)):
            raise IngestionError("RESULT_ARTIFACT_INVALID", "content_list.json must contain an array or object")
        return markdown_bytes, content_bytes


class DocumentParsingService:
    def __init__(
        self,
        repository: Repository,
        storage: ObjectStorage,
        mineru: MinerUClient,
        settings: Settings,
        url_verifier=verify_signed_get_url,
    ):
        self.repository = repository
        self.storage = storage
        self.mineru = mineru
        self.settings = settings
        self.url_verifier = url_verifier

    @staticmethod
    def document_view(document: dict[str, Any], reused: bool) -> dict[str, Any]:
        return {
            "document_id": document["id"],
            "sha256": document["sha256"],
            "original_filename": document["original_filename"],
            "media_type": document["media_type"],
            "size_bytes": document["size_bytes"],
            "storage_status": document["storage_status"],
            "raw_object_key": document["cos_object_key"],
            "reused": reused,
        }

    @staticmethod
    def run_view(run: dict[str, Any], reused: bool = False) -> dict[str, Any]:
        result = {
            "document_id": run["document_id"],
            "parse_run_id": run["id"],
            "parser": run["parser"],
            "config_fingerprint": run["config_fingerprint"],
            "status": run["status"],
            "attempt": run["attempt"],
            "submission_attempts": run["submission_attempts"],
            "reused": reused,
            "error_code": run["error_code"],
            "error_message": run["error_message"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "completed_at": run["completed_at"],
        }
        if run["status"] == "succeeded":
            result["artifacts"] = {
                "result_zip_key": run["result_zip_key"],
                "markdown_key": run["markdown_key"],
                "content_list_key": run["content_list_key"],
            }
        return result

    def ingest(self, path: str | Path) -> dict[str, Any]:
        source = Path(path)
        metadata = inspect_file(source, self.settings.max_document_bytes)
        object_key = f"raw/{metadata.sha256[:2]}/{metadata.sha256}{metadata.extension}"
        document, created = self.repository.create_or_get_document(metadata, object_key)
        if not created:
            stored_size = self.storage.size_bytes(document["cos_object_key"])
            if document["storage_status"] == "ready" and stored_size == document["size_bytes"]:
                return self.document_view(document, reused=True)

        self.repository.update_document_storage(document["id"], "uploading")
        try:
            self.storage.put_file(document["cos_object_key"], source, metadata.media_type)
            stored_size = self.storage.size_bytes(document["cos_object_key"])
            if stored_size is None:
                raise IngestionError("COS_UPLOAD_UNVERIFIED", "uploaded object was not found by COS HEAD")
            if stored_size != metadata.size_bytes:
                raise IngestionError(
                    "COS_UPLOAD_SIZE_MISMATCH",
                    f"COS HEAD size {stored_size} does not match local size {metadata.size_bytes}",
                )
        except Exception as exc:
            error = exc if isinstance(exc, IngestionError) else IngestionError("COS_UPLOAD_FAILED", safe_message(exc))
            self.repository.update_document_storage(document["id"], "failed", error.code, str(error))
            raise error
        self.repository.update_document_storage(document["id"], "ready")
        ready = self.repository.get_document(document["id"])
        assert ready is not None
        return self.document_view(ready, reused=False)

    def verify_cos_access(self, document_id: str) -> dict[str, Any]:
        """Verify object metadata and signed-GET connectivity without claiming full integrity."""

        document = self.repository.get_document(document_id)
        if document is None:
            raise IngestionError("DOCUMENT_NOT_FOUND", "document not found")
        stored_size = self.storage.size_bytes(document["cos_object_key"])
        if stored_size is None:
            raise IngestionError("COS_OBJECT_NOT_FOUND", "document original was not found by COS HEAD")
        size_matches = stored_size == document["size_bytes"]
        if not size_matches:
            raise IngestionError(
                "COS_OBJECT_SIZE_MISMATCH",
                f"COS HEAD size {stored_size} does not match recorded size {document['size_bytes']}",
            )
        signed_url = self.storage.presign_get(
            document["cos_object_key"], self.settings.signed_url_ttl_seconds
        )
        bytes_read = self.url_verifier(signed_url, self.settings.http_timeout_seconds)
        if bytes_read < 1:
            raise IngestionError("SIGNED_URL_EMPTY", "signed GET returned no content")
        return {
            "document_id": document["id"],
            "raw_object_key": document["cos_object_key"],
            "head_object_found": True,
            "head_size_bytes": stored_size,
            "recorded_size_bytes": document["size_bytes"],
            "head_size_matches": True,
            "signed_get_connectivity": True,
            "signed_get_bytes_read": bytes_read,
            "integrity_scope": "connectivity_only_not_full_file_integrity",
        }

    def _submit_existing(self, run: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
        config = json.loads(run["config_json"])
        signed_url = self.storage.presign_get(
            document["cos_object_key"], self.settings.signed_url_ttl_seconds
        )
        self.url_verifier(signed_url, self.settings.http_timeout_seconds)
        task_id = self.mineru.submit(signed_url, config, f"doc-{document['id']}-run-{run['id']}")
        return self.repository.update_run(
            run["id"],
            task_id=task_id,
            status="submitted",
            submission_attempts=run["submission_attempts"] + 1,
            error_code=None,
            error_message=None,
        )

    def start_parse(
        self,
        document_id: str,
        *,
        force: bool = False,
        is_ocr: bool | None = None,
        model_version: str = "vlm",
        language: str = "ch",
        enable_formula: bool = True,
        enable_table: bool = True,
        page_ranges: str | None = None,
        no_cache: bool = False,
    ) -> dict[str, Any]:
        document = self.repository.get_document(document_id)
        if document is None:
            raise IngestionError("DOCUMENT_NOT_FOUND", "document not found")
        if (
            document["storage_status"] != "ready"
            or self.storage.size_bytes(document["cos_object_key"]) != document["size_bytes"]
        ):
            raise IngestionError("DOCUMENT_NOT_READY", "document original is not ready in COS")
        resolved_ocr = document["media_type"].startswith("image/") if is_ocr is None else is_ocr
        _config, canonical, fingerprint = normalized_parse_config(
            is_ocr=resolved_ocr,
            model_version=model_version,
            language=language,
            enable_formula=enable_formula,
            enable_table=enable_table,
            page_ranges=page_ranges,
            no_cache=no_cache,
        )
        reusable = self.repository.find_reusable_run(
            document_id, fingerprint, include_success=not force
        )
        if reusable is not None:
            return self.run_view(reusable, reused=True)
        run, created = self.repository.create_parse_run(document_id, canonical, fingerprint)
        if not created:
            return self.run_view(run, reused=True)
        try:
            submitted = self._submit_existing(run, document)
        except Exception as exc:
            error = exc if isinstance(exc, IngestionError) else IngestionError("SUBMIT_FAILED", safe_message(exc))
            status = "submission_unknown" if error.code == "MINERU_SUBMISSION_UNKNOWN" else "failed"
            failed = self.repository.update_run(
                run["id"], status=status, error_code=error.code,
                error_message=str(error), completed_at=utc_now() if status == "failed" else None
            )
            raise IngestionError(
                error.code, f"parse run {failed['id']} submission status is {status}: {error}"
            ) from exc
        return self.run_view(submitted)

    @staticmethod
    def _is_fetch_failure(message: str) -> bool:
        lowered = message.lower()
        return any(term in lowered for term in ("fetch", "download", "url", "下载", "链接", "获取文件"))

    def _persist_result(self, run: dict[str, Any], full_zip_url: str) -> dict[str, Any]:
        zip_bytes = self.mineru.download_zip(full_zip_url, self.settings.max_zip_bytes)
        markdown, content_list = extract_required_artifacts(
            zip_bytes, self.settings.max_zip_uncompressed_bytes
        )
        prefix = f"parsed/{run['id']}"
        keys = {
            "result_zip_key": f"{prefix}/result.zip",
            "markdown_key": f"{prefix}/full.md",
            "content_list_key": f"{prefix}/content_list.json",
        }
        self.storage.put_bytes(keys["result_zip_key"], zip_bytes, "application/zip")
        self.storage.put_bytes(keys["markdown_key"], markdown, "text/markdown; charset=utf-8")
        self.storage.put_bytes(keys["content_list_key"], content_list, "application/json")
        expected_sizes = {
            keys["result_zip_key"]: len(zip_bytes),
            keys["markdown_key"]: len(markdown),
            keys["content_list_key"]: len(content_list),
        }
        if any(self.storage.size_bytes(key) != size for key, size in expected_sizes.items()):
            raise IngestionError(
                "RESULT_PERSIST_UNVERIFIED", "one or more result artifact HEAD sizes do not match"
            )
        return self.repository.update_run(
            run["id"], status="succeeded", error_code=None, error_message=None,
            completed_at=utc_now(), **keys
        )

    def sync(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get_parse_run(run_id)
        if run is None:
            raise IngestionError("PARSE_RUN_NOT_FOUND", "parse run not found")
        if run["status"] in TERMINAL_STATUSES:
            return self.run_view(run, reused=True)
        if not run["task_id"]:
            raise IngestionError("PARSE_RUN_NOT_SUBMITTED", "parse run has no persisted MinerU task_id")
        remote = self.mineru.query(run["task_id"])
        state = remote.get("state")
        if state == "failed":
            message = safe_message(remote.get("err_msg", "MinerU task failed"))
            if run["submission_attempts"] < 2 and self._is_fetch_failure(message):
                document = self.repository.get_document(run["document_id"])
                assert document is not None
                try:
                    return self.run_view(self._submit_existing(run, document))
                except Exception as exc:
                    retry_error = exc if isinstance(exc, IngestionError) else IngestionError(
                        "FETCH_RETRY_FAILED", safe_message(exc)
                    )
                    failed = self.repository.update_run(
                        run_id,
                        status="failed",
                        error_code=retry_error.code,
                        error_message=str(retry_error),
                        completed_at=utc_now(),
                    )
                    return self.run_view(failed)
            failed = self.repository.update_run(
                run_id, status="failed", error_code="MINERU_TASK_FAILED",
                error_message=message, completed_at=utc_now()
            )
            return self.run_view(failed)
        if state in {"pending"}:
            return self.run_view(self.repository.update_run(run_id, status="submitted"))
        if state in {"running", "converting"}:
            return self.run_view(self.repository.update_run(run_id, status="running"))
        if state != "done":
            raise IngestionError("MINERU_STATE_UNKNOWN", f"unknown MinerU task state: {safe_message(state)}")
        full_zip_url = remote.get("full_zip_url")
        if not isinstance(full_zip_url, str) or not full_zip_url:
            pending = self.repository.update_run(
                run_id, status="result_pending", error_code="RESULT_URL_MISSING",
                error_message="MinerU done response has no full_zip_url"
            )
            return self.run_view(pending)
        pending = self.repository.update_run(
            run_id, status="result_pending", error_code=None, error_message=None
        )
        try:
            succeeded = self._persist_result(pending, full_zip_url)
        except Exception as exc:
            error = exc if isinstance(exc, IngestionError) else IngestionError("RESULT_PERSIST_FAILED", safe_message(exc))
            pending = self.repository.update_run(
                run_id, status="result_pending", error_code=error.code, error_message=str(error)
            )
            return self.run_view(pending)
        return self.run_view(succeeded)

    def wait(self, run_id: str, timeout_seconds: int, initial_interval: float = 2, max_interval: float = 20) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        interval = initial_interval
        while True:
            result = self.sync(run_id)
            if result["status"] in TERMINAL_STATUSES:
                return result
            if time.monotonic() >= deadline:
                raise IngestionError("POLL_TIMEOUT", "overall polling timeout reached; rerun sync or wait")
            time.sleep(interval)
            interval = min(interval * 1.6, max_interval)

    def read_artifact(self, run_id: str, artifact: str) -> bytes:
        run = self.repository.get_parse_run(run_id)
        if run is None:
            raise IngestionError("PARSE_RUN_NOT_FOUND", "parse run not found")
        if run["status"] != "succeeded":
            raise IngestionError("RESULT_NOT_READY", "parse result is not ready")
        field, limit = {
            "markdown": ("markdown_key", self.settings.max_zip_uncompressed_bytes),
            "content-list": ("content_list_key", self.settings.max_zip_uncompressed_bytes),
        }[artifact]
        return self.storage.get_bytes(run[field], limit)


def add_parse_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", choices=("pipeline", "vlm"), default="vlm")
    parser.add_argument("--language", default="ch")
    parser.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--formula", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--table", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--page-ranges")
    parser.add_argument("--mineru-no-cache", action="store_true")
    parser.add_argument("--force", action="store_true", help="create a new run unless the same config is active")


def parse_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "force": args.force,
        "is_ocr": args.ocr,
        "model_version": args.model,
        "language": args.language,
        "enable_formula": args.formula,
        "enable_table": args.table,
        "page_ranges": args.page_ranges,
        "no_cache": args.mineru_no_cache,
    }


def make_service(repository: Repository, settings: Settings, need_mineru: bool = True) -> DocumentParsingService:
    storage = CosObjectStorage(settings)
    mineru = MinerUClient(settings) if need_mineru else None
    return DocumentParsingService(repository, storage, mineru, settings)  # type: ignore[arg-type]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("DOCUMENT_DB_PATH", "var/document_ingestion.sqlite3"),
        help="SQLite metadata database path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="validate, deduplicate, and upload one original")
    ingest.add_argument("--file", required=True)

    cos_check = subparsers.add_parser(
        "verify-cos", help="verify COS HEAD size and signed-GET connectivity without MinerU"
    )
    cos_check.add_argument("--document-id", required=True)

    start = subparsers.add_parser("parse", help="start or reuse a MinerU parse run")
    start.add_argument("--document-id", required=True)
    add_parse_options(start)

    sync = subparsers.add_parser("sync", help="query MinerU once and persist a completed result")
    sync.add_argument("--parse-run-id", required=True)

    wait = subparsers.add_parser("wait", help="poll a persisted parse run to a terminal state")
    wait.add_argument("--parse-run-id", required=True)
    wait.add_argument("--timeout", type=int, default=1800)

    status = subparsers.add_parser("status", help="read local parse status without external calls")
    status.add_argument("--parse-run-id", required=True)

    result = subparsers.add_parser("result", help="read a successful artifact from private COS")
    result.add_argument("--parse-run-id", required=True)
    result.add_argument("--artifact", choices=("markdown", "content-list"), required=True)

    combined = subparsers.add_parser("ingest-and-parse", help="ingest a file and start parsing it")
    combined.add_argument("--file", required=True)
    combined.add_argument("--wait", action="store_true")
    combined.add_argument("--timeout", type=int, default=1800)
    add_parse_options(combined)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repository = Repository(args.db)
        settings = Settings.from_env()
        if args.command == "status":
            run = repository.get_parse_run(args.parse_run_id)
            if run is None:
                raise IngestionError("PARSE_RUN_NOT_FOUND", "parse run not found")
            output = DocumentParsingService.run_view(run)
        elif args.command == "ingest":
            output = make_service(repository, settings, need_mineru=False).ingest(args.file)
        elif args.command == "verify-cos":
            output = make_service(repository, settings, need_mineru=False).verify_cos_access(
                args.document_id
            )
        elif args.command == "parse":
            output = make_service(repository, settings).start_parse(
                args.document_id, **parse_kwargs(args)
            )
        elif args.command == "sync":
            output = make_service(repository, settings).sync(args.parse_run_id)
        elif args.command == "wait":
            output = make_service(repository, settings).wait(args.parse_run_id, args.timeout)
        elif args.command == "result":
            value = make_service(repository, settings, need_mineru=False).read_artifact(
                args.parse_run_id, args.artifact
            )
            sys.stdout.buffer.write(value)
            if not value.endswith(b"\n"):
                sys.stdout.buffer.write(b"\n")
            return 0
        elif args.command == "ingest-and-parse":
            service = make_service(repository, settings)
            document = service.ingest(args.file)
            run = service.start_parse(document["document_id"], **parse_kwargs(args))
            if args.wait and run["status"] not in TERMINAL_STATUSES:
                run = service.wait(run["parse_run_id"], args.timeout)
            output = {"document": document, "parse_run": run}
        else:  # pragma: no cover - argparse enforces a command
            raise AssertionError(args.command)
    except (IngestionError, OSError, sqlite3.Error) as exc:
        code = exc.code if isinstance(exc, IngestionError) else "INGESTION_ERROR"
        print(json.dumps({"error": code, "message": safe_message(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
