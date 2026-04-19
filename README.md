🇺🇸 English · [🇲🇽 Español](README.es.md)

# email-downloader

Python toolkit to back up entire IMAP mailboxes as local `.eml` files,
organized by domain, account, and folder. Built for email migrations
(preserving the historical archive before the old server is shut down) or any
scenario where you need to archive IMAP accounts in a robust and resumable
way.

## What's included

Three scripts that complement each other, from simplest to most complete:

| Script | What it's for |
|--------|---------------|
| `scripts/backup.py` | Simple first-time download of a single account (no skip logic). |
| `scripts/resume.py` | **Idempotent and resumable** download of a single account. Skips already-downloaded mail, writes atomically, reconnects if the network drops, emits a parseable `RESULT status=ok ...` final line. In practice it replaces `backup.py` for first-time downloads as well. |
| `scripts/orchestrator.py` | Runs N `resume.py` workers in parallel (default 3) over a list of accounts. Persistent per-run state, retries with backoff, drift detection on config changes, hard-crash recovery. Built to back up dozens of accounts **unattended**. |

### Why `resume.py` is idempotent

- Identifies each message by its **IMAP UID** (`UID SEARCH ALL` + `UID FETCH`),
  which is stable across sessions — unlike sequence numbers.
- Saves as `<UID>_<subject>.eml` inside the matching folder.
- Before downloading, builds a set of UIDs already present on disk and skips
  those.
- Writes each `.eml` via `tmp + os.replace()`: if you kill the process
  mid-write, no truncated file is left behind (only an orphan `.eml.tmp`
  that the next run cleans up).
- Detects "Broken pipe" / "EOF" on individual fetches and reconnects to IMAP
  without losing folder progress.

### Why the orchestrator is robust

- **Parallelism per account**, not per message. Each worker launches
  `resume.py` as an independent subprocess.
- **Persistent state** in `data/.orchestrator-runs/<run_id>/`:
  - `manifest.json`: snapshot of run inputs (immutable except via
    `--accept-drift`, see below).
  - `state.json`: live state of each account (pending/running/completed/
    failed/dropped).
  - `logs/<domain>__<account>__<attempt>.log`: stdout/stderr per attempt.
- **Retries with backoff**: 60s, 5min, 30min. After the third failure the
  account is marked `failed` without blocking the others.
- **Ctrl-C safe**: terminates workers with SIGTERM, returns `running`
  accounts to `pending`, persists state. Resume with `--resume <run_id>`.
- **Hard-crash recovery**: if the orchestrator dies via `kill -9`, OOM,
  or a power outage, on `--resume` it detects orphan `running` accounts
  and returns them to `pending` automatically (without counting the crash
  as a retry).
- **Drift detection**: if `cuentas.yaml` changed since the run started
  (accounts removed, password rotated, path changed, server changed),
  it prints a diff and forces you to choose between `--accept-drift`
  (use current values) or reverting the file.

## Requirements

