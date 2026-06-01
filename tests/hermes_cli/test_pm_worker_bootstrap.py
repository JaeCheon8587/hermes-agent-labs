"""Tests for PM worker bootstrap runner autorun."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import pm_worker_bootstrap as boot


def _valid_architect_envelope() -> str:
    return """# Architect Task Envelope

이 파일은 Claude Code 실행 프롬프트가 아니다.

## Stage boundary
- architect/design-only 단계다.
- 사용자 설계 승인 전 구현을 시작하지 않는다.
- production 코드와 테스트 코드는 수정하지 않는다.

## Project constraints
- project_root: /tmp/workspace
- production_code_root: src
- design_artifact_path: .soul/artifacts/design/plan_t_design.md

## Original user request
도서 등록 API를 설계해줘.

## Architect task
- title: 도서 등록 API 설계
- key: T1

## Expected output classes
- 설계 요약
- 현재 코드/구조 관찰 결과
- 구현 작업분해
- 검증 계획
- 사용자 확인/승인 필요사항
"""


def test_main_runs_startup_runner_before_execing_worker(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(("runner", argv, kwargs))
        return SimpleNamespace(returncode=0)

    def fake_execvpe(file, argv, env):
        calls.append(("worker", file, argv, env))
        raise RuntimeError("exec called")

    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_AUTORUN", "1")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_COMMAND", "python3 -m tools.pm_claude_delegation run --help")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr(boot.subprocess, "run", fake_run)
    monkeypatch.setattr(boot.os, "execvpe", fake_execvpe)

    with pytest.raises(RuntimeError, match="exec called"):
        boot.main(["--", "hermes", "-p", "backend-specialist", "chat", "-q", "work kanban task t_1"])

    assert calls[0][0] == "runner"
    assert calls[0][1] == ["python3", "-m", "tools.pm_claude_delegation", "run", "--help"]
    assert calls[0][2]["cwd"] == str(tmp_path)
    assert calls[1][0] == "worker"
    assert calls[1][1] == "hermes"
    assert calls[1][2][:3] == ["hermes", "-p", "backend-specialist"]


def test_main_does_not_exec_worker_when_runner_fails(monkeypatch, tmp_path):
    exec_called = False
    recorded = []

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=7)

    def fake_execvpe(file, argv, env):
        nonlocal exec_called
        exec_called = True

    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_AUTORUN", "1")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_COMMAND", "python3 -m tools.pm_claude_delegation run --help")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr(boot.subprocess, "run", fake_run)
    monkeypatch.setattr(boot.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(boot, "_record_runner_failure", lambda message, returncode=None: recorded.append((message, returncode)))

    rc = boot.main(["--", "hermes", "chat", "-q", "work kanban task t_1"])

    assert rc == 7
    assert exec_called is False
    assert recorded == [("PM Claude runner exited with code 7", 7)]


def test_run_startup_runner_uses_timeout(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_AUTORUN", "1")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_COMMAND", "python3 -m tools.pm_claude_delegation run --help")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_TIMEOUT", "123")
    monkeypatch.setattr(boot.subprocess, "run", fake_run)

    assert boot.run_startup_runner() == 0
    assert captured["timeout"] == 123


def test_run_startup_runner_records_timeout_as_failure(monkeypatch, tmp_path):
    recorded = []

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, timeout=1)

    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_AUTORUN", "1")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_COMMAND", "python3 -m tools.pm_claude_delegation run --help")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr(boot.subprocess, "run", fake_run)
    monkeypatch.setattr(boot, "_record_runner_failure", lambda message, returncode=None: recorded.append((message, returncode)))

    assert boot.run_startup_runner() == 124
    assert "timed out" in recorded[0][0]


def test_architect_intake_gate_allows_complete_instruction(monkeypatch, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(_valid_architect_envelope(), encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_AUTORUN", "1")
    monkeypatch.setenv(
        "HERMES_PM_CLAUDE_RUNNER_COMMAND",
        f"python3 -m tools.pm_claude_delegation run --mode architect --prompt-file {prompt.name}",
    )
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr(boot.subprocess, "run", fake_run)

    assert boot.run_startup_runner() == 0
    assert calls


def test_architect_intake_gate_blocks_incomplete_instruction_before_runner(monkeypatch, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("# Architect Task Envelope\n\n## Original user request\n(empty)\n", encoding="utf-8")
    blocked = []

    def fake_run(argv, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("runner should not execute when intake gate blocks")

    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_AUTORUN", "1")
    monkeypatch.setenv(
        "HERMES_PM_CLAUDE_RUNNER_COMMAND",
        f"python3 -m tools.pm_claude_delegation run --mode architect --prompt-file {prompt.name}",
    )
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr(boot.subprocess, "run", fake_run)
    monkeypatch.setattr(boot, "_block_architect_instruction_incomplete", lambda result: blocked.append(result))

    assert boot.run_startup_runner() == boot._ARCHITECT_INTAKE_BLOCKED_RC
    assert blocked
    assert "## Stage boundary" in blocked[0]["missing"]
    assert "placeholder '(empty)' remains in architect instruction" in blocked[0]["empty"]


def test_main_exits_cleanly_when_architect_intake_gate_blocks(monkeypatch, tmp_path):
    exec_called = False

    def fake_execvpe(file, argv, env):
        nonlocal exec_called
        exec_called = True

    monkeypatch.setattr(boot, "run_startup_runner", lambda: boot._ARCHITECT_INTAKE_BLOCKED_RC)
    monkeypatch.setattr(boot.os, "execvpe", fake_execvpe)

    rc = boot.main(["--", "hermes", "chat", "-q", "work kanban task t_1"])

    assert rc == 0
    assert exec_called is False
