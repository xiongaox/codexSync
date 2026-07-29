from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .exceptions import ConfigError
from .models import (
    AppConfig,
    BackupConfig,
    ConflictConfig,
    FiltersConfig,
    IdentityConfig,
    LoggingConfig,
    PathsConfig,
    ProcessDetectionConfig,
    SafetyConfig,
    S3Config,
    StateConfig,
    StorageConfig,
    SyncConfig,
    TargetsConfig,
)


def _to_path(
    value: str | None,
    field_name: str,
    *,
    base_dir: Path,
    workspace_root: Path | None = None,
    required: bool = True,
) -> Path | None:
    if not value:
        if required:
            raise ConfigError(f"Missing required path field: {field_name}")
        return None

    resolved = _expand_workspace_var(value, workspace_root, field_name)
    raw = Path(resolved).expanduser()
    if raw.is_absolute():
        return raw
    anchor = workspace_root if workspace_root else base_dir
    return (anchor / raw).resolve()


def _expand_workspace_var(raw_value: str, workspace_root: Path | None, field_name: str) -> str:
    token = "${workspace_root}"
    if token not in raw_value:
        return raw_value
    if workspace_root is None:
        raise ConfigError(
            f"{field_name} uses {token}, but paths.workspace_root_dir is not configured"
        )
    return raw_value.replace(token, str(workspace_root))


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    base_dir = path.parent.resolve()
    with path.open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)

    identity_raw = raw.get("identity", {})
    paths_raw = raw.get("paths", {})
    sync_raw = raw.get("sync", {})
    safety_raw = raw.get("safety", {})
    proc_raw = raw.get("process_detection", {})
    backup_raw = raw.get("backup", {})
    filters_raw = raw.get("filters", {})
    targets_raw = raw.get("targets", {})
    conflict_raw = raw.get("conflict", {})
    state_raw = raw.get("state", {})
    logging_raw = raw.get("logging", {})
    storage_raw = raw.get("storage", {})
    s3_raw = raw.get("s3", {})

    identity = IdentityConfig(machine_id=identity_raw.get("machine_id"))

    workspace_root_dir = _to_path(
        paths_raw.get("workspace_root_dir"),
        "paths.workspace_root_dir",
        base_dir=base_dir,
        required=False,
    )
    cloud_root_dir = _to_path(
        paths_raw.get("cloud_root_dir"),
        "paths.cloud_root_dir",
        base_dir=base_dir,
        workspace_root=workspace_root_dir,
        required=False,
    )
    backup_dir = _to_path(
        paths_raw.get("backup_dir"),
        "paths.backup_dir",
        base_dir=base_dir,
        workspace_root=workspace_root_dir,
    )
    temp_dir = _to_path(
        paths_raw.get("temp_dir"),
        "paths.temp_dir",
        base_dir=base_dir,
        workspace_root=workspace_root_dir,
    )
    if backup_dir is None or temp_dir is None:
        raise ConfigError("paths.backup_dir and paths.temp_dir are required")

    paths = PathsConfig(
        workspace_root_dir=workspace_root_dir,
        local_state_dir=_to_path(
            paths_raw.get("local_state_dir"),
            "paths.local_state_dir",
            base_dir=base_dir,
            workspace_root=workspace_root_dir,
            required=False,
        ),
        cloud_root_dir=cloud_root_dir,
        backup_dir=backup_dir,
        temp_dir=temp_dir,
    )

    sync = SyncConfig(
        mode=sync_raw.get("mode", "cold"),
        direction=sync_raw.get("direction", "bidirectional"),
        compare=str(sync_raw.get("compare", "mtime")).strip().lower(),
        time_tolerance_seconds=int(sync_raw.get("time_tolerance_seconds", 0)),
        equal_mtime_action=str(sync_raw.get("equal_mtime_action", "skip")).strip().lower(),
        dry_run_default=bool(sync_raw.get("dry_run_default", True)),
        delete_policy=sync_raw.get("delete_policy", "never"),
        session_mode=(
            str(sync_raw.get("session_mode")).strip().lower()
            if sync_raw.get("session_mode") is not None
            else None
        ),
    )

    safety = SafetyConfig(
        require_codex_stopped=bool(safety_raw.get("require_codex_stopped", True)),
        fail_on_unknown=bool(safety_raw.get("fail_on_unknown", True)),
    )

    background_process_names = _parse_background_process_names(proc_raw)
    process_detection = ProcessDetectionConfig(
        process_names=_parse_process_names(proc_raw.get("process_names", ["codex.exe", "codex"])),
        grace_period_seconds=int(proc_raw.get("grace_period_seconds", 2)),
        allow_terminate_if_running=bool(proc_raw.get("allow_terminate_if_running", True)),
        manual_terminate_confirmation=bool(proc_raw.get("manual_terminate_confirmation", True)),
        terminate_confirmation_mode=str(proc_raw.get("terminate_confirmation_mode", "gui")).strip().lower(),
        terminate_timeout_seconds=int(proc_raw.get("terminate_timeout_seconds", 20)),
        background_process_names=background_process_names,
    )

    backup = BackupConfig(
        backup_before_overwrite=bool(backup_raw.get("backup_before_overwrite", True)),
        retention_days=int(backup_raw.get("retention_days", 30)),
        max_backups=int(backup_raw.get("max_backups", 0)),
        compression=str(backup_raw.get("compression", "none")).strip().lower(),
    )

    filters = FiltersConfig(exclude_globs=list(filters_raw.get("exclude_globs", [])))
    targets = TargetsConfig(include_roots=list(targets_raw.get("include_roots", [])))
    conflict = ConflictConfig(
        policy=conflict_raw.get("policy", "manual_abort"),
        report_conflicts=bool(conflict_raw.get("report_conflicts", True)),
    )
    state = StateConfig(
        manifest_file=_to_path(
            state_raw.get("manifest_file"),
            "state.manifest_file",
            base_dir=base_dir,
            workspace_root=workspace_root_dir,
            required=False,
        ),
        s3_metadata_file=_to_path(
            state_raw.get("s3_metadata_file"),
            "state.s3_metadata_file",
            base_dir=base_dir,
            workspace_root=workspace_root_dir,
            required=False,
        ),
        data_version=int(state_raw.get("data_version", 1)),
    )

    log_file = logging_raw.get("file")
    logging_cfg = LoggingConfig(
        level=logging_raw.get("level", "INFO"),
        file=_to_path(
            log_file,
            "logging.file",
            base_dir=base_dir,
            workspace_root=workspace_root_dir,
            required=False,
        ),
        format=logging_raw.get("format", "text"),
        retention_days=int(logging_raw.get("retention_days", 7)),
        archive_mode=str(logging_raw.get("archive_mode", "zip")).strip().lower(),
        max_file_size_mb=int(logging_raw.get("max_file_size_mb", 10)),
        machine_id=identity.machine_id,
    )
    storage = StorageConfig(backend=str(storage_raw.get("backend", "filesystem")).strip().lower())
    s3 = S3Config(
        bucket=(str(s3_raw.get("bucket")).strip() if s3_raw.get("bucket") else None),
        prefix=str(s3_raw.get("prefix", "codexsync")).strip().strip("/"),
        region=(str(s3_raw.get("region")).strip() if s3_raw.get("region") else None),
        endpoint_url=(str(s3_raw.get("endpoint_url")).strip() if s3_raw.get("endpoint_url") else None),
        addressing_style=str(s3_raw.get("addressing_style", "auto")).strip().lower(),
        verify_tls=bool(s3_raw.get("verify_tls", True)),
    )

    cfg = AppConfig(
        identity=identity,
        paths=paths,
        sync=sync,
        safety=safety,
        process_detection=process_detection,
        backup=backup,
        filters=filters,
        targets=targets,
        conflict=conflict,
        state=state,
        logging=logging_cfg,
        storage=storage,
        s3=s3,
    )
    _validate_config(cfg)
    return cfg


