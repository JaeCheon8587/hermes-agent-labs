"""Tests for PM workflow preflight enforcement in pm_execute_plan."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tools import pm_workflow_tool as pm
from tools.pm_workflow_tool import (
    _attempt_preflight_repair,
    _run_worker_preflight,
    pm_create_plan,
    pm_execute_plan,
    _save_plans,
    _load_plans,
    _plans_path,
)


WORKER = "backend-specialist"


@pytest.fixture(autouse=True)
def _isolate_plans(tmp_path, monkeypatch):
    """Redirect plan storage to tmp_path so tests are isolated."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _make_plan(project_path="/tmp/proj") -> str:
    """Create a minimal design-only awaiting_approval plan and return its plan_id."""
    result = json.loads(pm_create_plan(
        request="build feature X",
        summary="Feature X design",
        plan_kind="design",
        tasks=[
            {"key": "T1", "title": "Research", "body": "research step", "assignee": WORKER, "mode": "architect"},
        ],
        project_path=project_path,
    ))
    assert result["ok"]
    return result["plan_id"]


# ---------------------------------------------------------------------------
# Preflight unit tests
# ---------------------------------------------------------------------------

class TestRunWorkerPreflight:
    def test_no_required_assignees_passes(self):
        """project_manager-only plans skip preflight."""
        result = _run_worker_preflight(["project_manager"])
        assert result["ok"] is True
        assert result["required"] == []

    @patch("tools.pm_workflow_tool._quick_chat_probe", return_value={"assignee": WORKER, "ok": True})
    @patch("tools.pm_workflow_tool._auth_snapshot", return_value={"assignee": WORKER, "auth_ok": True})
    @patch("tools.pm_workflow_tool._gateway_health_check", return_value={"ok": True, "pid": 123})
    def test_all_healthy(self, _gw, _auth, _chat):
        result = _run_worker_preflight([WORKER, "project_manager"])
        assert result["ok"] is True
        assert WORKER in result["results"]

    @patch("tools.pm_workflow_tool._quick_chat_probe", return_value={"assignee": WORKER, "ok": False, "reason": "gateway not running"})
    @patch("tools.pm_workflow_tool._auth_snapshot", return_value={"assignee": WORKER, "auth_ok": True})
    @patch("tools.pm_workflow_tool._gateway_health_check", return_value={"ok": False, "pid": None, "reason": "gateway not running"})
    def test_chat_probe_failure(self, _gw, _auth, _chat):
        result = _run_worker_preflight([WORKER])
        assert result["ok"] is False
        assert result["results"][WORKER]["ok"] is False

    @patch("tools.pm_workflow_tool._quick_chat_probe", return_value={"assignee": WORKER, "ok": True})
    @patch("tools.pm_workflow_tool._auth_snapshot", return_value={"assignee": WORKER, "auth_ok": False, "error": "no creds"})
    @patch("tools.pm_workflow_tool._gateway_health_check", return_value={"ok": True, "pid": 1})
    def test_auth_failure_does_not_block(self, _gw, _auth, _chat):
        """Auth snapshot is advisory; chat probe ok means overall ok."""
        result = _run_worker_preflight([WORKER])
        assert result["ok"] is True
        assert result["results"][WORKER]["auth_snapshot"]["auth_ok"] is False
        assert result["results"][WORKER]["ok"] is True


class TestAttemptPreflightRepair:
    def test_already_ok_is_noop(self):
        preflight = {"ok": True, "required": [], "results": {}, "gateway": {}}
        repaired = _attempt_preflight_repair(preflight)
        assert repaired["ok"] is True

    @patch("tools.pm_workflow_tool._run_worker_preflight")
    def test_repair_reruns_for_failed(self, mock_rerun):
        mock_rerun.return_value = {"ok": True, "required": [WORKER], "results": {WORKER: {"ok": True}}, "gateway": {"ok": True}}
        failed = {
            "ok": False,
            "required": [WORKER],
            "results": {WORKER: {"ok": False, "chat_probe": {"ok": False}}},
            "gateway": {"ok": False, "pid": None, "reason": "no pid"},
        }
        repaired = _attempt_preflight_repair(failed)
        assert repaired["ok"] is True
        mock_rerun.assert_called_once_with([WORKER])


