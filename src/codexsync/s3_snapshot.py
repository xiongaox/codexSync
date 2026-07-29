from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backup import BackupManager
from .exceptions import ConfigError, ConflictError, FailSafeError
from .filters import PathFilter
from .models import AppConfig, CopyAction, FileMeta, SyncPlan
from .scanner import scan_tree
from .sync_engine import SyncEngine


@dataclass(slots=True, frozen=True)
class SnapshotInfo:
    snapshot_id: str
    machine_id: str
    created_at: str
    file_count: int
    total_size: int


class S3SnapshotService:
    """Immutable ZIP snapshots stored in an S3-compatible bucket."""

    def __init__(self, cfg: AppConfig, local_root: Path) -> None:
        if cfg.storage.backend != "s3":
            raise ConfigError("S3 commands require storage.backend=s3")
        if not cfg.s3.bucket:
            raise ConfigError("s3.bucket is required")
        self.cfg = cfg
        self.local_root = local_root
        self.bucket = cfg.s3.bucket
        self.prefix = cfg.s3.prefix.strip("/")
        self.temp_root = cfg.paths.temp_dir
        self.metadata_file = cfg.state.s3_metadata_file or cfg.paths.temp_dir / "s3-metadata.json"
        self.client = _make_s3_client(cfg)

    def push(self, dry_run: bool = False) -> SnapshotInfo:
        latest = self._get_latest()
        baseline = self._get_local_baseline()
        if latest and baseline != latest["snapshot_id"]:
            raise ConflictError(
                "Remote latest snapshot is newer than this machine's baseline. "
                "Run `pull --merge` (or `pull`) before push."
            )

        archive, manifest, info = self._create_snapshot_files()
        if dry_run:
            print(f"Dry-run: would upload snapshot {info.snapshot_id} ({info.file_count} file(s)).")
            archive.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
            return info

        try:
            print(f"Uploading snapshot {info.snapshot_id}...")
            self.client.upload_file(
                str(archive), self.bucket, self._key(f"snapshots/{info.snapshot_id}/archive.zip"),
                Callback=_Progress("Upload", archive.stat().st_size),
            )
            self.client.upload_file(
                str(manifest), self.bucket, self._key(f"snapshots/{info.snapshot_id}/manifest.json"),
                Callback=_Progress("Upload manifest", manifest.stat().st_size),
            )
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key("latest.json"),
                Body=json.dumps({"snapshot_id": info.snapshot_id}, sort_keys=True).encode("utf-8"),
                ContentType="application/json",
            )
            self._set_local_baseline(info.snapshot_id)
            print("Upload complete. Snapshot is now latest.")
            return info
        except Exception as exc:
            raise FailSafeError(f"S3 upload failed; latest snapshot was not safely published: {exc}") from exc
        finally:
            archive.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)

    def pull(self, snapshot_id: str | None = None, merge: bool = False, dry_run: bool = False) -> SnapshotInfo:
        resolved_id = snapshot_id or self._get_latest_id()
        if not resolved_id:
            raise ConfigError("No S3 snapshot is available to pull")
        staging = self.temp_root / f"s3-pull-{uuid.uuid4().hex}"
        archive = staging / "archive.zip"
        manifest_path = staging / "manifest.json"
        extracted = staging / "content"
        try:
            staging.mkdir(parents=True, exist_ok=True)
            print(f"Downloading snapshot {resolved_id}...")
            self._download(resolved_id, "manifest.json", manifest_path, "Download manifest")
            manifest = _load_snapshot_manifest(manifest_path)
            self._download(resolved_id, "archive.zip", archive, "Download")
            if _sha256(archive) != manifest["archive_sha256"]:
                raise FailSafeError("Downloaded archive checksum does not match its manifest")
            _safe_extract(archive, extracted)
            _verify_extracted(extracted, manifest)
            info = _snapshot_info(manifest)
            plan = self._build_pull_plan(extracted, merge)
            if plan.conflicts:
                raise ConflictError("Merge conflicts: " + ", ".join(plan.conflicts))
            if dry_run:
                print(f"Dry-run: would restore {plan.action_count} file(s) from {resolved_id}.")
                return info
            self._execute_local_plan(plan)
            self._set_local_baseline(resolved_id)
            print(f"Pull complete. Restored {plan.action_count} file(s).")
            return info
        except (ConfigError, ConflictError, FailSafeError):
            raise
        except Exception as exc:
            raise FailSafeError(f"S3 pull failed safely: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def list_snapshots(self) -> list[SnapshotInfo]:
        prefix = self._key("snapshots/")
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            ids: set[str] = set()
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    key = item.get("Key", "")
                    if key.endswith("/manifest.json"):
                        ids.add(key.removeprefix(prefix).removesuffix("/manifest.json"))
            result: list[SnapshotInfo] = []
            for snapshot_id in sorted(ids, reverse=True):
                temp = self.temp_root / f"s3-list-{uuid.uuid4().hex}.json"
                try:
                    self._download(snapshot_id, "manifest.json", temp, "Read manifest")
                    result.append(_snapshot_info(_load_snapshot_manifest(temp)))
                finally:
                    temp.unlink(missing_ok=True)
            return result
        except Exception as exc:
            raise FailSafeError(f"Unable to list S3 snapshots: {exc}") from exc

    def check_connection(self) -> None:
        try:
            self.client.list_objects_v2(Bucket=self.bucket, Prefix=self._key("snapshots/"), MaxKeys=1)
        except Exception as exc:
            raise FailSafeError(f"Cannot access S3 bucket/prefix: {exc}") from exc

    def _create_snapshot_files(self) -> tuple[Path, Path, SnapshotInfo]:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        archive = self.temp_root / f"{snapshot_id}.zip"
        manifest_path = self.temp_root / f"{snapshot_id}.manifest.json"
        index = scan_tree(self.local_root, self.cfg.targets.include_roots, PathFilter(self.cfg.filters.exclude_globs))
        files: list[dict[str, Any]] = []
        # Codex state may contain files restored with a pre-1980 mtime. ZIP's
        # on-disk timestamp range starts in 1980; keep the original mtime in
        # the manifest and clamp only the ZIP container metadata.
        with zipfile.ZipFile(
            archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            strict_timestamps=False,
        ) as zf:
            for rel, meta in sorted(index.items()):
                zf.write(meta.abs_path, arcname=rel)
                files.append({"path": rel, "size": meta.size, "mtime_ns": meta.mtime_ns, "sha256": _sha256(meta.abs_path)})
        machine = self.cfg.identity.machine_id or "unknown-machine"
        payload = {
            "data_version": 1,
            "snapshot_id": snapshot_id,
            "machine_id": machine,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "archive_sha256": _sha256(archive),
            "files": files,
        }
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return archive, manifest_path, _snapshot_info(payload)

    def _build_pull_plan(self, extracted: Path, merge: bool) -> SyncPlan:
        remote = scan_tree(extracted, self.cfg.targets.include_roots, PathFilter(self.cfg.filters.exclude_globs))
        local = scan_tree(self.local_root, self.cfg.targets.include_roots, PathFilter(self.cfg.filters.exclude_globs))
        actions: list[CopyAction] = []
        conflicts: list[str] = []
        for rel, remote_meta in sorted(remote.items()):
            local_meta = local.get(rel)
            if not merge or local_meta is None or _sha256(local_meta.abs_path) == _sha256(remote_meta.abs_path):
                actions.append(CopyAction(remote_meta.abs_path, self.local_root / rel, rel))
                continue
            if rel == "session_index.jsonl":
                merged = _merge_jsonl(remote_meta.abs_path, local_meta.abs_path, extracted / ".merged-session-index.jsonl")
                actions.append(CopyAction(merged, self.local_root / rel, rel))
            else:
                conflicts.append(rel)
        return SyncPlan(to_local=actions, conflicts=conflicts)

    def _execute_local_plan(self, plan: SyncPlan) -> None:
        manager = BackupManager(
            self.cfg.paths.backup_dir,
            self.cfg.identity.machine_id,
            self.cfg.backup.retention_days,
            self.cfg.backup.max_backups,
            self.cfg.backup.compression,
        )
        SyncEngine(manager, self.cfg.paths.temp_dir, self.cfg.backup.backup_before_overwrite, self.cfg.safety.fail_on_unknown).execute(plan, dry_run=False)

    def _download(self, snapshot_id: str, filename: str, destination: Path, label: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        head = self.client.head_object(Bucket=self.bucket, Key=self._key(f"snapshots/{snapshot_id}/{filename}"))
        self.client.download_file(
            self.bucket, self._key(f"snapshots/{snapshot_id}/{filename}"), str(destination),
            Callback=_Progress(label, int(head.get("ContentLength", 0))),
        )

    def _get_latest(self) -> dict[str, str] | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key("latest.json"))
            return json.loads(response["Body"].read().decode("utf-8"))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise

    def _get_latest_id(self) -> str | None:
        latest = self._get_latest()
        return latest.get("snapshot_id") if latest else None

    def _get_local_baseline(self) -> str | None:
        if not self.metadata_file.exists():
            return None
        try:
            return json.loads(self.metadata_file.read_text(encoding="utf-8")).get("last_snapshot_id")
        except (OSError, json.JSONDecodeError) as exc:
            raise FailSafeError(f"Cannot read local S3 metadata safely: {exc}") from exc

    def _set_local_baseline(self, snapshot_id: str) -> None:
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.metadata_file.with_suffix(self.metadata_file.suffix + ".tmp")
        tmp.write_text(json.dumps({"last_snapshot_id": snapshot_id}, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.metadata_file)

    def _key(self, suffix: str) -> str:
        return f"{self.prefix}/{suffix}" if self.prefix else suffix


class _Progress:
    def __init__(self, label: str, total: int) -> None:
        self.label, self.total, self.transferred, self.last = label, total, 0, 0.0

    def __call__(self, amount: int) -> None:
        self.transferred += amount
        now = time.monotonic()
        if self.transferred < self.total and now - self.last < 0.2:
            return
        self.last = now
        percent = (self.transferred / self.total * 100) if self.total else 100
        print(f"\r{self.label}: {self.transferred}/{self.total} bytes ({percent:.0f}%)", end="", file=sys.stderr, flush=True)
        if self.transferred >= self.total:
            print(file=sys.stderr)


def _make_s3_client(cfg: AppConfig) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ConfigError("S3 support requires boto3. Install codexsync with its dependencies.") from exc
    style = cfg.s3.addressing_style
    config = Config(s3={"addressing_style": style}) if style != "auto" else Config()
    return boto3.client("s3", region_name=cfg.s3.region, endpoint_url=cfg.s3.endpoint_url, verify=cfg.s3.verify_tls, config=config)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_snapshot_manifest(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    required = {"snapshot_id", "machine_id", "created_at", "archive_sha256", "files"}
    if not required.issubset(raw) or not isinstance(raw["files"], list):
        raise FailSafeError("S3 snapshot manifest is invalid")
    return raw


def _snapshot_info(manifest: dict[str, Any]) -> SnapshotInfo:
    files = manifest["files"]
    return SnapshotInfo(manifest["snapshot_id"], manifest["machine_id"], manifest["created_at"], len(files), sum(int(item["size"]) for item in files))


def _safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for item in zf.infolist():
            candidate = (destination / item.filename).resolve()
            try:
                candidate.relative_to(destination.resolve())
            except ValueError as exc:
                raise FailSafeError(f"Unsafe path in archive: {item.filename}") from exc
        zf.extractall(destination)


def _verify_extracted(root: Path, manifest: dict[str, Any]) -> None:
    for item in manifest["files"]:
        path = root / item["path"]
        if not path.is_file() or path.stat().st_size != int(item["size"]) or _sha256(path) != item["sha256"]:
            raise FailSafeError(f"Snapshot file verification failed: {item['path']}")
        mtime_ns = int(item.get("mtime_ns", 0))
        if mtime_ns:
            path.touch()
            import os
            os.utime(path, ns=(mtime_ns, mtime_ns))


def _merge_jsonl(remote: Path, local: Path, destination: Path) -> Path:
    try:
        remote_lines = remote.read_text(encoding="utf-8").splitlines()
        local_lines = local.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ConflictError("session_index.jsonl is not valid UTF-8 and cannot be merged safely") from exc
    lines = list(dict.fromkeys(remote_lines + local_lines))
    destination.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return destination


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    code = str(response.get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""
    return code in {"NoSuchKey", "404", "NotFound"}
