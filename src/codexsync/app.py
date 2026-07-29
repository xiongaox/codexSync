from __future__ import annotations

import logging
import os
import platform
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import zipfile

from .backup import BackupManager
from .config import load_config
from .exceptions import ConfigError, ConflictError, FailSafeError, SafetyPreconditionError
from .filters import PathFilter
from .gui_prompt import confirm_process_termination
from .manifest import build_manifest, load_manifest, save_manifest
from .models import AppConfig, CopyAction, FileMeta, SyncManifest, SyncPlan
from .planner import build_sync_plan
from .process_detector import CodexProcessDetector, ProcessInfo
from .scanner import scan_tree
from .state_locator import detect_local_state_dir, resolve_state_dirs
from .sync_engine import SyncEngine
from .s3_snapshot import S3SnapshotService, SnapshotInfo

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class AppContext:
    config: AppConfig
    local_dir: Path
    cloud_dir: Path
    plan: SyncPlan
    local_index: dict[str, FileMeta]
    cloud_index: dict[str, FileMeta]


@dataclass(slots=True, frozen=True)
class RestoreResult:
    snapshot_name: str
    target: str
    restored_files: int


@dataclass(slots=True, frozen=True)
class ProcessSnapshot:
    main_processes: list[ProcessInfo]
    subprocesses: list[ProcessInfo]
    sandbox_detected: bool


@dataclass(slots=True, frozen=True)
class PreflightCheckResult:
    name: str
    status: str
    details: str


@dataclass(slots=True)
class PreflightReport:
    checks: list[PreflightCheckResult]

    @property
    def failures(self) -> list[PreflightCheckResult]:
        return [item for item in self.checks if item.status == "FAIL"]

    @property
    def warnings(self) -> list[PreflightCheckResult]:
        return [item for item in self.checks if item.status == "WARN"]

    @property
    def passed(self) -> list[PreflightCheckResult]:
        return [item for item in self.checks if item.status == "PASS"]

    @property
    def is_ok(self) -> bool:
        return not self.failures


def build_context(
    config_path: Path,
    manual_terminate_confirmation_override: bool | None = None,
    enforce_safety: bool = True,
) -> AppContext:
    cfg = load_config(config_path)
    initialize_runtime_paths(cfg)
    local_dir, cloud_dir = resolve_state_dirs(cfg.paths.local_state_dir, cfg.paths.cloud_root_dir)
    if enforce_safety:
        _enforce_safety_preconditions(cfg, manual_terminate_confirmation_override)

    local_idx, cloud_idx = _build_indexes(cfg, local_dir, cloud_dir)
    manifest = load_manifest(cfg.state.manifest_file, cfg.state.data_version)
    plan = build_sync_plan(
        local_index=local_idx,
        cloud_index=cloud_idx,
        local_root=local_dir,
        cloud_root=cloud_dir,
        previous_manifest=manifest,
        compare_mode=cfg.sync.compare,
        tolerance_seconds=cfg.sync.time_tolerance_seconds,
        conflict_policy=cfg.conflict.policy,
        equal_mtime_action=cfg.sync.equal_mtime_action,
    )
    return AppContext(
        config=cfg,
        local_dir=local_dir,
        cloud_dir=cloud_dir,
        plan=plan,
        local_index=local_idx,
        cloud_index=cloud_idx,
    )


def print_plan(plan: SyncPlan) -> None:
    print("Plan:")
    print(f"  to_local: {len(plan.to_local)}")
    print(f"  to_cloud: {len(plan.to_cloud)}")
    print(f"  actions: {plan.action_count}")
    print(f"  conflicts: {len(plan.conflicts)}")
    for rel_path in plan.conflicts:
        print(f"    conflict: {rel_path}")
    for item in plan.to_local:
        print(f"    cloud -> local: {item.relative_path}")
    for item in plan.to_cloud:
        print(f"    local -> cloud: {item.relative_path}")


