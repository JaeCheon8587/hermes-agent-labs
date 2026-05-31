"""Kanban worker bootstrap hooks for PM-managed runner tasks.

This module is intentionally tiny: the dispatcher can wrap a normal
``hermes -p <profile> chat -q ...`` worker with ``python -m`` here so mandatory
startup actions run before the LLM session begins.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def _record_runner_failure(message: str, *, returncode: int | None = None) -> None:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if not task_id:
        return
    try:
        from hermes_cli import kanban_db as kb

        with kb.connect() as conn:
            kb._record_task_failure(
                conn,
                task_id,
                message[:500],
                outcome="pm_claude_runner_failed",
                release_claim=True,
                end_run=True,
                event_payload_extra={"returncode": returncode} if returncode is not None else None,
            )
    except Exception:
        # The bootstrap runs before the normal agent logger is necessarily
        # configured. Keep failure recording best-effort and let the process
        # exit non-zero so crash detection still has a signal.
        pass


def _runner_timeout_seconds() -> int:
    raw = os.environ.get("HERMES_PM_CLAUDE_RUNNER_TIMEOUT", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return 1800


def run_startup_runner() -> int:
    if os.environ.get("HERMES_PM_CLAUDE_RUNNER_AUTORUN") != "1":
        return 0
    command = os.environ.get("HERMES_PM_CLAUDE_RUNNER_COMMAND", "").strip()
    if not command:
        _record_runner_failure("PM Claude runner autorun requested but command is empty")
        return 2
    workdir = Path(os.environ.get("HERMES_PM_CLAUDE_RUNNER_WORKDIR") or os.getcwd()).expanduser()
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        _record_runner_failure(f"PM Claude runner command parse failed: {exc}")
        return 2
    if not argv:
        _record_runner_failure("PM Claude runner command is empty after parsing")
        return 2
    timeout = _runner_timeout_seconds()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(workdir),
            env=os.environ.copy(),
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _record_runner_failure(f"PM Claude runner timed out after {exc.timeout}s")
        return 124
    except Exception as exc:
        _record_runner_failure(f"PM Claude runner startup failed: {exc}")
        return 1
    if completed.returncode != 0:
        _record_runner_failure(
            f"PM Claude runner exited with code {completed.returncode}",
            returncode=completed.returncode,
        )
    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        print("pm_worker_bootstrap: missing command after --", file=sys.stderr)
        return 2
    rc = run_startup_runner()
    if rc != 0:
        return rc
    os.execvpe(args[0], args, os.environ.copy())
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