# ---------------------------------------------------------------------------
# Integration: pm_execute_plan with preflight
# ---------------------------------------------------------------------------

class TestExecutePlanPreflightSuccess:
    """Preflight passes on first try -> cards are created."""

    @patch("tools.pm_workflow_tool._gateway_health_check", return_value={"ok": True, "pid": 99})
    @patch("tools.pm_workflow_tool._auth_snapshot", return_value={"assignee": WORKER, "auth_ok": True})
    @patch("tools.pm_workflow_tool._quick_chat_probe", return_value={"assignee": WORKER, "ok": True})
    @patch("tools.pm_workflow_tool.pm_create_kanban_workflow")
    def test_success_creates_cards(self, mock_kanban, _chat, _auth, _gw):
        mock_kanban.return_value = json.dumps({
            "ok": True,
            "created_tasks": [{"key": "T1", "task_id": "t_abc"}],
        })
        plan_id = _make_plan()
        result = json.loads(pm_execute_plan(plan_id))
        assert result["ok"] is True
        assert result["plan_status"] == "design_in_progress"
        mock_kanban.assert_called_once()

        # Verify preflight_result persisted on the plan.
        data = _load_plans()
        plan = data["plans"][plan_id]
        assert plan["preflight_result"]["ok"] is True


class TestExecutePlanExecutionKind:
    @patch("tools.pm_workflow_tool._gateway_health_check", return_value={"ok": True, "pid": 99})
    @patch("tools.pm_workflow_tool._auth_snapshot", return_value={"assignee": WORKER, "auth_ok": True})
    @patch("tools.pm_workflow_tool._quick_chat_probe", return_value={"assignee": WORKER, "ok": True})
    @patch("tools.pm_workflow_tool.pm_create_kanban_workflow")
    def test_execution_kind_starts_implementation_without_design_phase(self, mock_kanban, _chat, _auth, _gw, tmp_path):
        workspace = tmp_path / "execution-kind-workspace"
        workspace.mkdir(parents=True)
        result = json.loads(pm_create_plan(
            request="Implement approved order detail summary changes.",
            summary="execution-kind plan",
            plan_kind="execution",
            tasks=[
                {"key": "T1", "title": "implementer", "body": "implement approved design", "assignee": WORKER, "mode": "implementer"},
                {"key": "T2", "title": "reviewer", "body": "review approved design", "assignee": WORKER, "mode": "reviewer", "parents": ["T1"]},
                {"key": "T3", "title": "final", "body": "report to user", "assignee": "project_manager", "parents": ["T2"]},
            ],
            contract={
                "design_artifact": ".soul/artifacts/design/approved_order_detail_design.md",
                "implementation_paths": [
                    "src/Orders/OrderDetailService.cs",
                    "tests/Orders.Tests/OrderDetailServiceTests.cs",
                ],
            },
            project_path=str(workspace),
        ))
        assert result["ok"] is True
        plan_id = result["plan_id"]
        mock_kanban.return_value = json.dumps({
            "ok": True,
            "created_tasks": [
                {"key": "T1", "task_id": "t_impl", "parents": []},
                {"key": "T2", "task_id": "t_review", "parents": ["t_impl"]},
                {"key": "T3", "task_id": "t_final", "parents": ["t_review"]},
            ],
        })

        executed = json.loads(pm_execute_plan(plan_id))
        assert executed["ok"] is True
        assert executed["phase"] == "implementation"
        assert executed["plan_status"] == "executed"
        assert executed["mode"] == "execute_plan"
        assert executed["created_followup_tasks"][0]["task_id"] == "t_impl"
        mock_kanban.assert_called_once()

        data = _load_plans()
        plan = data["plans"][plan_id]
        assert plan["plan_kind"] == "execution"
        assert plan["status"] == "executed"
        assert plan["design_status"] == "approved"
        assert plan["approved_scope_export"]["ok"] is True
        assert plan["workflow_contract"]["ok"] is True


