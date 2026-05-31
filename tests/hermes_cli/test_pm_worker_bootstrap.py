"""Tests for PM worker bootstrap runner autorun."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import pm_worker_bootstrap as boot


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