def run_sync(ctx: AppContext, dry_run: bool) -> None:
    if ctx.plan.conflicts and ctx.config.conflict.policy == "manual_abort":
        details = ", ".join(ctx.plan.conflicts)
        if not ctx.config.conflict.report_conflicts:
            details = "hidden by configuration"
        raise ConflictError(f"Conflict detected for files: {details}. Resolve manually and rerun.")

    mgr = BackupManager(
        backup_root=ctx.config.paths.backup_dir,
        machine_id=ctx.config.identity.machine_id or platform.node(),
        retention_days=ctx.config.backup.retention_days,
        max_backups=ctx.config.backup.max_backups,
        compression=ctx.config.backup.compression,
    )
    engine = SyncEngine(
        backup_manager=mgr,
        temp_dir=ctx.config.paths.temp_dir,
        backup_before_overwrite=ctx.config.backup.backup_before_overwrite,
        fail_on_unknown=ctx.config.safety.fail_on_unknown,
    )
    engine.execute(ctx.plan, dry_run=dry_run)

    if not dry_run:
        local_idx, cloud_idx = _build_indexes(ctx.config, ctx.local_dir, ctx.cloud_dir)
        manifest = build_manifest(local_idx, cloud_idx, ctx.config.state.data_version)
        save_manifest(manifest, ctx.config.state.manifest_file)


def restore_from_backup(
    config_path: Path,
    snapshot_name: str | None,
    target: str,
    dry_run: bool,
    manual_terminate_confirmation_override: bool | None = None,
) -> RestoreResult:
    cfg = load_config(config_path)
    initialize_runtime_paths(cfg)
    _enforce_safety_preconditions(cfg, manual_terminate_confirmation_override)

    target_root = _resolve_restore_target(cfg, target)
    snapshot = _resolve_snapshot_dir(cfg.paths.backup_dir, snapshot_name)
    plan, extracted_snapshot_dir = _build_restore_plan_from_snapshot(
        snapshot=snapshot,
        target_root=target_root,
        include_roots=cfg.targets.include_roots,
        exclude_globs=cfg.filters.exclude_globs,
        temp_root=cfg.paths.temp_dir,
    )

    mgr = BackupManager(
        backup_root=cfg.paths.backup_dir,
        machine_id=cfg.identity.machine_id or platform.node(),
        retention_days=cfg.backup.retention_days,
        max_backups=cfg.backup.max_backups,
        compression=cfg.backup.compression,
    )
    engine = SyncEngine(
        backup_manager=mgr,
        temp_dir=cfg.paths.temp_dir,
        backup_before_overwrite=cfg.backup.backup_before_overwrite,
        fail_on_unknown=cfg.safety.fail_on_unknown,
    )
    try:
        engine.execute(plan, dry_run=dry_run)
    finally:
        if extracted_snapshot_dir is not None:
            shutil.rmtree(extracted_snapshot_dir, ignore_errors=True)

    return RestoreResult(
        snapshot_name=snapshot.name,
        target=target,
        restored_files=plan.action_count,
    )


def validate_config_only(config_path: Path) -> None:
    try:
        cfg = load_config(config_path)
        initialize_runtime_paths(cfg)
        if cfg.storage.backend == "s3":
            _ = detect_local_state_dir(cfg.paths.local_state_dir)
        else:
            if cfg.paths.cloud_root_dir is None:
                raise ConfigError("paths.cloud_root_dir is required for filesystem sync")
            _ = resolve_state_dirs(cfg.paths.local_state_dir, cfg.paths.cloud_root_dir)
    except ConfigError:
        raise


def push_s3_snapshot(
    config_path: Path,
    dry_run: bool,
    manual_terminate_confirmation_override: bool | None = None,
) -> SnapshotInfo:
    cfg = load_config(config_path)
    initialize_runtime_paths(cfg)
    local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
    _enforce_safety_preconditions(cfg, manual_terminate_confirmation_override)
    return S3SnapshotService(cfg, local_dir).push(dry_run=dry_run)


def pull_s3_snapshot(
    config_path: Path,
    snapshot_id: str | None,
    merge: bool,
    dry_run: bool,
    manual_terminate_confirmation_override: bool | None = None,
) -> SnapshotInfo:
    cfg = load_config(config_path)
    initialize_runtime_paths(cfg)
    local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
    _enforce_safety_preconditions(cfg, manual_terminate_confirmation_override)
    return S3SnapshotService(cfg, local_dir).pull(snapshot_id=snapshot_id, merge=merge, dry_run=dry_run)


