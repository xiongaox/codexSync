# codexSync

Open-source utility for syncing local Codex state between personal machines using a cloud-synced folder.

> [!IMPORTANT]
> Current real-world validation is Windows-to-Windows only.
> macOS support exists in code/CI, but end-to-end handoff on real macOS machines is not yet validated.

## Why

Developers may want to continue working with Codex on another machine without losing local session state.

## What this does

* Syncs local Codex state directory
* Works only after Codex process is closed
* Uses any cloud-synced folder (Dropbox, OneDrive, Syncthing, etc.)

## What this does NOT do

* No integration with Codex internals
* No API usage
* No token extraction
* No network interception
* No real-time sync
* No checks for cloud client process/health
* No checks for free space in cloud/network storage

## Design principles

* Simple and predictable
* Safe (no corruption)
* Offline-friendly
* Backup-first
* Windows-first

## How it works (MVP)

1. Detect if Codex is running
2. If not running:

   * Compare local and cloud state
   * Sync newer files
   * Create backup before overwrite

## Conflict policy

`conflict.policy` supports:

- `manual_abort`: report conflict and stop (default)
- `prefer_cloud`: auto-resolve conflict by taking cloud version
- `prefer_local`: auto-resolve conflict by taking local version
- `prefer_newer_mtime`: auto-resolve conflict by taking side with newer mtime

## Sync options

`sync.compare` controls file comparison strategy:

- `mtime` (default): compare by `size + mtime`
- `mtime_hash_fallback`: fast `size + mtime` path, but when values are equal/close (within tolerance), compare file content hash (SHA-256)

`sync.equal_mtime_action` controls behavior when file mtimes are equal (within `sync.time_tolerance_seconds`) but files differ:

- `skip`: do not copy (default)
- `prefer_local`: copy local version to cloud
- `prefer_cloud`: copy cloud version to local
- `manual_abort`: mark as conflict and stop in `manual_abort` conflict mode

`sync.session_mode` controls scope of `sessions` sync:

- `all`: sync all `sessions` content (default behavior)
- `last_date_only`: sync only the latest date-based folder under `sessions`
  - supported layouts: `sessions/YYYY-MM-DD/...` and `sessions/YYYY/MM/DD/...`
  - if no date-based layout is detected, codexSync logs a warning and syncs all `sessions` files

## Backups

- `backup.compression` supports:
  - `none` (default): backup snapshot is stored as directory tree
  - `zip`: backup snapshot is stored as a single `.zip` file
- `restore` supports both snapshot formats and auto-selects the latest by mtime if `--from` is omitted.

## Logging

- Levels: `DEBUG|INFO|WARNING|ERROR`
- Formats (configurable): `text|json|logfmt`
- Rotation/size/retention/archive rules apply equally to all formats (`text`, `json`, `logfmt`)
- UTF-8 for all log files
- Daily log files with machine id (`<stem>-<machine>-YYYY-MM-DD[.N].log`)
- Daily/time rotation + size rotation (`logging.max_file_size_mb`, default `10`)
- Retention cleanup (`logging.retention_days`, default `7`)
- Old log storage mode (`logging.archive_mode`):
  - `zip` (default): archive rotated/old logs into `.zip`
  - `text`: keep rotated logs as plain text files

## Platform and CI

- Runtime support is currently Windows-first.
- macOS support is allowed in current project scope (Apple Silicon target).
- Linux runtime support is intentionally out of MVP scope for now.
- CI currently runs on:
  - `windows-latest`
  - `macos-latest`

## CLI commands

Run from project root:

```powershell
python -m codexsync -c config.toml <command>
```

Generate `config.toml` from bundled template:

```powershell
python -m codexsync init-config
```

Generate to a custom location:

```powershell
python -m codexsync init-config --output D:\codexSync\config.toml
```

Overwrite existing config file:

```powershell
python -m codexsync init-config --output D:\codexSync\config.toml --force
```

Validation:

```powershell
python -m codexsync -c config.toml validate
```

Preflight diagnostics (same behavior for `doctor` and `preflight`):

