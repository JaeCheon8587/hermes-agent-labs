"""Kanban worker bootstrap hooks for PM-managed runner tasks.

This module is intentionally tiny: the dispatcher can wrap a normal
``hermes -p <profile> chat -q ...`` worker with ``python -m`` here so mandatory
startup actions run before the LLM session begins.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
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


def _bootstrap_log_path(command: str, workdir: Path) -> Path | None:
    raw = os.environ.get("HERMES_PM_CLAUDE_BOOTSTRAP_LOG", "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else workdir / path
    manifest_path = _resolve_runner_manifest_path(command, workdir)
    if manifest_path is None:
        return None
    return manifest_path.with_suffix(".bootstrap.log")


def _write_bootstrap_log(command: str, workdir: Path, message: str) -> None:
    path = _bootstrap_log_path(command, workdir)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def _runner_expected_run_id() -> int | None:
    raw = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _extract_runner_flag(command: str, flag: str) -> str:
    try:
        argv = shlex.split(command)
    except ValueError:
        return ""
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
        if token.startswith(flag + "="):
            return str(token.split("=", 1)[1]).strip()
    return ""


def _resolve_runner_manifest_path(command: str, workdir: Path) -> Path | None:
    raw = os.environ.get("HERMES_PM_CLAUDE_RUNNER_MANIFEST", "").strip()
    if not raw:
        raw = _extract_runner_flag(command, "--manifest-path")
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = workdir / path
    return path


def _relativize_to_workdir(path: Path, workdir: Path) -> str:
    try:
        return path.resolve().relative_to(workdir.resolve()).as_posix()
    except Exception:
        return str(path)


def _read_runner_manifest(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _runner_artifact_path(manifest: dict, workdir: Path) -> Path | None:
    raw = str(manifest.get("artifact_path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = workdir / path
    return path


def _runner_success_summary(manifest: dict, workdir: Path) -> tuple[str, str, dict]:
    mode = str(manifest.get("mode") or "worker").strip() or "worker"
    artifact_path = _runner_artifact_path(manifest, workdir)
    artifact_display = _relativize_to_workdir(artifact_path, workdir) if artifact_path else "(missing artifact path)"
    status = str(manifest.get("status") or "completed").strip() or "completed"
    summary = f"PM Claude runner {mode} completed. Artifact: {artifact_display}"
    result_lines = [
        f"PM Claude runner {mode} completed with status={status}.",
        f"Artifact: {artifact_display}",
    ]
    metadata = {
        "claude_delegation": {
            "used": True,
            "status": status,
            "mode": mode,
            "manifest_path": _relativize_to_workdir(Path(str(manifest.get("manifest_path") or "")).expanduser(), workdir)
            if str(manifest.get("manifest_path") or "").strip()
            else "",
            "artifact_path": artifact_display if artifact_path else "",
        },
        "artifacts": [str(artifact_path)] if artifact_path else [],
        "runner": "pm_claude_delegation",
    }
    if artifact_path and artifact_path.is_file():
        try:
            artifact_text = artifact_path.read_text(encoding="utf-8")
        except Exception:
            artifact_text = ""
        first_meaningful = next((line.strip() for line in artifact_text.splitlines() if line.strip()), "")
        if first_meaningful:
            result_lines.append(first_meaningful)
    return summary, "\n".join(result_lines), metadata


def _runner_failure_reason(manifest: dict, workdir: Path, manifest_path: Path) -> str:
    mode = str(manifest.get("mode") or "worker").strip() or "worker"
    status = str(manifest.get("status") or "failed").strip() or "failed"
    exit_code = manifest.get("exit_code")
    reasons = [f"PM Claude runner {mode} ended with status={status} exit_code={exit_code}."]
    artifact_path = _runner_artifact_path(manifest, workdir)
    if artifact_path is not None:
        reasons.append(f"Artifact: {_relativize_to_workdir(artifact_path, workdir)}")
    validation = manifest.get("artifact_contract_validation")
    if isinstance(validation, dict):
        errors = [str(item).strip() for item in (validation.get("errors") or []) if str(item).strip()]
        if errors:
            reasons.append("Validation: " + "; ".join(errors))
    repair = manifest.get("artifact_repair")
    if isinstance(repair, dict) and repair.get("attempts"):
        reasons.append(f"Repair attempts: {len(repair.get('attempts') or [])}")
    reasons.append(f"Manifest: {_relativize_to_workdir(manifest_path, workdir)}")
    return "\n".join(reasons)


def _finalize_runner_task(command: str, workdir: Path) -> int | None:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if not task_id:
        return None
    manifest_path = _resolve_runner_manifest_path(command, workdir)
    if manifest_path is None:
        _write_bootstrap_log(command, workdir, "finalize skipped: no manifest path")
        return None

    from hermes_cli import kanban_db as kb

    expected_run_id = _runner_expected_run_id()
    _write_bootstrap_log(command, workdir, f"finalize begin task_id={task_id} manifest={manifest_path}")
    if not manifest_path.is_file():
        reason = f"PM Claude runner finished but manifest is missing: {_relativize_to_workdir(manifest_path, workdir)}"
        _write_bootstrap_log(command, workdir, reason)
        with kb.connect() as conn:
            task = kb.get_task(conn, task_id)
            if task is None:
                return 0
            if task.status in {"done", "blocked", "archived"}:
                return 0
            if not kb.block_task(conn, task_id, reason=reason, expected_run_id=expected_run_id):
                _record_runner_failure(reason)
                return 1
        return 0

    manifest = _read_runner_manifest(manifest_path)
    if manifest is None:
        reason = f"PM Claude runner manifest is invalid JSON: {_relativize_to_workdir(manifest_path, workdir)}"
        _write_bootstrap_log(command, workdir, reason)
        with kb.connect() as conn:
            task = kb.get_task(conn, task_id)
            if task is None:
                return 0
            if task.status in {"done", "blocked", "archived"}:
                return 0
            if not kb.block_task(conn, task_id, reason=reason, expected_run_id=expected_run_id):
                _record_runner_failure(reason)
                return 1
        return 0

    status = str(manifest.get("status") or "").strip().lower()
    exit_code = manifest.get("exit_code")
    ok = status in {"completed", "success"} and isinstance(exit_code, int) and exit_code == 0
    _write_bootstrap_log(command, workdir, f"finalize manifest status={status} exit_code={exit_code} ok={ok}")
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        if task is None:
            return 0
        if task.status in {"done", "blocked", "archived"}:
            return 0
        if ok:
            summary, result, metadata = _runner_success_summary(manifest, workdir)
            completed = kb.complete_task(
                conn,
                task_id,
                summary=summary,
                result=result,
                metadata=metadata,
                expected_run_id=expected_run_id,
            )
            _write_bootstrap_log(command, workdir, f"finalize complete_task completed={completed}")
            if not completed:
                _record_runner_failure("PM Claude runner finished successfully but task completion failed")
                return 1
            return 0
        reason = _runner_failure_reason(manifest, workdir, manifest_path)
        blocked = kb.block_task(conn, task_id, reason=reason, expected_run_id=expected_run_id)
        _write_bootstrap_log(command, workdir, f"finalize block_task blocked={blocked}")
        if not blocked:
            _record_runner_failure(reason)
            return 1
    return 0


def run_startup_runner() -> int:
    if os.environ.get("HERMES_PM_CLAUDE_RUNNER_AUTORUN") != "1":
        return 0
    command = os.environ.get("HERMES_PM_CLAUDE_RUNNER_COMMAND", "").strip()
    if not command:
        _record_runner_failure("PM Claude runner autorun requested but command is empty")
        return 2
    workdir = Path(os.environ.get("HERMES_PM_CLAUDE_RUNNER_WORKDIR") or os.getcwd()).expanduser()
    _write_bootstrap_log(command, workdir, f"startup runner begin cwd={workdir} command={command}")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        _write_bootstrap_log(command, workdir, f"startup runner parse failed: {exc}")
        _record_runner_failure(f"PM Claude runner command parse failed: {exc}")
        return 2
    if not argv:
        _write_bootstrap_log(command, workdir, "startup runner empty argv after parsing")
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
        _write_bootstrap_log(command, workdir, f"startup runner timed out timeout={exc.timeout}")
        _record_runner_failure(f"PM Claude runner timed out after {exc.timeout}s")
        return 124
    except Exception as exc:
        _write_bootstrap_log(command, workdir, f"startup runner failed: {exc}")
        _record_runner_failure(f"PM Claude runner startup failed: {exc}")
        return 1
    _write_bootstrap_log(command, workdir, f"startup runner finished rc={completed.returncode}")
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
    command = os.environ.get("HERMES_PM_CLAUDE_RUNNER_COMMAND", "").strip()
    workdir = Path(os.environ.get("HERMES_PM_CLAUDE_RUNNER_WORKDIR") or os.getcwd()).expanduser()
    finalized = _finalize_runner_task(command, workdir)
    if finalized is not None:
        return finalized
    if rc != 0:
        return rc
    os.execvpe(args[0], args, os.environ.copy())
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
