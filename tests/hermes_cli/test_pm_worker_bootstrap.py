"""Tests for PM worker bootstrap runner autorun."""
from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import pm_worker_bootstrap as boot
from tools import pm_workflow_tool as pm


def _seed_ready_task(db_path, task_id: str = "t_runner") -> str:
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        created = kb.create_task(conn, title="architect", assignee="backend-specialist")
        conn.execute("UPDATE tasks SET id = ? WHERE id = ?", (task_id, created))
        conn.commit()
        return task_id


def _runner_manifest(tmp_path, *, status: str = "completed", exit_code: int = 0) -> str:
    artifact = tmp_path / ".soul" / "artifacts" / "design" / "plan_design.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("## 상태\n설계 완료.\n\n## 설계 요약\nGET /books 설계 정리.\n", encoding="utf-8")
    manifest = tmp_path / ".soul" / "artifacts" / "claude" / "plan_t_runner_architect_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "runner": "pm_claude_delegation",
        "mode": "architect",
        "plan_id": "plan",
        "task_id": "t_runner",
        "status": status,
        "exit_code": exit_code,
        "artifact_path": str(artifact),
        "manifest_path": str(manifest),
        "stdout_path": str(manifest.with_suffix(".stdout.txt")),
        "stderr_path": str(manifest.with_suffix(".stderr.txt")),
    }, ensure_ascii=False), encoding="utf-8")
    return str(manifest)


def _seed_design_plan(task_id: str) -> str:
    plan_id = "plan"
    pm._save_plans({
        "plans": {
            plan_id: {
                "plan_id": plan_id,
                "plan_kind": "design",
                "status": "design_in_progress",
                "design_status": "in_progress",
                "summary": "Design smoke",
                "request": "Design smoke request",
                "tasks": [{"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect"}],
                "created_design_tasks": [{"key": "T1", "task_id": task_id, "status": "ready", "assignee": "backend-specialist", "mode": "architect"}],
                "created_tasks": [{"key": "T1", "task_id": task_id, "status": "ready", "assignee": "backend-specialist", "mode": "architect"}],
                "created_followup_tasks": [],
                "updated_at": 0,
            }
        }
    }, profile="project_manager")
    return plan_id


def test_main_autocompletes_task_from_successful_runner_manifest_and_skips_exec(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    task_id = _seed_ready_task(db_path)
    manifest_path = _runner_manifest(tmp_path)

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0)

    def fake_execvpe(file, argv, env):
        raise AssertionError("worker process should not exec after successful runner finalization")

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_AUTORUN", "1")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_COMMAND", f"python3 -m tools.pm_claude_delegation run --manifest-path {manifest_path}")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_MANIFEST", manifest_path)
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr(pm, "validate_task_completion_gate", lambda conn, task_id, metadata=None, profile="project_manager": {"ok": True})
    monkeypatch.setattr(boot.subprocess, "run", fake_run)
    monkeypatch.setattr(boot.os, "execvpe", fake_execvpe)

    rc = boot.main(["--", "hermes", "-p", "backend-specialist", "chat", "-q", "work kanban task t_runner"])

    assert rc == 0
    with kb.connect(db_path=db_path) as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "done"
        assert ".soul/artifacts/design/plan_design.md" in (task.result or "")


def test_main_blocks_task_from_failed_runner_manifest_and_skips_exec(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    task_id = _seed_ready_task(db_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plan_id = _seed_design_plan(task_id)
    manifest_path = _runner_manifest(tmp_path, status="artifact_contract_failed", exit_code=2)

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0)

    def fake_execvpe(file, argv, env):
        raise AssertionError("worker process should not exec after deterministic runner block")

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_AUTORUN", "1")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_COMMAND", f"python3 -m tools.pm_claude_delegation run --manifest-path {manifest_path}")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_MANIFEST", manifest_path)
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr(boot.subprocess, "run", fake_run)
    monkeypatch.setattr(boot.os, "execvpe", fake_execvpe)

    rc = boot.main(["--", "hermes", "-p", "backend-specialist", "chat", "-q", "work kanban task t_runner"])

    assert rc == 0
    with kb.connect(db_path=db_path) as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert "artifact_contract_failed" in (kb.latest_summary(conn, task_id) or "")
    plan = pm._load_plans(profile="project_manager")["plans"][plan_id]
    assert plan["status"] == "design_in_progress"
    assert plan["design_status"] == "blocked"
    assert plan["created_design_tasks"][0]["status"] == "blocked"
    assert plan["created_tasks"][0]["status"] == "blocked"


def test_main_finalizes_failed_manifest_even_when_runner_exits_nonzero(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    task_id = _seed_ready_task(db_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plan_id = _seed_design_plan(task_id)
    manifest_path = _runner_manifest(tmp_path, status="failed", exit_code=124)

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=124)

    def fake_execvpe(file, argv, env):
        raise AssertionError("worker process should not exec after failed runner manifest")

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_AUTORUN", "1")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_COMMAND", f"python3 -m tools.pm_claude_delegation run --manifest-path {manifest_path}")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_MANIFEST", manifest_path)
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr(boot.subprocess, "run", fake_run)
    monkeypatch.setattr(boot.os, "execvpe", fake_execvpe)

    rc = boot.main(["--", "hermes", "-p", "backend-specialist", "chat", "-q", "work kanban task t_runner"])

    assert rc == 0
    with kb.connect(db_path=db_path) as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
    plan = pm._load_plans(profile="project_manager")["plans"][plan_id]
    assert plan["design_status"] == "blocked"


def test_main_runs_startup_runner_before_execing_worker_when_no_task_context(monkeypatch, tmp_path):
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
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
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


def test_main_blocks_task_when_runner_returns_zero_but_manifest_missing(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    task_id = _seed_ready_task(db_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plan_id = _seed_design_plan(task_id)
    manifest_path = tmp_path / ".soul" / "artifacts" / "claude" / "missing.json"

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0)

    def fake_execvpe(file, argv, env):
        raise AssertionError("worker process should not exec when manifest is missing after successful runner exit")

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_AUTORUN", "1")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_COMMAND", f"python3 -m tools.pm_claude_delegation run --manifest-path {manifest_path}")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_MANIFEST", str(manifest_path))
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr(boot.subprocess, "run", fake_run)
    monkeypatch.setattr(boot.os, "execvpe", fake_execvpe)

    rc = boot.main(["--", "hermes", "-p", "backend-specialist", "chat", "-q", "work kanban task t_runner"])

    assert rc == 0
    with kb.connect(db_path=db_path) as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert "manifest is missing" in (kb.latest_summary(conn, task_id) or "")
    plan = pm._load_plans(profile="project_manager")["plans"][plan_id]
    assert plan["design_status"] == "blocked"


def test_run_startup_runner_writes_bootstrap_debug_log(monkeypatch, tmp_path):
    log_path = tmp_path / ".soul" / "artifacts" / "claude" / "bootstrap.log"

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_AUTORUN", "1")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_COMMAND", "python3 -m tools.pm_claude_delegation run --help")
    monkeypatch.setenv("HERMES_PM_CLAUDE_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setenv("HERMES_PM_CLAUDE_BOOTSTRAP_LOG", str(log_path))
    monkeypatch.setattr(boot.subprocess, "run", fake_run)

    assert boot.run_startup_runner() == 0
    text = log_path.read_text(encoding="utf-8")
    assert "startup runner begin" in text
    assert "startup runner finished rc=0" in text