def list_s3_snapshots(config_path: Path) -> list[SnapshotInfo]:
    cfg = load_config(config_path)
    initialize_runtime_paths(cfg)
    local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
    return S3SnapshotService(cfg, local_dir).list_snapshots()


def run_preflight(config_path: Path) -> PreflightReport:
    checks: list[PreflightCheckResult] = []

    try:
        cfg = load_config(config_path)
        checks.append(PreflightCheckResult("config", "PASS", f"Loaded config from {config_path}"))
    except Exception as exc:
        checks.append(PreflightCheckResult("config", "FAIL", f"Cannot load config: {exc}"))
        return PreflightReport(checks=checks)

    try:
        initialize_runtime_paths(cfg)
        checks.append(PreflightCheckResult("runtime_paths", "PASS", "Runtime paths are initialized"))
    except Exception as exc:
        checks.append(PreflightCheckResult("runtime_paths", "FAIL", f"Runtime path initialization failed: {exc}"))
        return PreflightReport(checks=checks)

    local_dir: Path | None = None
    cloud_dir: Path | None = None
    try:
        if cfg.storage.backend == "s3":
            local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
            checks.append(PreflightCheckResult("state_dirs", "PASS", f"local={local_dir}; storage=s3"))
            try:
                S3SnapshotService(cfg, local_dir).check_connection()
                checks.append(PreflightCheckResult("s3", "PASS", "S3 bucket is reachable"))
            except Exception as exc:
                checks.append(PreflightCheckResult("s3", "FAIL", f"S3 check failed: {exc}"))
        else:
            if cfg.paths.cloud_root_dir is None:
                raise ConfigError("paths.cloud_root_dir is required for filesystem sync")
            local_dir, cloud_dir = resolve_state_dirs(cfg.paths.local_state_dir, cfg.paths.cloud_root_dir)
            checks.append(PreflightCheckResult("state_dirs", "PASS", f"local={local_dir}; cloud={cloud_dir}"))
    except Exception as exc:
        checks.append(PreflightCheckResult("state_dirs", "FAIL", f"State directories are not ready: {exc}"))

    if local_dir is not None:
        checks.append(_check_write_access("local_write", local_dir))
    if cloud_dir is not None:
        checks.append(_check_write_access("cloud_write", cloud_dir))

    checks.append(_check_write_access("backup_write", cfg.paths.backup_dir))
    checks.append(_check_write_access("temp_write", cfg.paths.temp_dir))

    try:
        _ = load_manifest(cfg.state.manifest_file, cfg.state.data_version)
        checks.append(PreflightCheckResult("manifest", "PASS", "Manifest data version is compatible"))
    except Exception as exc:
        checks.append(PreflightCheckResult("manifest", "FAIL", f"Manifest check failed: {exc}"))

    checks.append(_check_process_state(cfg))
    if local_dir is not None and cloud_dir is not None:
        checks.append(_check_clock_drift(local_dir, cloud_dir))
    checks.append(_check_orphan_temp_files(cfg.paths.temp_dir))

    return PreflightReport(checks=checks)


def print_preflight_report(report: PreflightReport) -> None:
    print("Preflight report:")
    for item in report.checks:
        print(f"  [{item.status}] {item.name}: {item.details}")
    print(
        "Summary: "
        f"pass={len(report.passed)} warn={len(report.warnings)} fail={len(report.failures)}"
    )