class TestExecutePlanRepairThenSuccess:
    """Preflight fails, repair succeeds -> cards are created."""

    @patch("tools.pm_workflow_tool.pm_create_kanban_workflow")
    @patch("tools.pm_workflow_tool._run_worker_preflight")
    @patch("tools.pm_workflow_tool._attempt_preflight_repair")
    def test_repair_then_cards(self, mock_repair, mock_preflight, mock_kanban):
        # First preflight fails.
        mock_preflight.return_value = {
            "ok": False,
            "required": [WORKER],
            "results": {WORKER: {"ok": False}},
            "gateway": {"ok": False},
        }
        # Repair succeeds.
        mock_repair.return_value = {
            "ok": True,
            "required": [WORKER],
            "results": {WORKER: {"ok": True}},
            "gateway": {"ok": True},
            "repair_actions": ["nudged gateway"],
        }
        mock_kanban.return_value = json.dumps({
            "ok": True,
            "created_tasks": [{"key": "T1", "task_id": "t_def"}],
        })

        plan_id = _make_plan()
        result = json.loads(pm_execute_plan(plan_id))
        assert result["ok"] is True
        assert result["plan_status"] == "design_in_progress"
        mock_kanban.assert_called_once()


class TestExecutePlanPreflightFailureNoCards:
    """Preflight fails even after repair -> no cards created, plan blocked."""

    @patch("tools.pm_workflow_tool._run_worker_preflight")
    @patch("tools.pm_workflow_tool._attempt_preflight_repair")
    def test_failure_blocks_plan(self, mock_repair, mock_preflight):
        mock_preflight.return_value = {
            "ok": False,
            "required": [WORKER],
            "results": {WORKER: {"ok": False, "chat_probe": {"ok": False, "reason": "gw down"}}},
            "gateway": {"ok": False},
        }
        mock_repair.return_value = {
            "ok": False,
            "required": [WORKER],
            "results": {WORKER: {"ok": False}},
            "gateway": {"ok": False},
            "repair_actions": ["no gateway pid found"],
        }
        plan_id = _make_plan()
        result = json.loads(pm_execute_plan(plan_id))
        assert result["ok"] is False
        assert result["status"] == "preflight_blocked"
        assert "preflight_result" in result

        # Verify plan status persisted.
        data = _load_plans()
        plan = data["plans"][plan_id]
        assert plan["status"] == "preflight_blocked"
        assert plan["preflight_result"]["ok"] is False

    @patch("tools.pm_workflow_tool._run_worker_preflight")
    @patch("tools.pm_workflow_tool._attempt_preflight_repair")
    @patch("tools.pm_workflow_tool.pm_create_kanban_workflow")
    def test_no_kanban_calls_on_failure(self, mock_kanban, mock_repair, mock_preflight):
        mock_preflight.return_value = {"ok": False, "required": [WORKER], "results": {WORKER: {"ok": False}}, "gateway": {"ok": False}}
        mock_repair.return_value = {"ok": False, "required": [WORKER], "results": {WORKER: {"ok": False}}, "gateway": {"ok": False}}

        plan_id = _make_plan()
        json.loads(pm_execute_plan(plan_id))
        mock_kanban.assert_not_called()

    @patch("tools.pm_workflow_tool._run_worker_preflight")
    @patch("tools.pm_workflow_tool._attempt_preflight_repair")
    def test_failure_clears_stale_followup_task_cache_for_execution_kind(self, mock_repair, mock_preflight, tmp_path):
        workspace = tmp_path / "execution-kind-stale-followups"
        workspace.mkdir(parents=True)
        plan = pm._ensure_plan_workflow_fields({
            "plan_id": "plan_execution_kind_stale_followups",
            "request": "execute approved design",
            "summary": "execution kind stale followups",
            "project_path": str(workspace),
            "plan_kind": "execution",
            "status": "approved",
            "design_status": "approved",
            "tasks": [
                {"key": "T1", "title": "implementer", "body": "implement", "assignee": WORKER, "mode": "implementer"},
                {"key": "T2", "title": "reviewer", "body": "review", "assignee": WORKER, "mode": "reviewer", "parents": ["T1"]},
                {"key": "T3", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T2"]},
            ],
            "created_followup_tasks": [{"key": "T1", "task_id": "stale_impl"}],
            "created_tasks": [{"key": "T1", "task_id": "stale_impl"}],
        })
        _save_plans({"plans": {plan["plan_id"]: plan}})
        mock_preflight.return_value = {"ok": False, "required": [WORKER], "results": {WORKER: {"ok": False}}, "gateway": {"ok": False}}
        mock_repair.return_value = {"ok": False, "required": [WORKER], "results": {WORKER: {"ok": False}}, "gateway": {"ok": False}}

        result = json.loads(pm_execute_plan(plan["plan_id"]))

        assert result["ok"] is False
        assert result["status"] == "preflight_blocked"
        saved = _load_plans()["plans"][plan["plan_id"]]
        assert saved["created_followup_tasks"] == []
        assert saved["created_tasks"] == []


class TestRetryFromPreflightBlocked:
    """A plan in preflight_blocked state can be retried."""

    @patch("tools.pm_workflow_tool._run_worker_preflight")
    @patch("tools.pm_workflow_tool._attempt_preflight_repair")
    @patch("tools.pm_workflow_tool.pm_create_kanban_workflow")
    def test_retry_succeeds(self, mock_kanban, mock_repair, mock_preflight):
        # First call: preflight fails.
        mock_preflight.return_value = {"ok": False, "required": [WORKER], "results": {WORKER: {"ok": False}}, "gateway": {"ok": False}}
        mock_repair.return_value = {"ok": False, "required": [WORKER], "results": {WORKER: {"ok": False}}, "gateway": {"ok": False}}

        plan_id = _make_plan()
        r1 = json.loads(pm_execute_plan(plan_id))
        assert r1["ok"] is False
        assert r1["status"] == "preflight_blocked"

        # Second call (retry): preflight passes.
        mock_preflight.return_value = {"ok": True, "required": [WORKER], "results": {WORKER: {"ok": True}}, "gateway": {"ok": True}}
        mock_kanban.return_value = json.dumps({"ok": True, "created_tasks": [{"key": "T1", "task_id": "t_retry"}]})

        r2 = json.loads(pm_execute_plan(plan_id))
        assert r2["ok"] is True
        assert r2["plan_status"] == "design_in_progress"
        mock_kanban.assert_called_once()

    @patch("tools.pm_workflow_tool._run_worker_preflight")
    @patch("tools.pm_workflow_tool.pm_create_kanban_workflow")
    def test_retry_infers_legacy_design_preflight_phase_when_execution_phase_missing(self, mock_kanban, mock_preflight, tmp_path):
        workspace = tmp_path / "legacy-preflight-blocked-workspace"
        workspace.mkdir(parents=True)
        plan = pm._ensure_plan_workflow_fields({
            "plan_id": "plan_legacy_design_preflight_block",
            "request": "legacy blocked design retry",
            "summary": "legacy blocked design retry",
            "project_path": str(workspace),
            "status": "preflight_blocked",
            "execution_phase": None,
            "last_execution_stage": "repair",
            "preflight_result": {"ok": False, "required": [WORKER]},
            "tasks": [
                {"key": "T1", "title": "Research", "body": "research step", "assignee": WORKER, "mode": "architect"},
                {"key": "T2", "title": "Synthesis", "body": "wrap up", "assignee": "project_manager", "parents": ["T1"]},
            ],
        })
        _save_plans({"plans": {plan["plan_id"]: plan}})

        mock_preflight.return_value = {"ok": True, "required": [WORKER], "results": {WORKER: {"ok": True}}, "gateway": {"ok": True}}
        mock_kanban.return_value = json.dumps({"ok": True, "created_tasks": [{"key": "T1", "task_id": "t_legacy_retry"}]})

        result = json.loads(pm_execute_plan(plan["plan_id"]))

        assert result["ok"] is True
        assert result["phase"] == "design"
        assert result["plan_status"] == "design_in_progress"
        mock_kanban.assert_called_once()


    @patch("tools.pm_workflow_tool.pm_create_kanban_workflow")
    def test_pm_execute_plan_rejects_implementation_phase_retry(self, mock_kanban, tmp_path):
        workspace = tmp_path / "wrong-transition-workspace"
        (workspace / ".soul").mkdir(parents=True)
        plan = pm._ensure_plan_workflow_fields({
            "plan_id": "plan_wrong_transition_preflight_retry",
            "request": "implement after approved design",
            "summary": "wrong transition retry",
            "project_path": str(workspace),
            "status": "preflight_blocked",
            "design_status": "awaiting_approval",
            "execution_phase": "implementation",
            "tasks": [
                {"key": "T1", "title": "architect", "body": "design", "assignee": WORKER, "mode": "architect"},
                {"key": "T2", "title": "implementer", "body": "implement", "assignee": WORKER, "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "body": "review", "assignee": WORKER, "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T3"]},
            ],
            "created_design_tasks": [{"key": "T1", "task_id": "t_design", "status": "done"}],
            "implementation_preflight_result": {"ok": False, "required": [WORKER]},
        })
        _save_plans({"plans": {plan["plan_id"]: plan}})

        result = json.loads(pm_execute_plan(plan["plan_id"]))

        assert result["ok"] is False
        assert result["error"] == "plan is blocked in implementation preflight; retry with pm_approve_design_and_execute"
        assert result["status"] == "preflight_blocked"
        assert result["execution_phase"] == "implementation"
        mock_kanban.assert_not_called()

    @patch("tools.pm_workflow_tool.pm_create_kanban_workflow")
    def test_pm_execute_plan_rejects_legacy_implementation_block_when_execution_phase_missing(self, mock_kanban, tmp_path):
        workspace = tmp_path / "legacy-wrong-transition-workspace"
        (workspace / ".soul").mkdir(parents=True)
        plan = pm._ensure_plan_workflow_fields({
            "plan_id": "plan_legacy_wrong_transition_preflight_retry",
            "request": "implement after approved design",
            "summary": "legacy wrong transition retry",
            "project_path": str(workspace),
            "status": "preflight_blocked",
            "design_status": "awaiting_approval",
            "execution_phase": None,
            "last_execution_stage": "implementation_repair",
            "tasks": [
                {"key": "T1", "title": "architect", "body": "design", "assignee": WORKER, "mode": "architect"},
                {"key": "T2", "title": "implementer", "body": "implement", "assignee": WORKER, "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "body": "review", "assignee": WORKER, "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T3"]},
            ],
            "created_design_tasks": [{"key": "T1", "task_id": "t_design", "status": "done"}],
            "preflight_result": {"ok": False, "required": [WORKER]},
            "repair_result": {"ok": False, "required": [WORKER], "repair_actions": ["legacy implementation repair"]},
        })
        _save_plans({"plans": {plan["plan_id"]: plan}})

        result = json.loads(pm_execute_plan(plan["plan_id"]))

        assert result["ok"] is False
        assert result["error"] == "plan is blocked in implementation preflight; retry with pm_approve_design_and_execute"
        assert result["status"] == "preflight_blocked"
        assert result["execution_phase"] == "implementation"
        mock_kanban.assert_not_called()


class TestApproveDesignRetryFromPreflightBlocked:
    """A design handoff blocked in implementation preflight can be retried."""

    @patch("tools.pm_workflow_tool._run_worker_preflight")
    @patch("tools.pm_workflow_tool._attempt_preflight_repair")
    @patch("tools.pm_workflow_tool.pm_create_kanban_workflow")
    def test_retry_succeeds(self, mock_kanban, mock_repair, mock_preflight, tmp_path):
        workspace = tmp_path / "approve-retry-workspace"
        (workspace / ".soul").mkdir(parents=True)
        plan = pm._ensure_plan_workflow_fields({
            "plan_id": "plan_retry_after_design_preflight_block",
            "request": "implement after design approval",
            "summary": "retry implementation handoff",
            "project_path": str(workspace),
            "status": "awaiting_design_approval",
            "design_status": "awaiting_approval",
            "tasks": [
                {"key": "T1", "title": "architect", "body": "design", "assignee": WORKER, "mode": "architect"},
                {"key": "T2", "title": "implementer", "body": "implement", "assignee": WORKER, "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "body": "review", "assignee": WORKER, "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T3"]},
            ],
            "created_design_tasks": [{"key": "T1", "task_id": "t_design", "status": "done"}],
            "design_artifact_path": ".soul/artifacts/design/approved_design.md",
            "contract": {"expected_deliverables": ["src/feature.py"]},
        })
        _save_plans({"plans": {plan["plan_id"]: plan}})

        mock_preflight.return_value = {"ok": False, "required": [WORKER], "results": {WORKER: {"ok": False}}, "gateway": {"ok": False}}
        mock_repair.return_value = {"ok": False, "required": [WORKER], "results": {WORKER: {"ok": False}}, "gateway": {"ok": False}}

        first = json.loads(pm.pm_approve_design_and_execute(plan["plan_id"]))
        assert first["ok"] is False
        assert first["status"] == "preflight_blocked"

        mock_preflight.return_value = {"ok": True, "required": [WORKER], "results": {WORKER: {"ok": True}}, "gateway": {"ok": True}}
        mock_kanban.return_value = json.dumps({
            "ok": True,
            "created_tasks": [
                {"key": "T2", "task_id": "t_impl", "parents": ["t_design"]},
                {"key": "T3", "task_id": "t_review", "parents": ["t_impl"]},
                {"key": "T4", "task_id": "t_final", "parents": ["t_review"]},
            ],
        })

        second = json.loads(pm.pm_approve_design_and_execute(plan["plan_id"]))
        assert second["ok"] is True
        assert second["plan_status"] == "executed"
        assert second["phase"] == "implementation"
        mock_kanban.assert_called_once()

    @patch("tools.pm_workflow_tool._run_worker_preflight")
    @patch("tools.pm_workflow_tool._attempt_preflight_repair")
    def test_preflight_block_clears_stale_followup_task_cache(self, mock_repair, mock_preflight, tmp_path):
        workspace = tmp_path / "approve-stale-followups"
        (workspace / ".soul").mkdir(parents=True)
        plan = pm._ensure_plan_workflow_fields({
            "plan_id": "plan_approve_stale_followups",
            "request": "implement after design approval",
            "summary": "stale followups should clear on block",
            "project_path": str(workspace),
            "status": "awaiting_design_approval",
            "design_status": "awaiting_approval",
            "tasks": [
                {"key": "T1", "title": "architect", "body": "design", "assignee": WORKER, "mode": "architect"},
                {"key": "T2", "title": "implementer", "body": "implement", "assignee": WORKER, "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "body": "review", "assignee": WORKER, "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T3"]},
            ],
            "created_design_tasks": [{"key": "T1", "task_id": "t_design", "status": "done"}],
            "created_followup_tasks": [{"key": "T2", "task_id": "stale_impl"}],
            "created_tasks": [{"key": "T2", "task_id": "stale_impl"}],
            "design_artifact_path": ".soul/artifacts/design/approved_design.md",
            "contract": {
                "expected_deliverables": ["src/Books/GetBooksEndpoint.cs"],
                "implementation_paths": ["src/Books/GetBooksEndpoint.cs"],
            },
        })
        _save_plans({"plans": {plan["plan_id"]: plan}})
        mock_preflight.return_value = {"ok": False, "required": [WORKER], "results": {WORKER: {"ok": False}}, "gateway": {"ok": False}}
        mock_repair.return_value = {"ok": False, "required": [WORKER], "results": {WORKER: {"ok": False}}, "gateway": {"ok": False}}

        result = json.loads(pm.pm_approve_design_and_execute(plan["plan_id"]))

        assert result["ok"] is False
        assert result["status"] == "preflight_blocked"
        saved = _load_plans()["plans"][plan["plan_id"]]
        assert saved["created_followup_tasks"] == []
        assert saved["created_tasks"] == []