def _validate_config(cfg: AppConfig) -> None:
    if cfg.storage.backend not in {"filesystem", "s3"}:
        raise ConfigError("storage.backend must be one of: filesystem, s3")
    if cfg.storage.backend == "s3":
        if not cfg.s3.bucket:
            raise ConfigError("s3.bucket is required when storage.backend=s3")
        if not cfg.s3.prefix:
            raise ConfigError("s3.prefix must not be empty")
        if cfg.s3.addressing_style not in {"auto", "path", "virtual"}:
            raise ConfigError("s3.addressing_style must be one of: auto, path, virtual")
    if cfg.storage.backend == "filesystem" and cfg.paths.cloud_root_dir is None:
        raise ConfigError("paths.cloud_root_dir is required when storage.backend=filesystem")
    if cfg.sync.mode != "cold":
        raise ConfigError("Only cold sync mode is supported")

    if cfg.sync.compare not in {"mtime", "mtime_hash_fallback"}:
        raise ConfigError("sync.compare must be one of: mtime, mtime_hash_fallback")

    if cfg.sync.direction != "bidirectional":
        raise ConfigError("Only bidirectional sync direction is supported")

    if cfg.sync.delete_policy != "never":
        raise ConfigError("Only delete_policy=never is supported in MVP")

    if cfg.sync.time_tolerance_seconds < 0:
        raise ConfigError("sync.time_tolerance_seconds must be >= 0")

    allowed_equal_mtime_actions = {"skip", "prefer_local", "prefer_cloud", "manual_abort"}
    if cfg.sync.equal_mtime_action not in allowed_equal_mtime_actions:
        raise ConfigError(
            "sync.equal_mtime_action must be one of: skip, prefer_local, prefer_cloud, manual_abort"
        )

    allowed_session_modes = {None, "all", "last_date_only"}
    if cfg.sync.session_mode not in allowed_session_modes:
        raise ConfigError("sync.session_mode must be one of: all, last_date_only")

    allowed_conflict_policies = {"manual_abort", "prefer_cloud", "prefer_local", "prefer_newer_mtime"}
    if cfg.conflict.policy not in allowed_conflict_policies:
        raise ConfigError(
            "conflict.policy must be one of: manual_abort, prefer_cloud, prefer_local, prefer_newer_mtime"
        )

    if cfg.backup.compression not in {"none", "zip"}:
        raise ConfigError("backup.compression must be one of: none, zip")

    if not cfg.process_detection.process_names:
        raise ConfigError("process_detection.process_names must not be empty")

    if cfg.process_detection.terminate_timeout_seconds < 0:
        raise ConfigError("process_detection.terminate_timeout_seconds must be >= 0")

    if cfg.process_detection.terminate_confirmation_mode not in {"gui", "console"}:
        raise ConfigError("process_detection.terminate_confirmation_mode must be one of: gui, console")

    allowed_os_keys = {"windows", "macos", "linux"}
    for os_key, names in cfg.process_detection.background_process_names.items():
        if os_key not in allowed_os_keys:
            raise ConfigError(
                f"process_detection.background_process_names has unsupported OS key: {os_key}"
            )
        if not isinstance(names, list):
            raise ConfigError(
                f"process_detection.background_process_names.{os_key} must be a list of process names"
            )

    if cfg.logging.format.lower() not in {"text", "json", "logfmt"}:
        raise ConfigError("logging.format must be one of: text, json, logfmt")

    if cfg.logging.archive_mode not in {"text", "zip"}:
        raise ConfigError("logging.archive_mode must be one of: text, zip")

    if cfg.logging.retention_days < 0:
        raise ConfigError("logging.retention_days must be >= 0")

    if cfg.logging.max_file_size_mb <= 0:
        raise ConfigError("logging.max_file_size_mb must be > 0")

    if cfg.paths.local_state_dir and cfg.paths.cloud_root_dir and cfg.paths.local_state_dir == cfg.paths.cloud_root_dir:
        raise ConfigError("paths.local_state_dir and paths.cloud_root_dir must be different")


def _parse_background_process_names(proc_raw: dict[str, Any]) -> dict[str, list[str]]:
    default_mapping: dict[str, list[str]] = {
        "windows": ["codex-windows-sandbox"],
        "macos": [],
        "linux": [],
    }
    raw_mapping = proc_raw.get("background_process_names")
    if isinstance(raw_mapping, dict):
        parsed: dict[str, list[str]] = {}
        for key in ("windows", "macos", "linux"):
            value = raw_mapping.get(key, default_mapping[key])
            if not isinstance(value, list):
                raise ConfigError(
                    f"process_detection.background_process_names.{key} must be a list of process names"
                )
            parsed[key] = [str(name).strip() for name in value if str(name).strip()]
        return parsed
    return default_mapping


def _parse_process_names(raw_value: Any) -> list[str]:
    if not isinstance(raw_value, list):
        raise ConfigError("process_detection.process_names must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_value:
        name = str(item).strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result