```powershell
python -m codexsync -c config.toml doctor
python -m codexsync -c config.toml preflight
```

Build sync plan (no changes):

```powershell
python -m codexsync -c config.toml plan
```

Build plan with process snapshot (`--verbose`):

```powershell
python -m codexsync -c config.toml -v plan
```

Sync simulation (safe test):

```powershell
python -m codexsync -c config.toml sync --dry-run
```

Sync simulation with process snapshot (`--verbose`):

```powershell
python -m codexsync -c config.toml -v sync --dry-run
```

Real sync (writes files):

```powershell
python -m codexsync -c config.toml sync --apply
```

Typical handoff to another machine:

```powershell
python -m codexsync -c config.toml sync --apply
```

Restore from latest backup snapshot to local state:

```powershell
python -m codexsync -c config.toml restore --apply
```

Restore from specific backup snapshot:

```powershell
python -m codexsync -c config.toml restore --from <snapshot_dir_name> --apply
```

Restore from specific zip snapshot:

```powershell
python -m codexsync -c config.toml restore --from <snapshot_name.zip> --apply
```

Restore to cloud target instead of local:

```powershell
python -m codexsync -c config.toml restore --target cloud --apply
```

Preview restore without writing:

```powershell
python -m codexsync -c config.toml restore --dry-run
```

List CLI help:

```powershell
python -m codexsync -h
```

## S3 / Cloudflare R2 snapshots

Set `storage.backend = "s3"` and configure `[s3]` with a private bucket and S3-compatible endpoint. Credentials are supplied through the AWS SDK credential chain, not `config.toml`.

For Cloudflare R2, use `addressing_style = "path"` and `region = "auto"`:

```toml
[storage]
backend = "s3"

[s3]
bucket = "codexsync"
prefix = "codexsync"
endpoint_url = "https://ACCOUNT_ID.r2.cloudflarestorage.com"
region = "auto"
addressing_style = "path"
```

Set the R2 API token values in each machine's environment before running the CLI:

```powershell
$env:AWS_ACCESS_KEY_ID = "..."
$env:AWS_SECRET_ACCESS_KEY = "..."
```

```powershell
# Source machine: close Codex, then publish an immutable snapshot.
python -m codexsync -c config.toml push

# Target machine: close Codex, then restore the latest snapshot.
python -m codexsync -c config.toml pull

# When both machines have new content, conservatively merge first.
python -m codexsync -c config.toml pull --merge

# Inspect or restore a historical snapshot.
python -m codexsync -c config.toml list-snapshots
python -m codexsync -c config.toml pull --snapshot <snapshot-id>
```

`push` publishes a ZIP plus a SHA-256 manifest before updating the remote `latest` pointer. `pull` verifies both before any local write and always creates the configured local backup before overwriting. A push from an out-of-date machine is rejected; run `pull` or `pull --merge` first.

## Quick daily workflow

Install once with `python -m pip install -e .`, then use the short `cs` command. `cs init-config` writes the default config to `~/.codexsync/config.toml`; every command automatically uses `./config.toml` when present, otherwise that user config file. `-c <path>` remains available for alternate configurations.

```text
Source machine: close Codex -> cs push
Target machine: close Codex -> cs pull -> open Codex
Both machines changed: close Codex -> cs pull --merge -> resolve conflicts -> cs push
```

Process termination behavior:

- On Windows, if Codex is still running during `sync`/`restore`, codexSync can terminate Codex processes before continuing.
- By default, manual GUI confirmation is enabled (`process_detection.manual_terminate_confirmation = true`).
- Background process tracking is configured by OS in `process_detection.background_process_names`:
  - `windows = ["codex-windows-sandbox"]`
  - `macos = []`
  - `linux = []`
- Confirmation channel is configured by `process_detection.terminate_confirmation_mode = "gui" | "console"` (default: `gui`).
- All confirmation prompts are in English in both GUI and console modes.
- If `codex-windows-sandbox` is detected, codexSync reports that Codex is still running and exits with code `3` (no auto-terminate).
- If `codex.exe` is running but `codex-windows-sandbox` is not detected, codexSync asks whether to terminate Codex and continue.
- You can force manual confirmation from CLI:

```powershell
python -m codexsync -c config.toml --manual-terminate-confirmation sync --apply
```

- You can force auto-terminate without manual confirmation for the current run only:

```powershell
python -m codexsync -c config.toml --auto-terminate-without-confirmation sync --apply
```

- On macOS/Linux, previous behavior is kept: running Codex process fails safety precondition.
- `--verbose` works for `plan`, `sync --dry-run`, `sync --apply`, `restore --dry-run`, and `restore --apply`; it logs tracked processes with PID/name.
- In verbose mode on Windows, codexSync logs only:
  - whether `codex.exe` is running,
  - whether `codex-windows-sandbox` is detected,
  - subprocesses under `codex.exe` (PID/name/cmd).

Termination-related exit codes for automation:

- `3` Codex is running / sandbox detected / user rejected termination
- `5` termination was approved but failed before timeout (fail-safe)

## CLI exit codes

- `0` success
- `1` runtime error
- `2` conflict detected (manual resolution required)
- `3` Codex is running (cold sync precondition failed)
- `4` invalid config or CLI arguments
- `5` safe abort (`fail-safe`)

`doctor`/`preflight` return:

- `0` when all checks passed or warnings only
- `5` when at least one preflight check failed

## Required operation protocol

This tool assumes a strict handoff flow between machines:

1. Close Codex on machine A.
2. Wait until cloud sync fully propagates machine A changes.
3. Run codexSync on machine B.
4. Start Codex on machine B only after sync completes.
5. Sign in to Codex again on machine B after file sync.

Important: per OpenAI licensing constraints, authentication tokens are not transferred by codexSync.

The project intentionally does not verify cloud-provider sync status, cloud client process state, or free space on cloud/network storage. These are user responsibilities.

## Scheduler setup scripts

This repository includes editable scheduler setup templates:

- Windows Task Scheduler:
  - `scripts/scheduler/windows/task.config.ps1`
  - `scripts/scheduler/windows/install-task.ps1`
  - `scripts/scheduler/windows/remove-task.ps1`
  - detailed guide: [scripts/scheduler/windows/README.MD](./scripts/scheduler/windows/README.MD)
- macOS launchd (LaunchAgent):
  - `scripts/scheduler/macos/launchd.config.sh`
  - `scripts/scheduler/macos/install-launchd.sh`
  - `scripts/scheduler/macos/uninstall-launchd.sh`
  - detailed guide: [scripts/scheduler/macos/README.MD](./scripts/scheduler/macos/README.MD)

Windows install:

```powershell
cd scripts/scheduler/windows
# 1) Edit task.config.ps1
.\install-task.ps1
```

Windows remove:

```powershell
cd scripts/scheduler/windows
.\remove-task.ps1
```

macOS install:

```bash
cd scripts/scheduler/macos
# 1) Edit launchd.config.sh
chmod +x install-launchd.sh uninstall-launchd.sh run-codexsync.sh
./install-launchd.sh
```

macOS remove:

```bash
cd scripts/scheduler/macos
./uninstall-launchd.sh
```

Important:

- These scripts only register scheduled jobs, not OS services.
- Keep `MODE="dry-run"` while validating behavior; switch to `apply` only when ready.
- Cold sync protocol still applies: codexSync must run only when Codex is not running.

## Status

MVP (ready for public repository and community testing)

## Publishing

See release checklist: [docs/PUBLISHING.MD](./docs/PUBLISHING.MD)
Release notes: [CHANGELOG.MD](./CHANGELOG.MD)

## Licensing

This project uses dual licensing:

- Open-source license: `GPL-3.0-or-later` (see [LICENSE](./LICENSE))
- Commercial licensing path: see [COMMERCIAL_LICENSE.MD](./COMMERCIAL_LICENSE.MD)

Contributions are accepted under project contribution terms in:

- [CONTRIBUTING.MD](./CONTRIBUTING.MD)
- [CLA.MD](./CLA.MD)
