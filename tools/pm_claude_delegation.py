"""Python runner that makes architect/implementer Claude Code delegation auditable.

The PM workflow uses this runner as the mandatory execution path for design and
implementation workers. The runner executes a configured command, feeds it the
Hermes-generated prompt on stdin, writes the command output to the requested
artifact, and records an immutable-ish manifest under .soul/artifacts/claude/.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

_VALID_MODES = {"architect", "implementer"}
_VALID_FORMATS = {"design", "implementation"}
_SUCCESS_STATUSES = {"completed", "success"}
_MAX_ARCHITECT_ARTIFACT_REPAIR_ATTEMPTS = 2
_FALLBACK_ARCHITECT_HEADINGS = [
    "## 상태",
    "## 설계 요약",
    "## 목표 / 범위",
    "## 현황 분석",
    "## 검토한 대안",
    "## 선택한 설계",
    "## 영향 범위",
    "## 구현 작업분해",
    "## 검증 계획",
    "## 리스크와 완화 방안",
    "## 사용자 확인 필요사항",
    "## 구현 승인 전제",
]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _as_path(root: Path, value: str) -> Path:
    path = Path(str(value or "").strip()).expanduser()
    if path.is_absolute():
        return path
    return root / path


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return str(path)


def _run_command(argv: list[str], prompt: str, root: Path, timeout: int) -> tuple[subprocess.CompletedProcess[str], float]:
    start = time.monotonic()
    completed = subprocess.run(
        argv,
        input=prompt,
        text=True,
        capture_output=True,
        cwd=str(root),
        timeout=timeout,
        check=False,
    )
    return completed, time.monotonic() - start


def _extract_required_architect_headings(prompt: str) -> list[str]:
    headings: list[str] = []
    for match in re.finditer(r"`(## [^`\n]+)`", prompt or ""):
        heading = match.group(1).strip()
        if heading.startswith("## ") and heading not in headings:
            headings.append(heading)
    return headings or list(_FALLBACK_ARCHITECT_HEADINGS)


def _section_body(text: str, heading: str) -> str:
    marker = heading.strip()
    start = text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    rest = text[start:]
    next_match = re.search(r"(?m)^## ", rest)
    if next_match:
        rest = rest[: next_match.start()]
    return rest.strip()


def _validate_architect_artifact_text(text: str, required_headings: list[str]) -> dict[str, Any]:
    content = text or ""
    missing = [heading for heading in required_headings if heading not in content]
    empty = [heading for heading in required_headings if heading not in missing and not _section_body(content, heading)]
    first_nonblank = ""
    for line in content.splitlines():
        if line.strip():
            first_nonblank = line.strip()
            break
    errors: list[str] = []
    if missing:
        errors.append("missing required headings: " + ", ".join(missing))
    if empty:
        errors.append("empty required sections: " + ", ".join(empty))
    if required_headings and first_nonblank != required_headings[0]:
        errors.append(f"first non-empty line must be `{required_headings[0]}`")
    return {
        "ok": not errors,
        "missing_headings": missing,
        "empty_sections": empty,
        "first_nonblank": first_nonblank,
        "errors": errors,
    }


def _build_architect_repair_prompt(
    *,
    original_prompt: str,
    failed_artifact: str,
    validation: dict[str, Any],
    required_headings: list[str],
    attempt: int,
) -> str:
    strictness = "" if attempt == 1 else "\n이것은 마지막 자동 재시도다. 각 heading 아래에는 최소 2개 bullet 또는 2문장 이상을 작성한다.\n"
    return "\n".join([
        "# Architect Artifact Repair Prompt",
        "",
        "이전 Claude 출력은 architect artifact contract를 위반했다.",
        "이 작업은 구현이 아니라 설계 문서 형식/본문 복구다. production 코드와 테스트 코드는 수정하지 않는다.",
        strictness.strip(),
        "## 실패 사유",
        *[f"- {err}" for err in (validation.get("errors") or ["artifact contract violation"])],
        "",
        "## 다시 작성 규칙",
        f"- 최종 응답의 첫 줄은 반드시 `{required_headings[0]}` 이어야 한다.",
        "- 출력은 설계 문서 본문 그 자체여야 한다. 무엇을 수정/복구/작성했다는 설명을 하지 않는다.",
        "- `설계 산출물을 작성 완료했습니다`, `설계 문서를 수정 완료했습니다`, `주요 변경점`, `파일에 작성했습니다`, `승인해 주세요` 같은 상태 보고/작업 보고 문장을 절대 출력하지 않는다.",
        "- artifact path나 manifest path를 본문에 언급하지 않는다.",
        "- 아래 필수 heading을 순서대로 모두 포함한다.",
        "- 각 heading 아래에는 실제 판단 근거와 handoff 내용을 작성한다.",
        "- heading 체크리스트/요약표로 대체하지 않는다.",
        "- 구현 코드는 작성하지 않고, 사용자 승인 전 구현을 시작하지 않았음을 명시한다.",
        "",
        "## 필수 heading 순서",
        *[f"- `{heading}`" for heading in required_headings],
        "",
        "## 원래 작업 prompt",
        "```text",
        original_prompt,
        "```",
        "",
        "## 실패한 이전 artifact",
        "```markdown",
        failed_artifact,
        "```",
        "",
        f"이제 첫 글자부터 `{required_headings[0]}`를 출력한다. 다른 서문, 완료보고, 변경점 설명, 코드펜스 없이 설계 문서 본문만 출력한다.",
    ])


def _write_manifest(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _maybe_repair_architect_artifact(
    *,
    root: Path,
    argv: list[str],
    prompt: str,
    artifact: Path,
    manifest: Path,
    plan_id: str,
    task_id: str,
    command_argv: list[str],
    output_format: str,
    readonly: bool,
    timeout: int,
    allowed_paths: list[str] | None,
    initial_validation: dict[str, Any],
) -> dict[str, Any]:
    required_headings = _extract_required_architect_headings(prompt)
    attempts: list[dict[str, Any]] = []
    current_validation = initial_validation
    for attempt in range(1, _MAX_ARCHITECT_ARTIFACT_REPAIR_ATTEMPTS + 1):
        failed_text = artifact.read_text(encoding="utf-8") if artifact.exists() else ""
        failed_path = artifact.with_name(f"{artifact.stem}.failed{attempt - 1}{artifact.suffix}")
        failed_path.parent.mkdir(parents=True, exist_ok=True)
        failed_path.write_text(failed_text, encoding="utf-8")
        repair_prompt = _build_architect_repair_prompt(
            original_prompt=prompt,
            failed_artifact=failed_text,
            validation=current_validation,
            required_headings=required_headings,
            attempt=attempt,
        )
        repair_prompt_path = root / ".soul" / "prompts" / "repair" / f"{plan_id}_{task_id}_architect_repair{attempt}.md"
        repair_prompt_path.parent.mkdir(parents=True, exist_ok=True)
        repair_prompt_path.write_text(repair_prompt, encoding="utf-8")
        repair_artifact = artifact.with_name(f"{artifact.stem}.repair{attempt}{artifact.suffix}")
        repair_manifest = manifest.with_name(f"{manifest.stem}_repair{attempt}{manifest.suffix}")
        repair_stdout = repair_manifest.with_suffix(".stdout.txt")
        repair_stderr = repair_manifest.with_suffix(".stderr.txt")
        started_at = _now_iso()
        completed, duration = _run_command(argv, repair_prompt, root, timeout)
        repair_artifact.parent.mkdir(parents=True, exist_ok=True)
        repair_artifact.write_text(completed.stdout or "", encoding="utf-8")
        repair_stdout.parent.mkdir(parents=True, exist_ok=True)
        repair_stdout.write_text(completed.stdout or "", encoding="utf-8")
        repair_stderr.write_text(completed.stderr or "", encoding="utf-8")
        repair_status = "completed" if completed.returncode == 0 else "failed"
        validation = _validate_architect_artifact_text(completed.stdout or "", required_headings) if completed.returncode == 0 else {
            "ok": False,
            "errors": [f"repair command exited {completed.returncode}"],
            "missing_headings": [],
            "empty_sections": [],
        }
        repair_data = {
            "version": 1,
            "runner": "pm_claude_delegation",
            "mode": "architect",
            "plan_id": str(plan_id or "").strip(),
            "task_id": str(task_id or "").strip(),
            "repair_attempt": attempt,
            "workdir": str(root),
            "command": command_argv,
            "prompt_file": _display_path(root, repair_prompt_path),
            "output_format": output_format,
            "artifact_path": _display_path(root, repair_artifact),
            "manifest_path": _display_path(root, repair_manifest),
            "status": repair_status,
            "exit_code": completed.returncode,
            "started_at": started_at,
            "completed_at": _now_iso(),
            "duration_seconds": round(duration, 3),
            "stdout_path": str(repair_stdout),
            "stderr_path": str(repair_stderr),
            "allowed_paths": list(allowed_paths or []),
            "readonly": bool(readonly),
            "artifact_contract_validation": validation,
        }
        _write_manifest(repair_manifest, repair_data)
        attempt_record = {
            "attempt": attempt,
            "failed_artifact_path": _display_path(root, failed_path),
            "repair_prompt_path": _display_path(root, repair_prompt_path),
            "repair_artifact_path": _display_path(root, repair_artifact),
            "repair_manifest_path": _display_path(root, repair_manifest),
            "status": repair_status,
            "exit_code": completed.returncode,
            "validation": validation,
        }
        attempts.append(attempt_record)
        if completed.returncode == 0 and validation.get("ok"):
            artifact.write_text(completed.stdout or "", encoding="utf-8")
            return {"ok": True, "attempts": attempts, "final_validation": validation, "final_stdout_path": str(repair_stdout), "final_stderr_path": str(repair_stderr)}
        current_validation = validation
    return {"ok": False, "attempts": attempts, "final_validation": current_validation}


def run_delegation(
    *,
    mode: str,
    plan_id: str,
    task_id: str,
    workdir: str,
    command: str,
    prompt_file: str,
    output_format: str,
    artifact_path: str,
    manifest_path: str,
    readonly: bool = False,
    timeout: int = 1800,
    allowed_paths: list[str] | None = None,
) -> dict[str, Any]:
    mode = str(mode or "").strip()
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}")
    output_format = str(output_format or "").strip()
    if output_format not in _VALID_FORMATS:
        raise ValueError(f"output_format must be one of {sorted(_VALID_FORMATS)}")
    root = Path(workdir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    prompt_path = _as_path(root, prompt_file)
    artifact = _as_path(root, artifact_path)
    manifest = _as_path(root, manifest_path)
    stdout_path = manifest.with_suffix(".stdout.txt")
    stderr_path = manifest.with_suffix(".stderr.txt")
    prompt = prompt_path.read_text(encoding="utf-8")
    argv = shlex.split(command)
    if not argv:
        raise ValueError("command is required")

    started_at = _now_iso()
    completed, duration = _run_command(argv, prompt, root, timeout)
    status = "completed" if completed.returncode == 0 else "failed"

    artifact.parent.mkdir(parents=True, exist_ok=True)
    if completed.stdout:
        artifact.write_text(completed.stdout, encoding="utf-8")
    elif not artifact.exists():
        artifact.write_text("", encoding="utf-8")

    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(completed.stdout or "", encoding="utf-8")
    stderr_path.write_text(completed.stderr or "", encoding="utf-8")

    repair_result: dict[str, Any] | None = None
    artifact_validation: dict[str, Any] | None = None
    if mode == "architect" and output_format == "design" and completed.returncode == 0:
        required_headings = _extract_required_architect_headings(prompt)
        artifact_validation = _validate_architect_artifact_text(completed.stdout or "", required_headings)
        if not artifact_validation.get("ok"):
            repair_result = _maybe_repair_architect_artifact(
                root=root,
                argv=argv,
                prompt=prompt,
                artifact=artifact,
                manifest=manifest,
                plan_id=str(plan_id or "").strip(),
                task_id=str(task_id or "").strip(),
                command_argv=argv,
                output_format=output_format,
                readonly=readonly,
                timeout=timeout,
                allowed_paths=allowed_paths,
                initial_validation=artifact_validation,
            )
            artifact_validation = repair_result.get("final_validation") if isinstance(repair_result, dict) else artifact_validation
            if repair_result.get("ok"):
                status = "completed"
                completed_at_exit_code = 0
                stdout_path = Path(str(repair_result.get("final_stdout_path") or stdout_path))
                stderr_path = Path(str(repair_result.get("final_stderr_path") or stderr_path))
            else:
                status = "artifact_contract_failed"
                completed_at_exit_code = 2
        else:
            completed_at_exit_code = completed.returncode
    else:
        completed_at_exit_code = completed.returncode

    data: dict[str, Any] = {
        "version": 1,
        "runner": "pm_claude_delegation",
        "mode": mode,
        "plan_id": str(plan_id or "").strip(),
        "task_id": str(task_id or "").strip(),
        "workdir": str(root),
        "command": argv,
        "prompt_file": _display_path(root, prompt_path),
        "output_format": output_format,
        "artifact_path": _display_path(root, artifact),
        "manifest_path": _display_path(root, manifest),
        "status": status,
        "exit_code": completed_at_exit_code,
        "started_at": started_at,
        "completed_at": _now_iso(),
        "duration_seconds": round(duration, 3),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "allowed_paths": list(allowed_paths or []),
        "readonly": bool(readonly),
    }
    if artifact_validation is not None:
        data["artifact_contract_validation"] = artifact_validation
    if repair_result is not None:
        data["artifact_repair"] = {
            "attempted": True,
            "max_attempts": _MAX_ARCHITECT_ARTIFACT_REPAIR_ATTEMPTS,
            "attempts": repair_result.get("attempts") or [],
            "ok": bool(repair_result.get("ok")),
        }
    _write_manifest(manifest, data)
    return {"ok": status in _SUCCESS_STATUSES, "manifest_path": str(manifest), "artifact_path": str(artifact), "manifest": data}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Claude Code delegation and write a manifest")
    sub = parser.add_subparsers(dest="command_name", required=True)
    run = sub.add_parser("run")
    run.add_argument("--mode", required=True, choices=sorted(_VALID_MODES))
    run.add_argument("--plan-id", required=True)
    run.add_argument("--task-id", required=True)
    run.add_argument("--workdir", required=True)
    run.add_argument("--command", required=True)
    run.add_argument("--prompt-file", required=True)
    run.add_argument("--output-format", required=True, choices=sorted(_VALID_FORMATS))
    run.add_argument("--artifact-path", required=True)
    run.add_argument("--manifest-path", required=True)
    run.add_argument("--readonly", action="store_true")
    run.add_argument("--timeout", type=int, default=1800)
    run.add_argument("--allowed-path", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command_name == "run":
        result = run_delegation(
            mode=args.mode,
            plan_id=args.plan_id,
            task_id=args.task_id,
            workdir=args.workdir,
            command=args.command,
            prompt_file=args.prompt_file,
            output_format=args.output_format,
            artifact_path=args.artifact_path,
            manifest_path=args.manifest_path,
            readonly=args.readonly,
            timeout=args.timeout,
            allowed_paths=args.allowed_path,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
        if manifest.get("status") == "artifact_contract_failed":
            # Controlled artifact-contract failure: let the Hermes worker inspect
            # the manifest/artifacts and block the Kanban task with evidence,
            # rather than having the bootstrap treat it as a transient process
            # failure and consume outer task retries.
            return 0
        return 0 if result.get("ok") else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
