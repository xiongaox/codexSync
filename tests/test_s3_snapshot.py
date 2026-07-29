from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from codexsync.exceptions import ConflictError
from codexsync.models import (
    AppConfig,
    BackupConfig,
    ConflictConfig,
    FiltersConfig,
    IdentityConfig,
    LoggingConfig,
    PathsConfig,
    ProcessDetectionConfig,
    S3Config,
    SafetyConfig,
    StateConfig,
    StorageConfig,
    SyncConfig,
    TargetsConfig,
)
from codexsync.s3_snapshot import S3SnapshotService


class _Paginator:
    def __init__(self, client: "_FakeS3") -> None:
        self._client = client

    def paginate(self, Bucket: str, Prefix: str):  # noqa: N803
        yield {"Contents": [{"Key": key} for bucket, key in self._client.objects if bucket == Bucket and key.startswith(Prefix)]}


class _NotFound(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def upload_file(self, filename: str, bucket: str, key: str, Callback=None):  # noqa: N803
        data = Path(filename).read_bytes()
        self.objects[(bucket, key)] = data
        if Callback:
            Callback(len(data))

    def download_file(self, bucket: str, key: str, filename: str, Callback=None):  # noqa: N803
        data = self.objects[(bucket, key)]
        Path(filename).write_bytes(data)
        if Callback:
            Callback(len(data))

    def head_object(self, Bucket: str, Key: str):  # noqa: N803
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **_kwargs):  # noqa: N803
        self.objects[(Bucket, Key)] = Body.read() if hasattr(Body, "read") else bytes(Body)

    def get_object(self, Bucket: str, Key: str):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise _NotFound()
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    def get_paginator(self, _name: str):
        return _Paginator(self)


def _config(root: Path, machine_id: str) -> tuple[AppConfig, Path]:
    local = root / "local"
    local.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        identity=IdentityConfig(machine_id=machine_id),
        paths=PathsConfig(root, local, root / "cloud", root / "backups", root / "tmp"),
        sync=SyncConfig(),
        safety=SafetyConfig(require_codex_stopped=False),
        process_detection=ProcessDetectionConfig(),
        backup=BackupConfig(compression="zip"),
        filters=FiltersConfig(),
        targets=TargetsConfig(include_roots=["sessions", "session_index.jsonl"]),
        conflict=ConflictConfig(),
        state=StateConfig(s3_metadata_file=root / "state" / "s3.json"),
        logging=LoggingConfig(),
        storage=StorageConfig(backend="s3"),
        s3=S3Config(bucket="test-bucket", prefix="codexsync", endpoint_url="https://example.invalid", addressing_style="path"),
    )
    return cfg, local


class S3SnapshotTests(unittest.TestCase):
    def test_push_pull_merge_and_non_fast_forward_guard(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"s3-snapshot-{uuid.uuid4().hex}"
        fake = _FakeS3()
        try:
            cfg_a, local_a = _config(root / "a", "machine-a")
            (local_a / "sessions").mkdir()
            (local_a / "sessions" / "a.json").write_text("a", encoding="utf-8")
            (local_a / "session_index.jsonl").write_text('{"id":"a"}\n', encoding="utf-8")

            cfg_b, local_b = _config(root / "b", "machine-b")
            (local_b / "sessions").mkdir()
            (local_b / "sessions" / "b.json").write_text("b", encoding="utf-8")
            (local_b / "session_index.jsonl").write_text('{"id":"b"}\n', encoding="utf-8")

            with patch("codexsync.s3_snapshot._make_s3_client", return_value=fake):
                pushed = S3SnapshotService(cfg_a, local_a).push()
                with self.assertRaises(ConflictError):
                    S3SnapshotService(cfg_b, local_b).push()

                pulled = S3SnapshotService(cfg_b, local_b).pull(merge=True)
                self.assertEqual(pulled.snapshot_id, pushed.snapshot_id)
                self.assertEqual((local_b / "sessions" / "a.json").read_text(encoding="utf-8"), "a")
                self.assertEqual((local_b / "sessions" / "b.json").read_text(encoding="utf-8"), "b")
                self.assertEqual(
                    (local_b / "session_index.jsonl").read_text(encoding="utf-8"),
                    '{"id":"a"}\n{"id":"b"}\n',
                )

                merged = S3SnapshotService(cfg_b, local_b).push()
                self.assertNotEqual(merged.snapshot_id, pushed.snapshot_id)
                listed = S3SnapshotService(cfg_b, local_b).list_snapshots()
                self.assertEqual([item.snapshot_id for item in listed], sorted([pushed.snapshot_id, merged.snapshot_id], reverse=True))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_push_allows_pre_1980_source_mtime(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"s3-old-mtime-{uuid.uuid4().hex}"
        fake = _FakeS3()
        try:
            cfg, local = _config(root, "machine-a")
            source = local / "sessions" / "old.json"
            source.parent.mkdir()
            source.write_text("old", encoding="utf-8")
            os.utime(source, (1, 1))
            with patch("codexsync.s3_snapshot._make_s3_client", return_value=fake):
                info = S3SnapshotService(cfg, local).push()
            self.assertEqual(info.file_count, 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