def initialize_runtime_paths(cfg: AppConfig) -> None:
    """
    Prepares runtime directories/files for first run.
    Creates only codexSync-owned infrastructure and never creates local Codex state dir.
    """
    if cfg.paths.cloud_root_dir is not None:
        _ensure_dir(cfg.paths.cloud_root_dir, "paths.cloud_root_dir")
    _ensure_dir(cfg.paths.backup_dir, "paths.backup_dir")
    _ensure_dir(cfg.paths.temp_dir, "paths.temp_dir")
    if cfg.storage.backend == "filesystem":
        _bootstrap_cloud_targets(cfg)

    if cfg.logging.file:
        _ensure_dir(cfg.logging.file.parent, "logging.file parent")

    if cfg.state.manifest_file:
        _ensure_dir(cfg.state.manifest_file.parent, "state.manifest_file parent")
        if not cfg.state.manifest_file.exists():
            empty_manifest = SyncManifest(data_version=cfg.state.data_version, files={})
            save_manifest(empty_manifest, cfg.state.manifest_file)


def _build_indexes(cfg: AppConfig, local_dir: Path, cloud_dir: Path) -> tuple[dict[str, FileMeta], dict[str, FileMeta]]:
    path_filter = PathFilter(cfg.filters.exclude_globs)
    local_idx = scan_tree(local_dir, cfg.targets.include_roots, path_filter)
    cloud_idx = scan_tree(cloud_dir, cfg.targets.include_roots, path_filter)
    return _apply_session_mode(local_idx, cloud_idx, cfg.sync.session_mode)


def _apply_session_mode(
    local_idx: dict[str, FileMeta],
    cloud_idx: dict[str, FileMeta],
    session_mode: str | None,
) -> tuple[dict[str, FileMeta], dict[str, FileMeta]]:
    mode = (session_mode or "all").strip().lower()
    if mode != "last_date_only":
        return local_idx, cloud_idx

    latest_key = _latest_sessions_date_key(local_idx, cloud_idx)
    if latest_key is None:
        LOG.warning(
            "sync.session_mode=last_date_only is set, but no date-based sessions folders were detected. "
            "Proceeding with all sessions files."
        )
        return local_idx, cloud_idx

    LOG.info("sync.session_mode=last_date_only: only sessions for %s will be included", latest_key)
    return (
        _filter_sessions_by_date_key(local_idx, latest_key),
        _filter_sessions_by_date_key(cloud_idx, latest_key),
    )


def _filter_sessions_by_date_key(index: dict[str, FileMeta], date_key: str) -> dict[str, FileMeta]:
    filtered: dict[str, FileMeta] = {}
    for rel, meta in index.items():
        session_date = _extract_session_date_key(rel)
        if session_date is None:
            if not rel.startswith("sessions/"):
                filtered[rel] = meta
            continue
        if session_date == date_key:
            filtered[rel] = meta
    return filtered


def _latest_sessions_date_key(local_idx: dict[str, FileMeta], cloud_idx: dict[str, FileMeta]) -> str | None:
    all_dates: set[str] = set()
    for rel in set(local_idx.keys()) | set(cloud_idx.keys()):
        date_key = _extract_session_date_key(rel)
        if date_key:
            all_dates.add(date_key)
    if not all_dates:
        return None
    return sorted(all_dates)[-1]


def _extract_session_date_key(rel: str) -> str | None:
    normalized = rel.strip("/\\")
    if not normalized.startswith("sessions/"):
        return None

    parts = normalized.split("/")
    if len(parts) >= 2 and _is_iso_date(parts[1]):
        return parts[1]
    if len(parts) >= 4 and _is_ymd_triplet(parts[1], parts[2], parts[3]):
        return f"{parts[1]}-{parts[2]}-{parts[3]}"
    return None


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_ymd_triplet(year: str, month: str, day: str) -> bool:
    if len(year) != 4 or len(month) != 2 or len(day) != 2:
        return False
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return False
    try:
        date(int(year), int(month), int(day))
    except ValueError:
        return False
    return True


def _enforce_safety_preconditions(
    cfg: AppConfig,
    manual_terminate_confirmation_override: bool | None = None,
) -> None:
    if not cfg.safety.require_codex_stopped:
        return

    detector = CodexProcessDetector(cfg.process_detection.process_names)
    try:
        snapshot = collect_process_snapshot(cfg, detector=detector)
    except Exception as exc:
        if cfg.safety.fail_on_unknown:
            raise FailSafeError(
                f"Cannot verify Codex process status safely: {exc}"
            ) from exc
        LOG.warning("Cannot verify Codex process status. Continue due to fail_on_unknown=false: %s", exc)
        return

    if snapshot.main_processes:
        _handle_running_codex(
            cfg=cfg,
            detector=detector,
            snapshot=snapshot,
            manual_override=manual_terminate_confirmation_override,
        )

    if cfg.process_detection.grace_period_seconds > 0:
        time.sleep(cfg.process_detection.grace_period_seconds)