- macOS, Linux, or Windows
- [`uv`](https://github.com/astral-sh/uv) (modern Python package manager).
  The scripts use PEP 723 inline metadata to declare dependencies, so `uv`
  resolves Python ≥3.12 and PyYAML automatically in an isolated venv per
  script. **No `pip install` required.**

Installing `uv`:

```bash
# macOS
brew install uv

# Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# or:  winget install --id=astral-sh.uv -e
```

### Notes for Windows

The scripts work on Windows with three minor adjustments compared to the
documentation below:

- **Invocation**: the shebang `#!/usr/bin/env -S uv run --quiet` doesn't
  apply on Windows. Use `uv run scripts/foo.py ...` instead of
  `./scripts/foo.py ...`.
- **Password file paths**: instead of `/tmp/mail-pass-x`, use something
  like `%TEMP%\mail-pass-x` or an absolute Windows path. The path is just
  a string in `cuentas.yaml`; the code doesn't assume any platform.
- **Orchestrator's `latest` symlink**: `os.symlink()` on Windows requires
  admin privileges or "developer mode" enabled. Without it, you'll see a
  warning `! No se pudo actualizar symlink 'latest'` and the orchestrator
  will keep working normally — you just lose the `--resume latest`
  shortcut and have to pass the explicit `<run_id>`
  (`--resume 2026-04-19T1030-...`).

## Configuration

1. Clone the repo and copy the template:

    ```bash
    git clone https://github.com/lmuzquiz/email-downloader.git
    cd email-downloader
    cp config/cuentas.example.yaml config/cuentas.yaml
    ```

2. Edit `config/cuentas.yaml` with your real domains and accounts (structure
   documented inside the template).

3. Create the password files referenced by `password_file` in each account.
   Typical pattern:

    ```bash
    echo 'your-password-here' > /tmp/mail-pass-<something>
    chmod 600 /tmp/mail-pass-<something>
    ```

   `/tmp/` is ephemeral (cleared on reboot) — if you need persistence, use
   another path. Multiple accounts can share the same `password_file` if they
   share the same password.

## Usage

### Manual mode (one account at a time)

For one-off cases or debugging a specific account:

```bash
# Resumable download (recommended — also works for first-time)
./scripts/resume.py <DOMAIN> <ACCOUNT-NAME>

# Simple download (no skip logic; rarely needed)
./scripts/backup.py <DOMAIN> <ACCOUNT-NAME>
```

`<DOMAIN>` and `<ACCOUNT-NAME>` must match an entry in `cuentas.yaml`. The
output ends with a line like:

```
RESULT status=ok carpetas_total=7 carpetas_saltadas=0 correos_nuevos=151 correos_fallidos=0 reconexiones_fallidas=0
```

(Field names are in Spanish for historical reasons; mapping:
`carpetas_total`=total folders, `carpetas_saltadas`=folders skipped,
`correos_nuevos`=new messages downloaded, `correos_fallidos`=failed messages,
`reconexiones_fallidas`=failed reconnections.)

Exit code: `0` if everything is clean, `1` if there was any partial failure.

### Orchestrated mode (many accounts, unattended)

```bash
# New run against one or more domains
./scripts/orchestrator.py --dominios <DOMAIN1>,<DOMAIN2>,<DOMAIN3>

# Resume an interrupted run
./scripts/orchestrator.py --resume <run_id>
./scripts/orchestrator.py --resume latest

# List previous runs and their status
./scripts/orchestrator.py --list-runs

# Show drift against the manifest without resuming
./scripts/orchestrator.py --resume <run_id> --diff-manifest

# Resume accepting changes to cuentas.yaml since the original manifest
./scripts/orchestrator.py --resume <run_id> --accept-drift
```

Useful flags:

- `--workers <N>`: default `3`. Don't increase without testing against the
  target IMAP server — it may rate-limit your IP or drop connections.
- `--max-reintentos <N>`: default `3`. After that the account stays
  `failed`.

### When to use which

- **1 account:** manual mode.
- **3+ accounts, or you want to set it and forget it:** orchestrator.
- **Mass migration (dozens of mailboxes, hours of downloads):** orchestrator,
  with passwords temporarily shared across accounts of the same domain (all
  pointing to the same `password_file`) so you don't have to rotate each one
  individually.

## Data layout

```
data/
├── <domain>/
│   └── <account>/
│       ├── .uid-format            # marker: this directory uses UIDs
│       ├── INBOX/
│       │   ├── 12345_subject.eml  # prefix = IMAP UID
│       │   └── ...
│       ├── INBOX.Sent/
│       └── ...
└── .orchestrator-runs/            # only if you use the orchestrator
    ├── latest -> <run_id>         # symlink to the most recent run
    └── <run_id>/
        ├── manifest.json
        ├── state.json
        └── logs/
```

Everything inside `data/` is gitignored.

## Troubleshooting

### "Falta PyYAML" (PyYAML is missing) when running a script
You invoked it with `python3` directly, bypassing the `uv` shebang. Use
`./scripts/foo.py` (which honors the shebang) or `uv run scripts/foo.py`.

### A worker fails with auth error
The `password_file` doesn't exist, is empty, or the password is wrong. The
orchestrator retries 3 times with backoff (60s/5min/30min) — if it keeps
failing, check the file and relaunch with `--resume`.

### Folder names with accents / non-ASCII characters fail to open
IMAP folder names use **modified UTF-7** (RFC 3501), not UTF-8. The scripts
already implement the codec; if you see an error, report the exact console
output.

### A run was interrupted (Ctrl-C, kill, power outage)
All state is persisted in `data/.orchestrator-runs/<run_id>/`. Relaunch
with `./scripts/orchestrator.py --resume <run_id>` (or `--resume latest`).
The orchestrator recovers orphan `running` accounts and continues without
counting the crash as a retry.

### There are legacy files named like `00001_*.eml`
They're from an earlier version that used IMAP sequence numbers. The
current code uses UIDs and writes filenames as `<UID>_<subject>.eml`. If
you have legacy data and want to resume cleanly over it, rename the
account folder first (`mv data/<dom>/<account>
data/<dom>/<account>.legacy-seq`) and let the script do a fresh
UID-based download from scratch.

### CLI args are in Spanish
The original project was developed in a Spanish-speaking context, so the
CLI uses `--dominios`, `--workers`, `--max-reintentos`, etc. The
functionality is universal — only the surface syntax reflects the project's
origin. PRs welcome to add English aliases if there's interest.

## License

MIT — see `LICENSE`.
