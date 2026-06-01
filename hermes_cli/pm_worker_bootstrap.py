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


_ARCHITECT_INTAKE_REQUIRED_SECTIONS = (
    "# Architect Task Envelope",
    "## Stage boundary",
    "## Project constraints",
    "## Original user request",
    "## Architect task",
    "## Expected output classes",
)
_ARCHITECT_INTAKE_BLOCKED_RC = 75


def _arg_value(argv: list[str], name: str) -> str:
    try:
        idx = argv.index(name)
    except ValueError:
        return ""
    if idx + 1 >= len(argv):
        return ""
    return str(argv[idx + 1] or "").strip()


def _is_architect_runner(argv: list[str]) -> bool:
    return _arg_value(argv, "--mode") == "architect" and any("pm_claude_delegation" in part for part in argv)


def _validate_architect_instruction_envelope(text: str) -> dict[str, list[str]]:
    missing: list[str] = []
    empty: list[str] = []
    conflicts: list[str] = []
    body = text or ""

    for section in _ARCHITECT_INTAKE_REQUIRED_SECTIONS:
        if section not in body:
            missing.append(section)

    if "(empty)" in body:
        empty.append("placeholder '(empty)' remains in architect instruction")
    if "project_root:" not in body or "project_root: " not in body:
        missing.append("project_root")
    if "design_artifact_path:" not in body or "design_artifact_path: " not in body:
        missing.append("design_artifact_path")
    if "사용자 설계 승인 전 구현을 시작하지 않는다" not in body:
        missing.append("approval boundary: no implementation before user design approval")
    if "production 코드와 테스트 코드는 수정하지 않는다" not in body:
        missing.append("design-only write boundary")
    if "architect/design-only" not in body:
        missing.append("architect/design-only stage boundary")

    lowered = body.lower()
    implementation_markers = (
        "지금 구현", "구현을 완료", "코드를 수정", "파일을 수정", "테스트를 작성",
        "implement now", "modify the code", "write tests now",
    )
    if any(marker in lowered for marker in implementation_markers):
        conflicts.append("architect instruction appears to request implementation during design-only stage")

    return {"missing": missing, "empty": empty, "conflicts": conflicts}


def _format_architect_instruction_block_comment(result: dict[str, list[str]]) -> str:
    lines = [
        "[Architect 작업 지시서 보완 필요]",
        "",
        "설계를 시작하기 전에 PM 작업 지시서 필수 항목 보완이 필요합니다.",
    ]
    if result.get("missing"):
        lines.extend(["", "누락 항목:", *[f"- {item}" for item in result["missing"]]])
    if result.get("empty"):
        lines.extend(["", "비어 있거나 placeholder인 항목:", *[f"- {item}" for item in result["empty"]]])
    if result.get("conflicts"):
        lines.extend(["", "충돌 항목:", *[f"- {item}" for item in result["conflicts"]]])
    lines.extend([
        "",
        "PM 조치 필요:",
        "- 위 항목을 보완한 작업 지시서를 task body 또는 comment로 추가해 주세요.",
        "- 보완 후 task를 unblock하면 Architect intake gate가 다시 검수합니다.",
    ])
    return "\n".join(lines)


def _block_architect_instruction_incomplete(result: dict[str, list[str]]) -> None:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if not task_id:
        return
    reason_parts = []
    for key in ("missing", "empty", "conflicts"):
        values = result.get(key) or []
        if values:
            reason_parts.append(f"{key}=" + ", ".join(values[:5]))
    reason = "architect_instruction_incomplete: " + "; ".join(reason_parts)
    try:
        expected_run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
        run_id = int(expected_run_id) if expected_run_id.isdigit() else None
        from hermes_cli import kanban_db as kb

        with kb.connect() as conn:
            try:
                kb.add_comment(conn, task_id, "backend-architect", _format_architect_instruction_block_comment(result))
            except Exception:
                pass
            kb.block_task(conn, task_id, reason=reason[:500], expected_run_id=run_id)
    except Exception:
        pass


def _run_architect_instruction_intake_gate(argv: list[str], workdir: Path) -> bool:
    if not _is_architect_runner(argv):
        return False
    prompt_file = _arg_value(argv, "--prompt-file")
    if not prompt_file:
        result = {"missing": ["--prompt-file"], "empty": [], "conflicts": []}
    else:
        path = Path(prompt_file).expanduser()
        if not path.is_absolute():
            path = workdir / path
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            result = {"missing": [f"prompt file readable: {exc}"], "empty": [], "conflicts": []}
        else:
            result = _validate_architect_instruction_envelope(text)
    if any(result.values()):
        _block_architect_instruction_incomplete(result)
        return True
    return False


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
    if _run_architect_instruction_intake_gate(argv, workdir):
        return _ARCHITECT_INTAKE_BLOCKED_RC
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
    if rc == _ARCHITECT_INTAKE_BLOCKED_RC:
        return 0
    if rc != 0:
        return rc
    os.execvpe(args[0], args, os.environ.copy())
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
