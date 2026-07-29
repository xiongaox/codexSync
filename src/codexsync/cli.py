from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .app import (
    build_context,
    collect_process_snapshot,
    print_preflight_report,
    print_plan,
    pull_s3_snapshot,
    push_s3_snapshot,
    restore_from_backup,
    run_preflight,
    run_sync,
    list_s3_snapshots,
    validate_config_only,
)
from .config import load_config
from .exceptions import ConfigError, ConflictError, FailSafeError, SafetyPreconditionError
from .exit_codes import ExitCode
from .logging_setup import configure_logging
from .models import AppConfig, LoggingConfig

LOG = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codexsync", description="codexSync CLI")
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="Path to TOML config (defaults to ./config.toml or ~/.codexsync/config.toml)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging (includes tracked process snapshot)",
    )
    terminate_mode = parser.add_mutually_exclusive_group()
    terminate_mode.add_argument(
        "--manual-terminate-confirmation",
        dest="manual_terminate_confirmation_override",
        action="store_const",
        const=True,
        help="Force GUI/user confirmation before terminating Codex processes",
    )
    terminate_mode.add_argument(
        "--auto-terminate-without-confirmation",
        dest="manual_terminate_confirmation_override",
        action="store_const",
        const=False,
        help="Auto-terminate Codex process without manual confirmation for this run only",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="Validate config only")
    sub.add_parser("doctor", help="Run environment diagnostics before sync")
    sub.add_parser("preflight", help="Run environment diagnostics before sync")
    sub.add_parser("plan", help="Build and print sync plan")
    init_cfg = sub.add_parser("init-config", help="Write example config.toml file")
    init_cfg.add_argument(
        "--output",
        default=str(_user_config_path()),
        help="Output path for generated config file (defaults to ~/.codexsync/config.toml)",
    )
    init_cfg.add_argument(
        "--force",
        action="store_true",
        help="Overwrite output file if it already exists",
    )

    sync = sub.add_parser("sync", help="Run synchronization")
    mode_group = sync.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    mode_group.add_argument("--apply", action="store_true", help="Apply changes (overrides dry-run)")

    push = sub.add_parser("push", help="Create and upload an immutable S3 snapshot")
    push_mode = push.add_mutually_exclusive_group()
    push_mode.add_argument("--dry-run", action="store_true", help="Build snapshot without uploading")
    push_mode.add_argument("--apply", action="store_true", help="Upload snapshot (default)")

    pull = sub.add_parser("pull", help="Download and restore an S3 snapshot")
    pull.add_argument("--snapshot", default=None, help="Snapshot ID (defaults to S3 latest)")
    pull.add_argument("--merge", action="store_true", help="Conservatively merge local state with the snapshot")
    pull_mode = pull.add_mutually_exclusive_group()
    pull_mode.add_argument("--dry-run", action="store_true", help="Download and validate without restoring")
    pull_mode.add_argument("--apply", action="store_true", help="Restore snapshot (default)")

    sub.add_parser("list-snapshots", help="List immutable S3 snapshots")

    restore = sub.add_parser("restore", help="Restore files from backup snapshot")
    restore.add_argument("--from", dest="snapshot", default=None, help="Snapshot directory name in backup_dir")
    restore.add_argument(
        "--target",
        choices=["local", "cloud"],
        default="local",
        help="Restore destination root",
    )
    restore_mode = restore.add_mutually_exclusive_group()
    restore_mode.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    restore_mode.add_argument("--apply", action="store_true", help="Apply restore (overrides dry-run)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config_path = _resolve_config_path(args.config)
    try:
        # Logging is configured with defaults first, then with file settings from config when context is built.
        configure_logging(LoggingConfig(level="INFO", file=None), verbose=args.verbose)
        cfg_for_verbose = None
        if args.command in {"plan", "sync", "restore", "push", "pull", "list-snapshots"}:
            try:
                cfg_for_verbose = load_config(config_path)
                configure_logging(cfg_for_verbose.logging, verbose=args.verbose)
                _emit_verbose_process_snapshot(args.verbose, cfg_for_verbose)
            except ConfigError:
                # Preserve existing behavior: detailed config errors are handled below.
                cfg_for_verbose = None
            except Exception as exc:
                LOG.warning("Verbose process snapshot setup failed, continue with default logger: %s", exc)
                if cfg_for_verbose is not None:
                    _emit_verbose_process_snapshot(args.verbose, cfg_for_verbose)

        if args.command == "validate":
            validate_config_only(config_path)
            print("Config is valid.")
            return int(ExitCode.OK)

        if args.command == "init-config":
            output_path = Path(args.output).expanduser()
            created = init_config_template(output_path=output_path, force=args.force)
            print(f"Config template written: {created}")
            return int(ExitCode.OK)

        if args.command in {"doctor", "preflight"}:
            report = run_preflight(config_path)
            print_preflight_report(report)
            if report.is_ok:
                print("Preflight checks passed.")
                return int(ExitCode.OK)
            print("Preflight checks failed.")
            return int(ExitCode.FAIL_SAFE)

        if args.command == "plan":
            ctx = build_context(
                config_path,
                manual_terminate_confirmation_override=args.manual_terminate_confirmation_override,
                enforce_safety=False,
            )
            print_plan(ctx.plan)
            return int(ExitCode.OK)

        if args.command == "sync":
            ctx = build_context(
                config_path,
                manual_terminate_confirmation_override=args.manual_terminate_confirmation_override,
                enforce_safety=True,
            )
            dry_run = ctx.config.sync.dry_run_default
            if args.dry_run:
                dry_run = True
            if args.apply:
                dry_run = False
            run_sync(ctx, dry_run=dry_run)
            print("Sync finished." if not dry_run else "Dry-run finished.")
            return int(ExitCode.OK)

        if args.command == "push":
            info = push_s3_snapshot(
                config_path,
                dry_run=bool(args.dry_run),
                manual_terminate_confirmation_override=args.manual_terminate_confirmation_override,
            )
            print(f"Push finished. snapshot={info.snapshot_id} files={info.file_count} bytes={info.total_size}")
            return int(ExitCode.OK)

        if args.command == "pull":
            info = pull_s3_snapshot(
                config_path,
                snapshot_id=args.snapshot,
                merge=bool(args.merge),
                dry_run=bool(args.dry_run),
                manual_terminate_confirmation_override=args.manual_terminate_confirmation_override,
            )
            print(f"Pull finished. snapshot={info.snapshot_id} files={info.file_count} bytes={info.total_size}")
            return int(ExitCode.OK)

        if args.command == "list-snapshots":
            for item in list_s3_snapshots(config_path):
                print(f"{item.snapshot_id}\t{item.machine_id}\t{item.created_at}\t{item.file_count}\t{item.total_size}")
            return int(ExitCode.OK)

        if args.command == "restore":
            dry_run = True
            if args.dry_run:
                dry_run = True
            if args.apply:
                dry_run = False

            result = restore_from_backup(
                config_path=config_path,
                snapshot_name=args.snapshot,
                target=args.target,
                dry_run=dry_run,
                manual_terminate_confirmation_override=args.manual_terminate_confirmation_override,
            )
            mode = "Dry-run" if dry_run else "Restore"
            print(
                f"{mode} finished. snapshot={result.snapshot_name} "
                f"target={result.target} files={result.restored_files}"
            )
            return int(ExitCode.OK)

        return int(ExitCode.BAD_INPUT)

    except ConfigError as exc:
        LOG.error("Configuration error: %s", exc)
        return int(ExitCode.BAD_INPUT)
    except SafetyPreconditionError as exc:
        LOG.error("Safety precondition failed: %s", exc)
        return int(ExitCode.CODEX_RUNNING)
    except ConflictError as exc:
        LOG.error("Conflict detected: %s", exc)
        return int(ExitCode.CONFLICT_DETECTED)
    except FailSafeError as exc:
        LOG.error("Fail-safe stop: %s", exc)
        return int(ExitCode.FAIL_SAFE)
    except Exception as exc:  # pragma: no cover
        LOG.exception("Unhandled error: %s", exc)
        return int(ExitCode.INTERNAL_ERROR)


def init_config_template(output_path: Path, force: bool) -> Path:
    if output_path.exists() and not force:
        raise ConfigError(
            f"File already exists: {output_path}. Use --force to overwrite."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    template_path = Path(__file__).resolve().with_name("config.example.toml")
    template = template_path.read_text(encoding="utf-8")
    output_path.write_text(template, encoding="utf-8")
    return output_path.resolve()


def _user_config_path() -> Path:
    return Path.home() / ".codexsync" / "config.toml"


def _resolve_config_path(raw_path: str | None) -> Path:
    if raw_path:
        return Path(raw_path).expanduser()
    project_config = Path.cwd() / "config.toml"
    if project_config.exists():
        return project_config
    return _user_config_path()


def _emit_verbose_process_snapshot(verbose: bool, cfg: AppConfig) -> None:
    if not verbose:
        return
    try:
        snapshot = collect_process_snapshot(cfg)
    except Exception as exc:
        LOG.warning("Unable to collect process snapshot: %s", exc)
        return

    if not snapshot.main_processes:
        LOG.info("Process snapshot: codex.exe is not running.")
        return

    LOG.info("Process snapshot: codex.exe running (count=%d).", len(snapshot.main_processes))
    LOG.info("Process snapshot: codex-windows-sandbox detected: %s.", "yes" if snapshot.sandbox_detected else "no")
    if not snapshot.subprocesses:
        LOG.info("Process snapshot: no subprocesses detected for codex.exe.")
        return

    LOG.info("Process snapshot: %d subprocess(es) under codex.exe.", len(snapshot.subprocesses))
    for proc in snapshot.subprocesses:
        if proc.command_line:
            LOG.info("  pid=%s name=%s cmd=%s", proc.pid, proc.name, proc.command_line)
        else:
            LOG.info("  pid=%s name=%s", proc.pid, proc.name)