def _handle_running_codex(
    cfg: AppConfig,
    detector: CodexProcessDetector,
    snapshot: ProcessSnapshot,
    manual_override: bool | None,
) -> None:
    if not sys.platform.startswith("win"):
        raise SafetyPreconditionError("Codex process is running. Cold sync precondition failed.")

    background_present = snapshot.sandbox_detected
    if background_present:
        raise SafetyPreconditionError(
            "Codex is still running (codex-windows-sandbox detected). "
            "Close Codex and retry sync."
        )

    if not cfg.process_detection.allow_terminate_if_running:
        raise SafetyPreconditionError("Codex process is running and termination is disabled by config.")

    require_manual_confirmation = _resolve_manual_terminate_confirmation(
        manual_override=manual_override,
        cfg=cfg,
    )
    details = ", ".join(f"{proc.name}:{proc.pid}" for proc in snapshot.main_processes)
    if require_manual_confirmation:
        approved = confirm_process_termination(
            "Codex main process is still running, but codex-windows-sandbox is not detected.\n\n"
            f"Detected main processes: {details}\n\n"
            "Terminate Codex processes now and continue sync?",
            mode=cfg.process_detection.terminate_confirmation_mode,
        )
        if not approved:
            raise SafetyPreconditionError("User rejected Codex process termination.")
    else:
        LOG.info("Manual termination confirmation disabled; terminating Codex process automatically.")

    terminated = detector.terminate(snapshot.main_processes, timeout_seconds=cfg.process_detection.terminate_timeout_seconds)
    if not terminated:
        raise FailSafeError("Failed to terminate Codex processes before timeout.")


def _resolve_manual_terminate_confirmation(manual_override: bool | None, cfg: AppConfig) -> bool:
    if manual_override is not None:
        return manual_override
    return bool(cfg.process_detection.manual_terminate_confirmation)


def _current_os_background_processes(cfg: AppConfig) -> list[str]:
    os_key = _current_os_key()
    configured = cfg.process_detection.background_process_names.get(os_key, [])
    return [name.strip() for name in configured if name.strip()]


def collect_codex_processes(cfg: AppConfig) -> list[ProcessInfo]:
    snapshot = collect_process_snapshot(cfg)
    return snapshot.subprocesses


def collect_process_snapshot(cfg: AppConfig, detector: CodexProcessDetector | None = None) -> ProcessSnapshot:
    detector = detector or CodexProcessDetector(cfg.process_detection.process_names)
    main, subprocesses = detector.get_subprocess_tree(cfg.process_detection.process_names)
    markers = _current_os_background_processes(cfg)
    sandbox_detected = any(
        detector.has_marker(proc, marker)
        for marker in markers
        for proc in subprocesses
    )
    if not sandbox_detected and sys.platform.startswith("win"):
        sandbox_detected = _has_enable_sandbox_flag(main + subprocesses)
    return ProcessSnapshot(
        main_processes=main,
        subprocesses=subprocesses,
        sandbox_detected=sandbox_detected,
    )


def _has_enable_sandbox_flag(processes: list[ProcessInfo]) -> bool:
    return any("--enable-sandbox" in proc.command_line.lower() for proc in processes)


def _current_os_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _ensure_dir(path: Path, field_name: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Cannot create directory for {field_name}: {path}. {exc}") from exc
    if not path.is_dir():
        raise ConfigError(f"Path for {field_name} is not a directory: {path}")


def _bootstrap_cloud_targets(cfg: AppConfig) -> None:
    if cfg.paths.cloud_root_dir is None:
        raise ConfigError("paths.cloud_root_dir is required for filesystem sync")
    root = cfg.paths.cloud_root_dir.resolve()
    for rel in cfg.targets.include_roots:
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ConfigError(f"targets.include_roots points outside cloud root: {rel}") from exc

        # Heuristic: entries with suffix are treated as files, others as directories.
        if Path(rel).suffix:
            _ensure_dir(candidate.parent, f"targets.include_roots parent for {rel}")
        else:
            _ensure_dir(candidate, f"targets.include_roots dir {rel}")


def _resolve_restore_target(cfg: AppConfig, target: str) -> Path:
    if target == "local":
        return detect_local_state_dir(cfg.paths.local_state_dir)
    if target == "cloud":
        if cfg.paths.cloud_root_dir is None:
            raise ConfigError("restore --target cloud is unavailable when storage.backend=s3")
        return cfg.paths.cloud_root_dir
    raise ConfigError(f"Unsupported restore target: {target}")


def _resolve_snapshot_dir(backup_root: Path, snapshot_name: str | None) -> Path:
    if snapshot_name:
        snapshot = (backup_root / snapshot_name).resolve()
        if snapshot.exists() and _is_supported_snapshot(snapshot):
            return snapshot
        if snapshot.suffix.lower() != ".zip":
            zip_candidate = (backup_root / f"{snapshot_name}.zip").resolve()
            if zip_candidate.exists() and _is_supported_snapshot(zip_candidate):
                return zip_candidate
        raise ConfigError(f"Backup snapshot not found: {snapshot_name}")

    snapshots = [p for p in backup_root.iterdir() if _is_supported_snapshot(p)] if backup_root.exists() else []
    if not snapshots:
        raise ConfigError(f"No backup snapshots found in: {backup_root}")
    snapshots.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return snapshots[0]


def _is_supported_snapshot(path: Path) -> bool:
    return path.is_dir() or (path.is_file() and path.suffix.lower() == ".zip")


def _build_restore_plan_from_snapshot(
    snapshot: Path,
    target_root: Path,
    include_roots: list[str],
    exclude_globs: list[str],
    temp_root: Path,
) -> tuple[SyncPlan, Path | None]:
    if snapshot.is_dir():
        return _build_restore_plan(snapshot, target_root, include_roots, exclude_globs), None
    if snapshot.is_file() and snapshot.suffix.lower() == ".zip":
        return _build_restore_plan_from_zip_snapshot(
            snapshot_zip=snapshot,
            target_root=target_root,
            include_roots=include_roots,
            exclude_globs=exclude_globs,
            temp_root=temp_root,
        )
    raise ConfigError(f"Unsupported backup snapshot format: {snapshot}")


def _build_restore_plan_from_zip_snapshot(
    snapshot_zip: Path,
    target_root: Path,
    include_roots: list[str],
    exclude_globs: list[str],
    temp_root: Path,
) -> tuple[SyncPlan, Path]:
    _ = temp_root
    path_filter = PathFilter(exclude_globs)
    allowed_roots = [root.strip("/\\") for root in include_roots if root.strip("/\\")]
    staging_dir = target_root / f".codexsync-restore-{uuid.uuid4().hex}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    actions: list[CopyAction] = []
    with zipfile.ZipFile(snapshot_zip, mode="r") as zf:
        for entry in zf.infolist():
            if entry.is_dir():
                continue
            rel = entry.filename.replace("\\", "/").strip("/\\")
            if not _is_included_root(rel, allowed_roots):
                continue
            if path_filter.is_excluded(rel):
                continue
            staged = staging_dir / f"{uuid.uuid4().hex}.bin"
            with zf.open(entry, "r") as src, staged.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            dst_path = target_root / Path(rel.replace("/", os.sep))
            actions.append(CopyAction(src=staged, dst=dst_path, relative_path=rel))
    return SyncPlan(to_local=actions, to_cloud=[]), staging_dir


def _build_restore_plan(
    snapshot_dir: Path,
    target_root: Path,
    include_roots: list[str],
    exclude_globs: list[str],
) -> SyncPlan:
    path_filter = PathFilter(exclude_globs)
    allowed_roots = [root.strip("/\\") for root in include_roots if root.strip("/\\")]
    actions: list[CopyAction] = []

    for source in snapshot_dir.rglob("*"):
        if not source.is_file():
            continue
        rel = source.relative_to(snapshot_dir).as_posix()
        if not _is_included_root(rel, allowed_roots):
            continue
        if path_filter.is_excluded(rel):
            continue
        dst = target_root / Path(rel.replace("/", os.sep))
        actions.append(CopyAction(src=source, dst=dst, relative_path=rel))

    return SyncPlan(to_local=actions, to_cloud=[])


def _is_included_root(relative_path: str, include_roots: list[str]) -> bool:
    rel = relative_path.strip("/\\")
    for root in include_roots:
        if rel == root or rel.startswith(f"{root}/"):
            return True
    return False


def _check_write_access(name: str, directory: Path) -> PreflightCheckResult:
    probe = directory / f".codexsync-preflight-{uuid.uuid4().hex}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with probe.open("w", encoding="utf-8") as fh:
            fh.write("ok")
        return PreflightCheckResult(name, "PASS", f"Write access confirmed for {directory}")
    except Exception as exc:
        return PreflightCheckResult(name, "FAIL", f"Cannot write to {directory}: {exc}")
    finally:
        probe.unlink(missing_ok=True)


def _check_process_state(cfg: AppConfig) -> PreflightCheckResult:
    if not cfg.safety.require_codex_stopped:
        return PreflightCheckResult("codex_process", "WARN", "Process check is disabled by safety.require_codex_stopped=false")

    try:
        snapshot = collect_process_snapshot(cfg)
    except Exception as exc:
        if cfg.safety.fail_on_unknown:
            return PreflightCheckResult("codex_process", "FAIL", f"Cannot verify process state safely: {exc}")
        return PreflightCheckResult("codex_process", "WARN", f"Process check unavailable: {exc}")

    if snapshot.sandbox_detected:
        return PreflightCheckResult("codex_process", "FAIL", "codex-windows-sandbox is running")
    if snapshot.main_processes:
        details = ", ".join(f"{proc.name}:{proc.pid}" for proc in snapshot.main_processes)
        return PreflightCheckResult("codex_process", "FAIL", f"Codex process is running: {details}")
    return PreflightCheckResult("codex_process", "PASS", "Codex process is not running")


def _check_clock_drift(local_dir: Path, cloud_dir: Path) -> PreflightCheckResult:
    local_probe = local_dir / f".codexsync-clock-{uuid.uuid4().hex}.tmp"
    cloud_probe = cloud_dir / f".codexsync-clock-{uuid.uuid4().hex}.tmp"
    threshold_seconds = 5
    try:
        local_probe.write_text("clock", encoding="utf-8")
        cloud_probe.write_text("clock", encoding="utf-8")
        local_mtime = local_probe.stat().st_mtime_ns
        cloud_mtime = cloud_probe.stat().st_mtime_ns
        delta_seconds = abs(local_mtime - cloud_mtime) / 1_000_000_000
        if delta_seconds > threshold_seconds:
            return PreflightCheckResult(
                "clock_drift",
                "WARN",
                f"Suspicious mtime delta between local/cloud probes: {delta_seconds:.3f}s (> {threshold_seconds}s)",
            )
        return PreflightCheckResult("clock_drift", "PASS", f"Probe mtime delta={delta_seconds:.3f}s")
    except Exception as exc:
        return PreflightCheckResult("clock_drift", "WARN", f"Clock drift probe failed: {exc}")
    finally:
        local_probe.unlink(missing_ok=True)
        cloud_probe.unlink(missing_ok=True)


def _check_orphan_temp_files(temp_dir: Path) -> PreflightCheckResult:
    if not temp_dir.exists():
        return PreflightCheckResult("orphan_temp_files", "PASS", "Temp directory does not exist yet")
    orphan_files = [path for path in temp_dir.rglob("*.tmp") if path.is_file()]
    if orphan_files:
        return PreflightCheckResult(
            "orphan_temp_files",
            "WARN",
            f"Found {len(orphan_files)} orphan temp file(s) in {temp_dir}",
        )
    return PreflightCheckResult("orphan_temp_files", "PASS", "No orphan temp files found")
