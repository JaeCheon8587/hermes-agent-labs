from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tools import pm_workflow_tool as pm


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _pm_plans_path(home: Path) -> Path:
    return home / "profiles" / "project_manager" / "state" / "pm_plans.json"


def _write_pm_plan(home: Path, plan: dict) -> None:
    path = _pm_plans_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plans": {plan["plan_id"]: plan}}, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_pm_plan(home: Path, plan_id: str) -> dict:
    data = json.loads(_pm_plans_path(home).read_text(encoding="utf-8"))
    return data["plans"][plan_id]


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / ".soul").mkdir(parents=True, exist_ok=True)
    return root


def _base_plan(plan_id: str, project_path: str, tasks: list[dict], **extra) -> dict:
    if "contract" not in extra and any(str(task.get("mode") or "") in {"implementer", "reviewer"} for task in tasks):
        extra["contract"] = {
            "expected_deliverables": ["docs/specs/test-deliverable.md"],
            "implementation_paths": ["docs/specs/test-deliverable.md"],
        }
    plan = {
        "plan_id": plan_id,
        "request": "docs-only staged workflow test",
        "summary": "test plan",
        "project_path": project_path,
        "tasks": tasks,
        **extra,
    }
    return pm._ensure_plan_workflow_fields(plan)


def _write_claude_manifest(workspace: Path, plan_id: str, task_id: str, mode: str = "architect", artifact_text: str | None = None) -> Path:
    artifact_kind = "implementation" if mode == "implementer" else "design"
    artifact_rel = f".soul/artifacts/{artifact_kind}/{plan_id}_{mode}.md"
    artifact = workspace / artifact_rel
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if artifact_text is None and mode == "implementer":
        artifact_text = "\n".join([
            "## 구현 요약",
            "implemented",
            "## 변경 파일",
            "changed",
            "## 설계/승인 범위 일치 여부",
            "within scope",
            "## 검증 명령 및 결과",
            "tests passed",
            "## Reviewer Handoff",
            "review this",
            "## 남은 리스크",
            "none",
        ])
    artifact.write_text(artifact_text if artifact_text is not None else f"{mode} artifact", encoding="utf-8")
    manifest = workspace / ".soul" / "artifacts" / "claude" / f"{plan_id}_{task_id}_{mode}_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "version": 1,
        "runner": "pm_claude_delegation",
        "mode": mode,
        "plan_id": plan_id,
        "task_id": task_id,
        "workdir": str(workspace),
        "command": ["claude", "--print", mode],
        "prompt_file": f".soul/prompts/{task_id}_{mode}.md",
        "artifact_path": artifact_rel,
        "status": "completed",
        "exit_code": 0,
        "readonly": mode == "architect",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _claude_metadata(plan_id: str, mode: str = "architect") -> dict:
    return {"legacy_worker_claim_only": True, "plan_id": plan_id, "mode": mode}



def _complete_with_claude(conn, workspace: Path, plan_id: str, task_id: str, summary: str, mode: str = "architect") -> bool:
    _write_claude_manifest(workspace, plan_id, task_id, mode)
    return kb.complete_task(conn, task_id, summary=summary)

def test_dedicated_backend_worker_profiles_are_validated_by_mode():
    tasks, err = pm._normalize_tasks([
        {"key": "T1", "title": "design", "assignee": "backend-architect", "mode": "architect", "parents": []},
        {"key": "T2", "title": "implement", "assignee": "backend-implementer", "mode": "implementer", "parents": ["T1"]},
        {"key": "T3", "title": "review", "assignee": "backend-reviewer", "mode": "reviewer", "parents": ["T2"]},
        {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
    ])

    assert err is None
    assert [task["assignee"] for task in tasks] == [
        "backend-architect",
        "backend-implementer",
        "backend-reviewer",
        "project_manager",
    ]


def test_dedicated_backend_worker_profile_rejects_wrong_mode():
    tasks, err = pm._normalize_tasks([
        {"key": "T1", "title": "review", "assignee": "backend-reviewer", "mode": "implementer", "parents": []},
    ], require_pm_finalizer=False)

    assert tasks == []
    assert "backend-reviewer tasks must use mode" in err


def test_split_design_tasks_accepts_backend_architect_profile():
    design, followup = pm._split_design_tasks([
        {"key": "T1", "title": "design", "assignee": "backend-architect", "mode": "architect", "parents": []},
        {"key": "T2", "title": "implement", "assignee": "backend-implementer", "mode": "implementer", "parents": ["T1"]},
    ])

    assert [task["key"] for task in design] == ["T1"]
    assert [task["key"] for task in followup] == ["T2"]


def test_design_artifact_path_extraction_canonicalizes_project_scope(tmp_path):
    workspace = _workspace(tmp_path)
    artifact = workspace / ".soul" / "artifacts" / "design" / "plan_paths_design.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        """
- tests/Auth.Tests/LoginServiceTests.cs expects solution entries like Domain/Hermes.Domain.csproj.
- /tmp/not-this-project/src/External.csproj is outside this project.
- /mnt/c/Users/cross/OneDrive/Desktop/HermesTest/tests/Books.Tests/BookEndpointTests.cs is an absolute path example.
- appsettings/Program.cs is malformed prose and must not become a deliverable.

Expected files:
- src/Domain/Hermes.Domain.csproj
- src/Application/Hermes.Application.csproj
- src/Host/Program.cs
""".replace("/mnt/c/Users/cross/OneDrive/Desktop/HermesTest", str(workspace)),
        encoding="utf-8",
    )

    paths = pm._extract_design_artifact_concrete_paths(str(workspace), ".soul/artifacts/design/plan_paths_design.md")

    assert "src/Domain/Hermes.Domain.csproj" in paths
    assert "src/Application/Hermes.Application.csproj" in paths
    assert "src/Host/Program.cs" in paths
    assert "tests/Books.Tests/BookEndpointTests.cs" in paths
    assert "Domain/Hermes.Domain.csproj" not in paths
    assert "mnt/c/Users/cross/OneDrive/Desktop/HermesTest/tests/Books.Tests/BookEndpointTests.cs" not in paths
    assert "appsettings/Program.cs" not in paths


def test_workflow_contract_prefers_design_artifact_paths_over_stale_explicit_scope(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "src" / "Host" / "Extensions").mkdir(parents=True)
    (workspace / "tests" / "Host.Tests").mkdir(parents=True)
    artifact = workspace / ".soul" / "artifacts" / "design" / "plan_scope_design.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        """
## 영향 범위
- src/Host/Extensions/EndpointRouteBuilderExtensions.cs
- tests/Host.Tests/SmokeTests.cs
""".strip(),
        encoding="utf-8",
    )
    plan = _base_plan(
        "plan_scope",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-architect", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-implementer", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-reviewer", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
        status="executed",
        design_status="approved",
        design_approved_at=123,
        design_artifact_path=".soul/artifacts/design/plan_scope_design.md",
        contract={
            "design_artifact": ".soul/artifacts/design/plan_scope_design.md",
            "expected_deliverables": ["src/Books.Api/Program.cs"],
            "implementation_paths": ["src/Books.Api/Program.cs"],
        },
    )

    contract = pm._build_workflow_contract(plan)

    assert "src/Host/Extensions/EndpointRouteBuilderExtensions.cs" in contract["implementation_paths"]
    assert "tests/Host.Tests/SmokeTests.cs" in contract["implementation_paths"]
    assert "src/Books.Api/Program.cs" not in contract["implementation_paths"]
    assert "src/Books.Api/Program.cs" not in contract["required_evidence_by_phase"]["implementation"]
    assert "src/Host/Extensions/EndpointRouteBuilderExtensions.cs" in contract["phase_allowed_paths"]["implementer"]


def test_approved_scope_export_uses_reconciled_host_paths(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "src" / "Host" / "Extensions").mkdir(parents=True)
    artifact = workspace / ".soul" / "artifacts" / "design" / "plan_scope_export_design.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("- src/Host/Extensions/EndpointRouteBuilderExtensions.cs", encoding="utf-8")
    plan = _base_plan(
        "plan_scope_export",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-architect", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-implementer", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-reviewer", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
        status="executed",
        design_status="approved",
        design_approved_at=123,
        design_artifact_path=".soul/artifacts/design/plan_scope_export_design.md",
        contract={
            "design_artifact": ".soul/artifacts/design/plan_scope_export_design.md",
            "expected_deliverables": ["src/Books.Api/Program.cs"],
            "implementation_paths": ["src/Books.Api/Program.cs"],
        },
    )

    export = pm._export_approved_scope(plan, "implementer", "t_impl", approved_by="user", approved_at=123)

    assert export["ok"] is True
    scope = export["scope"]
    assert "src/Host/Extensions/EndpointRouteBuilderExtensions.cs" in scope["scope"]["allowed_paths"]
    assert "src/Books.Api/Program.cs" not in scope["scope"]["allowed_paths"]
    assert "src/Books.Api/Program.cs" not in scope["evidence"]["required"]
    assert "src/Books.Api/Program.cs" not in scope["design_spec"]["expected_deliverables"]
    assert scope["approval"]["approved_at"][11:19] == "09:02:03"
    assert scope["approval"]["design_approved_at"][11:19] == "09:02:03"
    assert scope["scope_sync"]["phase"] == "implementer"
    assert scope["scope_sync"]["task_id"] == "t_impl"
    assert scope["scope_sync"]["synced_at"]


def test_approved_scope_final_sync_does_not_overwrite_design_approval_time(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "src").mkdir(parents=True)
    plan = _base_plan(
        "plan_scope_timestamp",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-architect", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-implementer", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-reviewer", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
        status="executed",
        design_status="approved",
        design_approved_at=123,
        design_approved_by="designer",
        contract={"expected_deliverables": ["src/Smoke.cs"], "implementation_paths": ["src/Smoke.cs"]},
    )

    export = pm._export_approved_scope(plan, "final", "t_final", approved_by="final-sync", approved_at=999)

    assert export["ok"] is True
    scope = export["scope"]
    assert scope["approval"]["approved_by"] == "final-sync"
    assert scope["approval"]["approved_at"][11:19] == "09:02:03"
    assert scope["approval"]["design_approved_at"][11:19] == "09:02:03"
    assert not scope["approval"]["approved_at"].startswith("1970-01-01T00:16:39")
    assert scope["scope_sync"]["phase"] == "final"
    assert scope["scope_sync"]["task_id"] == "t_final"
    assert scope["scope_sync"]["synced_at"]


def test_claude_delegation_contract_applies_only_to_architect_and_implementer(tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_claude_contract",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
    )

    contract = pm._build_workflow_contract(plan)

    assert contract["claude_delegation"]["architect"]["required"] is True
    assert contract["claude_delegation"]["architect"]["mode"] == "read_only"
    assert contract["claude_delegation"]["implementer"]["required"] is True
    assert contract["claude_delegation"]["implementer"]["mode"] == "write_allowed"
    assert contract["claude_delegation"]["reviewer"]["required"] is False
    assert contract["claude_delegation"]["final"]["required"] is False


def test_architect_and_implementer_task_bodies_require_claude_delegation(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_kanban_workflow(
        request="설계와 구현은 Claude Code 위임을 강제하고 리뷰는 독립 검증한다",
        project_path=str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
    ))

    assert result["ok"] is True
    created = {row["key"]: row["task_id"] for row in result["created_tasks"]}
    with kb.connect() as conn:
        architect_body = kb.get_task(conn, created["T1"]).body
        implementer_body = kb.get_task(conn, created["T2"]).body
        reviewer_body = kb.get_task(conn, created["T3"]).body

    assert "[Python Claude runner 강제]" in architect_body
    assert "read-only" in architect_body
    assert "tools.pm_claude_delegation" in architect_body
    assert "[Python Claude runner 강제]" in implementer_body
    assert "write-allowed" in implementer_body
    assert "tools.pm_claude_delegation" in implementer_body
    assert "[Python Claude runner 강제]" not in reviewer_body


def test_completion_gate_rejects_worker_claim_without_runner_manifest(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_manifest_required",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            ],
            status="design_in_progress",
            design_status="in_progress",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "ready"}],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)

        with pytest.raises(kb.WorkflowCompletionBlockedError, match="Python Claude runner manifest is required"):
            kb.complete_task(conn, architect, summary="I used Claude", metadata={"claude_delegation": {"used": True, "command": "claude", "session_id": "fake", "output_artifact": "fake", "status": "completed"}})

        assert kb.get_task(conn, architect).status == "ready"


def test_completion_gate_accepts_python_runner_manifest(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_manifest_accept",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            ],
            status="design_in_progress",
            design_status="in_progress",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "ready"}],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)
        _write_claude_manifest(workspace, "plan_manifest_accept", architect, "architect")

        assert kb.complete_task(conn, architect, summary="runner completed")


def test_architect_completion_without_claude_delegation_manifest_is_blocked(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_claude_gate_arch",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            ],
            status="design_in_progress",
            design_status="in_progress",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "ready"}],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)

        with pytest.raises(kb.WorkflowCompletionBlockedError, match="Python Claude runner manifest is required"):
            kb.complete_task(conn, architect, summary="design done")

        task = kb.get_task(conn, architect)
        events = kb.list_events(conn, architect)

    assert task.status == "ready"
    assert any(event.kind == "completion_blocked_pm_workflow_contract" for event in events)


def test_implementer_completion_accepts_claude_delegation_manifest_and_reviewer_does_not_require_it(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
        implementer = kb.create_task(conn, title="implementer", assignee="backend-specialist", parents=[architect], workspace_kind="dir", workspace_path=str(workspace))
        reviewer = kb.create_task(conn, title="reviewer", assignee="backend-specialist", parents=[implementer], workspace_kind="dir", workspace_path=str(workspace))
        final = kb.create_task(conn, title="final", assignee="project_manager", parents=[reviewer], workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_claude_gate_impl",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
                {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
            ],
            status="executed",
            design_status="approved",
            design_approved_at=123,
            design_approved_by="user",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
            created_followup_tasks=[
                {"key": "T2", "task_id": implementer, "status": "ready", "parents": [architect]},
                {"key": "T3", "task_id": reviewer, "status": "todo", "parents": [implementer]},
                {"key": "T4", "task_id": final, "status": "todo", "parents": [reviewer]},
            ],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)

        with pytest.raises(kb.WorkflowCompletionBlockedError, match="Python Claude runner manifest is required"):
            kb.complete_task(conn, implementer, summary="implemented")

        _write_claude_manifest(workspace, "plan_claude_gate_impl", implementer, "implementer")
        assert kb.complete_task(
            conn,
            implementer,
            summary="implemented with Python runner manifest",
        )
        assert kb.complete_task(
            conn,
            reviewer,
            summary="reviewed without Claude delegation manifest",
            metadata={
                "verdict": "pass",
                "summary": "Reviewer stayed independent.",
                "changed_files": ["src/Books/BooksController.cs"],
                "scope_status": "within_allowed_paths",
                "validated_artifacts": ["src/Books/BooksController.cs"],
                "acceptance_checks": [{"criterion": "review", "status": "pass"}],
                "unmet_requirements": [],
                "design_alignment_summary": "Matches design.",
            },
        )


def test_implementer_completion_rejects_incomplete_handoff_artifact_headings(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
        implementer = kb.create_task(conn, title="implementer", assignee="backend-specialist", parents=[architect], workspace_kind="dir", workspace_path=str(workspace))
        reviewer = kb.create_task(conn, title="reviewer", assignee="backend-specialist", parents=[implementer], workspace_kind="dir", workspace_path=str(workspace))
        final = kb.create_task(conn, title="final", assignee="project_manager", parents=[reviewer], workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_impl_heading_gate",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
                {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
            ],
            status="executed",
            design_status="approved",
            design_approved_at=123,
            design_approved_by="user",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
            created_followup_tasks=[
                {"key": "T2", "task_id": implementer, "status": "ready", "parents": [architect]},
                {"key": "T3", "task_id": reviewer, "status": "todo", "parents": [implementer]},
                {"key": "T4", "task_id": final, "status": "todo", "parents": [reviewer]},
            ],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)
        _write_claude_manifest(
            workspace,
            "plan_impl_heading_gate",
            implementer,
            "implementer",
            artifact_text="## 구현 요약\n구현함\n## 변경 파일\n- src/Foo.cs",
        )

        with pytest.raises(kb.WorkflowCompletionBlockedError, match="artifact is missing required handoff headings"):
            kb.complete_task(conn, implementer, summary="implemented with incomplete handoff")

        task = kb.get_task(conn, implementer)
        events = kb.list_events(conn, implementer)

    assert task.status == "ready"
    assert any(event.kind == "completion_blocked_pm_workflow_contract" for event in events)



def test_reviewer_completion_blocks_pass_when_design_validation_coverage_is_missing(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
        implementer = kb.create_task(conn, title="implementer", assignee="backend-specialist", parents=[architect], workspace_kind="dir", workspace_path=str(workspace))
        reviewer = kb.create_task(conn, title="reviewer", assignee="backend-specialist", parents=[implementer], workspace_kind="dir", workspace_path=str(workspace))
        final = kb.create_task(conn, title="final", assignee="project_manager", parents=[reviewer], workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_design_coverage_gate",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
                {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
            ],
            status="executed",
            design_status="approved",
            design_approved_at=123,
            design_approved_by="user",
            acceptance_criteria=[
                "POST /books 정상 등록 201 Created",
                "POST 후 GET /books 목록에 신규 도서 포함",
            ],
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
            created_followup_tasks=[
                {"key": "T2", "task_id": implementer, "status": "ready", "parents": [architect]},
                {"key": "T3", "task_id": reviewer, "status": "todo", "parents": [implementer]},
                {"key": "T4", "task_id": final, "status": "todo", "parents": [reviewer]},
            ],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)
        _write_claude_manifest(workspace, "plan_design_coverage_gate", implementer, "implementer")
        assert kb.complete_task(conn, implementer, summary="implemented")

        with pytest.raises(kb.WorkflowCompletionBlockedError, match="design validation coverage missing"):
            kb.complete_task(
                conn,
                reviewer,
                summary="review pass with incomplete coverage",
                metadata={
                    "verdict": "pass",
                    "summary": "Reviewer checked only POST response.",
                    "changed_files": ["src/Books/BooksController.cs"],
                    "scope_status": "within_allowed_paths",
                    "validated_artifacts": ["src/Books/BooksController.cs"],
                    "acceptance_checks": [
                        {"criterion": "POST /books 정상 등록 201 Created", "status": "pass"},
                    ],
                    "unmet_requirements": [],
                    "design_alignment_summary": "Mostly matches design.",
                },
            )

        task = kb.get_task(conn, reviewer)
        events = kb.list_events(conn, reviewer)

    assert task.status == "ready"
    assert any(event.kind == "completion_blocked_pm_workflow_contract" for event in events)


def test_reviewer_design_validation_coverage_allows_concern_with_unmet_checks():
    plan = {"acceptance_criteria": ["POST 후 GET /books 목록에 신규 도서 포함"]}
    reviewer_meta = {
        "verdict": "concern",
        "scope_status": "within_allowed_paths",
        "acceptance_checks": [],
        "unmet_requirements": ["POST 후 GET /books 목록에 신규 도서 포함"],
    }

    assert pm._validate_design_validation_coverage(plan, reviewer_meta) == []


def test_pm_execute_plan_injects_exact_runner_invocation_and_prompt_file(hermes_home, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(pm, "_run_worker_preflight", lambda assignees: {"ok": True, "checked": list(assignees)})

    created = json.loads(pm.pm_create_plan(
        request="설계는 Python runner가 Claude Code에 위임한다",
        summary="runner injection test",
        project_path=str(workspace),
        plan_kind="design",
        tasks=[
            {"key": "T1", "title": "설계", "assignee": "backend-specialist", "mode": "architect", "parents": []},
        ],
    ))
    assert created["ok"] is True
    plan_id = created["plan_id"]

    executed = json.loads(pm.pm_execute_plan(plan_id, approved_by="user"))
    assert executed["ok"] is True
    task_id = executed["created_design_tasks"][0]["task_id"]

    prompt = workspace / ".soul" / "prompts" / f"{plan_id}_{task_id}_architect.md"
    context = workspace / ".soul" / "prompts" / "internal" / f"{plan_id}_{task_id}_architect_context.md"
    manifest = workspace / ".soul" / "artifacts" / "claude" / f"{plan_id}_{task_id}_architect_manifest.json"
    artifact = workspace / ".soul" / "artifacts" / "design" / f"{plan_id}_design.md"
    assert prompt.is_file()
    assert context.is_file()

    prompt_text = prompt.read_text(encoding="utf-8")
    assert "# Claude Code Architect Delegation Prompt" in prompt_text
    for heading in pm._COMMON_CLAUDE_PROMPT_SECTIONS:
        assert heading in prompt_text
    assert "read-only architect 단계" in prompt_text
    assert "설계는 Python runner가 Claude Code에 위임한다" in prompt_text
    assert "최종 응답 자체가 완전한 markdown 설계 문서" in prompt_text
    assert "상태 보고만 출력하지 말고" in prompt_text
    assert "heading 체크리스트/요약표로 대체하지 않는다" in prompt_text
    assert "## 설계 요약" in prompt_text
    assert str(manifest.relative_to(workspace)) not in prompt_text
    assert f"plan_id: {plan_id}" not in prompt_text

    context_text = context.read_text(encoding="utf-8")
    assert "설계는 Python runner가 Claude Code에 위임한다" in context_text
    assert f"- plan_id: {plan_id}" in context_text
    assert f"- task_id: {task_id}" in context_text
    assert str(manifest.relative_to(workspace)) in context_text

    with kb.connect() as conn:
        body = kb.get_task(conn, task_id).body
    assert "[Python Claude runner 강제]" in body
    assert "[Python Claude runner 실행 명령]" in body
    assert f"--plan-id {plan_id}" in body
    assert f"--task-id {task_id}" in body
    assert f"--prompt-file .soul/prompts/{plan_id}_{task_id}_architect.md" in body
    assert f"--artifact-path .soul/artifacts/design/{plan_id}_design.md" in body
    assert f"--manifest-path .soul/artifacts/claude/{plan_id}_{task_id}_architect_manifest.json" in body
    assert "--readonly" in body
    assert "python3 -m tools.pm_claude_delegation run" in body


def test_write_claude_runner_prompt_builds_implementer_prompt_and_internal_context(tmp_path):
    workspace = _workspace(tmp_path)
    result = pm._write_claude_runner_prompt(
        project_path=str(workspace),
        plan_id="plan_impl",
        task_id="t_impl",
        mode="implementer",
        request="승인된 설계 기준으로 구현해",
        task={
            "key": "T2",
            "title": "GET /books 구현",
            "body": "승인된 scope 안에서 GET /books를 구현하고 테스트 결과를 남긴다.",
        },
        parent_ids=["t_arch"],
    )

    assert result["ok"] is True
    prompt = Path(result["prompt_path"])
    context = Path(result["context_path"])
    assert prompt.is_file()
    assert context.is_file()

    prompt_text = prompt.read_text(encoding="utf-8")
    assert "# Claude Code Implementer Delegation Prompt" in prompt_text
    for heading in pm._COMMON_CLAUDE_PROMPT_SECTIONS:
        assert heading in prompt_text
    assert "## 구현 요약" in prompt_text
    assert "## Reviewer Handoff" in prompt_text
    assert "최종 응답 자체가 완전한 markdown 구현 handoff" in prompt_text
    assert "상태 보고만 출력하지 말고" in prompt_text
    assert "reviewer/human 검증 대기만으로 blocked 처리하지 않는다" in prompt_text
    assert "plan_id: plan_impl" not in prompt_text

    context_text = context.read_text(encoding="utf-8")
    assert "승인된 설계 기준으로 구현해" in context_text
    assert "- plan_id: plan_impl" in context_text
    assert "- task_id: t_impl" in context_text
    assert "--prompt-file .soul/prompts/plan_impl_t_impl_implementer.md" in context_text


def test_architect_prompt_adds_conditional_api_sections_without_forcing_them_on_simple_work(tmp_path):
    workspace = _workspace(tmp_path)
    api_result = pm._write_claude_runner_prompt(
        project_path=str(workspace),
        plan_id="plan_api",
        task_id="t_api",
        mode="architect",
        request="GET /books API 설계. 제목/저자/availability 응답, 빈 목록 200 + [], 오류 정책 포함.",
        task={"key": "T1", "title": "GET /books API 설계", "body": "src Host/Application/Infrastructure 구조를 보고 설계만 한다."},
        parent_ids=[],
    )
    api_prompt = Path(api_result["prompt_path"]).read_text(encoding="utf-8")

    assert "[인터페이스 계약]" in api_prompt
    assert "[엣지 케이스]" in api_prompt
    assert "[아키텍처 컨텍스트]" in api_prompt
    assert "[대안 비교]" in api_prompt
    assert "[사용자 확인 필요사항]" in api_prompt
    assert "`## API / DTO 계약`" in api_prompt
    assert "`## 검토한 대안`" in api_prompt

    simple_result = pm._write_claude_runner_prompt(
        project_path=str(workspace),
        plan_id="plan_simple",
        task_id="t_simple",
        mode="architect",
        request="README 문구 개선 방향만 설계해.",
        task={"key": "T1", "title": "문서 문구 개선 설계", "body": "문서 표현 개선 방향만 정리한다."},
        parent_ids=[],
    )
    simple_prompt = Path(simple_result["prompt_path"]).read_text(encoding="utf-8")

    assert "[인터페이스 계약]" not in simple_prompt
    assert "[엣지 케이스]" not in simple_prompt
    assert "[아키텍처 컨텍스트]" not in simple_prompt
    assert "`## API / DTO 계약`" not in simple_prompt
    assert "[대안 비교]" in simple_prompt


def test_implementer_prompt_adds_api_contract_and_detailed_handoff_requirements(tmp_path):
    workspace = _workspace(tmp_path)
    result = pm._write_claude_runner_prompt(
        project_path=str(workspace),
        plan_id="plan_impl_api",
        task_id="t_impl_api",
        mode="implementer",
        request="GET /books API 구현. title/author/availability와 빈 목록 200 + [] 검증.",
        task={"key": "T2", "title": "GET /books 구현", "body": "src Host handler와 tests를 승인 scope 안에서 수정한다."},
        parent_ids=["t_arch"],
    )
    prompt_text = Path(result["prompt_path"]).read_text(encoding="utf-8")

    assert "[인터페이스 계약]" in prompt_text
    assert "[엣지 케이스]" in prompt_text
    assert "[아키텍처 컨텍스트]" in prompt_text
    assert "파일별 변경 이유" in prompt_text
    assert "exit code 또는 pass/fail" in prompt_text
    assert "reviewer가 확인해야 할 설계 기준" in prompt_text
    assert "[대안 비교]" not in prompt_text


def test_claude_runner_prompt_uses_configured_command_in_internal_context(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("HERMES_PM_CLAUDE_COMMAND", "claude --print --max-turns 2")

    result = pm._write_claude_runner_prompt(
        project_path=str(workspace),
        plan_id="plan_cmd",
        task_id="t_cmd",
        mode="architect",
        request="설계만 해",
        task={"key": "T1", "title": "명령 설정 테스트", "body": "Claude command env를 반영한다."},
        parent_ids=[],
    )

    context_text = Path(result["context_path"]).read_text(encoding="utf-8")
    assert "--command 'claude --print --max-turns 2'" in context_text
    assert "--readonly" in context_text


def test_dedicated_worker_profiles_receive_claude_runner_body_sections(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_kanban_workflow(
        request="dedicated worker routing runner prompt test",
        project_path=str(workspace),
        plan_id="plan_dedicated",
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-architect", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-implementer", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-reviewer", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
    ))

    assert result["ok"] is True
    created = {row["key"]: row["task_id"] for row in result["created_tasks"]}
    with kb.connect() as conn:
        architect_body = kb.get_task(conn, created["T1"]).body
        implementer_body = kb.get_task(conn, created["T2"]).body
        reviewer_body = kb.get_task(conn, created["T3"]).body

    assert "[Python Claude runner 강제]" in architect_body
    assert "--prompt-file .soul/prompts/" in architect_body
    assert "--readonly" in architect_body
    assert "[Python Claude runner 강제]" in implementer_body
    assert "--prompt-file .soul/prompts/" in implementer_body
    assert "--readonly" not in implementer_body
    assert "[Python Claude runner 강제]" not in reviewer_body

def test_dedicated_worker_preflight_uses_chat_without_gateway_restart(monkeypatch):
    monkeypatch.setattr(pm, "_auth_snapshot", lambda assignee: {"assignee": assignee, "auth_ok": True})
    monkeypatch.setattr(pm, "_quick_chat_probe", lambda assignee: {"assignee": assignee, "ok": True, "stdout": "OK"})

    def fail_gateway(_assignee):
        raise AssertionError("dedicated worker preflight must not restart/check gateway")

    monkeypatch.setattr(pm, "_gateway_health_check", fail_gateway)

    result = pm._run_worker_preflight(["backend-architect", "backend-implementer", "backend-reviewer"])

    assert result["ok"] is True
    assert sorted(result["required"]) == ["backend-architect", "backend-implementer", "backend-reviewer"]
    assert all(item["gateway"]["attempted"] is False for item in result["results"].values())


def test_architect_completion_marks_plan_awaiting_design_approval(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_arch",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            ],
            status="design_in_progress",
            design_status="in_progress",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "ready"}],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)

        assert _complete_with_claude(conn, workspace, "plan_arch", architect, "design artifact ready")

    saved = _read_pm_plan(hermes_home, "plan_arch")
    assert saved["status"] == "awaiting_design_approval"
    assert saved["design_status"] == "awaiting_approval"
    assert saved["design_summary"] == "design artifact ready"
    assert saved["design_ready_at"]
    assert saved["created_design_tasks"][0]["status"] == "done"


def test_design_revision_reopens_completed_architect_task(hermes_home, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_revision_reopen",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            ],
            status="design_in_progress",
            design_status="in_progress",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "ready"}],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)

        assert _complete_with_claude(conn, workspace, "plan_revision_reopen", architect, "initial design artifact")

    monkeypatch.setenv("HERMES_HOME", str(hermes_home / "profiles" / "project_manager"))
    revision = json.loads(pm.pm_request_design_revision("plan_revision_reopen", revision_notes="API shape must change"))
    assert revision["ok"] is True
    assert revision["design_task_recovery"]["updates"][0]["reopened"] is True
    assert revision["design_task_recovery"]["updates"][0]["current_status"] == "ready"

    saved = pm._load_plans()["plans"]["plan_revision_reopen"]
    assert saved["status"] == "design_revision_requested"
    assert saved["created_design_tasks"][0]["status"] == "ready"

    with kb.connect() as conn:
        assert kb.get_task(conn, architect).status == "ready"
        comments = kb.list_comments(conn, architect)
        assert any("API shape must change" in comment.body for comment in comments)
        assert _complete_with_claude(conn, workspace, "plan_revision_reopen", architect, "revised design artifact")

    revised = pm._load_plans()["plans"]["plan_revision_reopen"]
    assert revised["status"] == "awaiting_design_approval"
    assert revised["design_status"] == "awaiting_approval"
    assert revised["design_summary"] == "revised design artifact"
    assert revised["created_design_tasks"][0]["status"] == "done"


def test_design_revision_state_rejects_reexecution_of_design_phase(hermes_home, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_revision_no_reexecute",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            ],
            status="design_revision_requested",
            design_status="revision_requested",
            execution_phase="design",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "ready"}],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)

    monkeypatch.setenv("HERMES_HOME", str(hermes_home / "profiles" / "project_manager"))
    result = json.loads(pm.pm_execute_plan("plan_revision_no_reexecute"))
    assert result["ok"] is False
    assert result["status"] == "design_revision_requested"
    assert result["error"] == "design revision is already in progress; reuse the existing architect tasks and wait for pm_mark_design_ready instead of re-executing the design phase"
    assert result["created_design_tasks"] == [{"key": "T1", "task_id": architect, "status": "ready"}]

    saved = pm._load_plans()["plans"]["plan_revision_no_reexecute"]
    assert saved["status"] == "design_revision_requested"
    assert saved["created_design_tasks"] == [{"key": "T1", "task_id": architect, "status": "ready"}]


def test_mode_guardrail_text_tells_implementer_to_complete_instead_of_blocking_for_review():
    text = pm._mode_guardrail_text({"mode": "implementer"})
    assert "implementer 모드" in text
    assert "이 카드는 완료 처리" in text
    assert "다음 게이트" in text
    assert "블로커가 아니라" in text


def test_design_approval_auto_completes_pm_gate_and_promotes_implementer(hermes_home, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(pm, "_run_worker_preflight", lambda assignees: {"ok": True, "checked": list(assignees)})
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
    plan = _base_plan(
        "plan_design_gate_auto_complete",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "설계 승인 게이트 확인 및 구현 진행 관리", "assignee": "project_manager", "parents": ["T1"]},
            {"key": "T3", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T2"]},
            {"key": "T4", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T3"]},
            {"key": "T5", "title": "final", "assignee": "project_manager", "parents": ["T4"]},
        ],
        contract={"expected_deliverables": ["algorithms/top_k_frequent.py"]},
        status="awaiting_design_approval",
        design_status="awaiting_approval",
        created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
        design_artifact_path=".soul/artifacts/design/plan_design_gate_auto_complete_design.md",
    )
    contract = pm._write_workflow_contract(plan)
    plan["workflow_contract"] = {"path": contract["path"], "ok": True}
    pm._save_plans({"plans": {plan["plan_id"]: plan}})

    result = json.loads(pm.pm_approve_design_and_execute("plan_design_gate_auto_complete", approved_by="user"))
    assert result["ok"] is True
    assert result["design_gate_completion"]["ok"] is True

    saved = pm._load_plans()["plans"]["plan_design_gate_auto_complete"]
    followups = {item["key"]: item for item in saved["created_followup_tasks"]}
    assert followups["T2"]["status"] == "done"

    with kb.connect() as conn:
        gate_id = followups["T2"]["task_id"]
        implementer_id = followups["T3"]["task_id"]
        assert kb.get_task(conn, gate_id).status == "done"
        assert kb.get_task(conn, implementer_id).status == "ready"


def test_mapping_plan_design_approval_marks_plan_approved_without_execution(hermes_home, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="mapping architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="mapping artifact ready")
    source_plan = _base_plan(
        "plan_design_source_for_mapping",
        str(workspace),
        tasks=[
            {"key": "S1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
        ],
        plan_kind="design",
        status="approved",
        design_status="approved",
        design_approved_at=1710000000,
        design_approved_by="user",
        design_artifact_path=".soul/artifacts/design/approved_design_source.md",
    )
    plan = _base_plan(
        "plan_mapping_approval_only",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "mapping architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
        ],
        plan_kind="mapping",
        contract={"source_plan_id": "plan_design_source_for_mapping"},
        status="awaiting_design_approval",
        design_status="awaiting_approval",
        created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
        design_artifact_path=".soul/artifacts/design/plan_mapping_approval_only_design.md",
    )
    data = {"plans": {source_plan["plan_id"]: source_plan, plan["plan_id"]: plan}}
    pm._save_plans(data)
    monkeypatch.setattr(pm, "pm_create_kanban_workflow", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("execution should not start for mapping-only approval")))

    result = json.loads(pm.pm_approve_design_and_execute("plan_mapping_approval_only", approved_by="user"))
    assert result["ok"] is True
    assert result["phase"] == "design_approved"
    assert result["plan_status"] == "approved"
    assert result["execution_started"] is False

    saved = pm._load_plans()["plans"]["plan_mapping_approval_only"]
    assert saved["status"] == "approved"
    assert saved["design_status"] == "approved"
    assert saved["design_approved_by"] == "user"
    assert saved["created_followup_tasks"] == []


def test_design_approval_rejects_staged_plan_without_real_followup_tasks(hermes_home, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(pm, "_run_worker_preflight", lambda assignees: {"ok": True, "checked": list(assignees)})
    monkeypatch.setattr(pm, "pm_create_kanban_workflow", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("execution should not start for invalid follow-up graph")))
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
    plan = _base_plan(
        "plan_missing_execution_followups",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "설계 요약 및 승인 준비", "assignee": "project_manager", "parents": ["T1"]},
        ],
        status="awaiting_design_approval",
        design_status="awaiting_approval",
        created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
        design_artifact_path=".soul/artifacts/design/plan_missing_execution_followups_design.md",
    )
    contract = pm._write_workflow_contract(plan)
    plan["workflow_contract"] = {"path": contract["path"], "ok": True}
    pm._save_plans({"plans": {plan["plan_id"]: plan}})

    result = json.loads(pm.pm_approve_design_and_execute("plan_missing_execution_followups", approved_by="user"))
    assert result["ok"] is False
    assert result["error"] == "resolved follow-up graph violates workflow contract"
    assert "staged plans require an implementer follow-up task after design approval" in result["validation_errors"]
    assert "staged plans require a reviewer follow-up task after design approval" in result["validation_errors"]
    assert "staged plans require a final PM follow-up task after design approval" in result["validation_errors"]

    saved = pm._load_plans()["plans"]["plan_missing_execution_followups"]
    assert saved["status"] == "execution_failed"


def test_staged_plan_rejects_reviewer_not_downstream_of_implementer(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)

    result = json.loads(pm.pm_create_plan(
        request="GET /books API만 구현하고 별도 리뷰를 거친다",
        summary="도서 조회 API staged workflow",
        project_path=str(workspace),
        contract={
            "expected_deliverables": ["src/Books/BooksController.cs"],
            "implementation_paths": ["src/Books/BooksController.cs"],
        },
        tasks=[
            {"key": "T1", "title": "설계", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "구현", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "리뷰", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T1"]},
            {"key": "T4", "title": "최종 보고", "assignee": "project_manager", "parents": ["T3"]},
        ],
    ))

    assert result["ok"] is False
    assert "T3: reviewer task must depend on an implementer task" in result["validation_errors"]


def test_implementer_completion_exports_reviewer_scope(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
        implementer = kb.create_task(conn, title="implementer", assignee="backend-specialist", parents=[architect], workspace_kind="dir", workspace_path=str(workspace))
        reviewer = kb.create_task(conn, title="reviewer", assignee="backend-specialist", parents=[implementer], workspace_kind="dir", workspace_path=str(workspace))
        final = kb.create_task(conn, title="final", assignee="project_manager", parents=[reviewer], workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_impl",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
                {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
            ],
            status="executed",
            design_status="approved",
            design_approved_at=123,
            design_approved_by="user",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
            created_followup_tasks=[
                {"key": "T2", "task_id": implementer, "status": "ready", "parents": [architect]},
                {"key": "T3", "task_id": reviewer, "status": "todo", "parents": [implementer]},
                {"key": "T4", "task_id": final, "status": "todo", "parents": [reviewer]},
            ],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)

        assert _complete_with_claude(conn, workspace, "plan_impl", implementer, "implemented deliverable", "implementer")

    saved = _read_pm_plan(hermes_home, "plan_impl")
    export = saved["approved_scope_export"]
    assert export["phase"] == "reviewer"
    scope = json.loads((workspace / ".soul" / "approved_scope.json").read_text(encoding="utf-8"))
    assert scope["phase"] == "reviewer"
    assert scope["task_id"] == reviewer
    assert ".soul/artifacts/verification/plan_impl_verification.md" in scope["scope"]["allowed_paths"]
    assert scope["design_spec"]["artifact_path"] == ".soul/artifacts/design/plan_impl_design.md"


def test_reviewer_completion_exports_final_scope(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
        implementer = kb.create_task(conn, title="implementer", assignee="backend-specialist", parents=[architect], workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, implementer, summary="implemented")
        reviewer = kb.create_task(conn, title="문서 변경 검토 및 제약 준수 확인", assignee="backend-specialist", parents=[implementer], workspace_kind="dir", workspace_path=str(workspace))
        final = kb.create_task(conn, title="최종 결과 정리 및 사용자 보고", assignee="project_manager", parents=[reviewer], workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_review",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
                {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
            ],
            status="executed",
            design_status="approved",
            design_approved_at=123,
            design_approved_by="user",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
            created_followup_tasks=[
                {"key": "T2", "task_id": implementer, "status": "done", "parents": [architect]},
                {"key": "T3", "task_id": reviewer, "status": "ready", "parents": [implementer]},
                {"key": "T4", "task_id": final, "status": "todo", "parents": [reviewer]},
            ],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)
        pm._export_approved_scope(plan, "reviewer", reviewer, approved_by="user", approved_at=123)

        assert kb.complete_task(
            conn,
            reviewer,
            summary="reviewed deliverable",
            metadata={
                "verdict": "pass",
                "summary": "All required checks passed.",
                "evidence": [".soul/artifacts/verification/plan_review_verification.md"],
                "changed_files": [".soul/artifacts/implementation/plan_review_implementation.md"],
                "scope_status": "within_allowed_paths",
                "validated_artifacts": ["docs/specs/test-deliverable.md"],
                "acceptance_checks": [{"criterion": "docs-only staged workflow test", "status": "pass"}],
                "unmet_requirements": [],
                "design_alignment_summary": "Implementation and deliverables match the approved design spec.",
                "escalation_target": "none",
                "escalation_reason": "",
            },
        )

    saved = _read_pm_plan(hermes_home, "plan_review")
    export = saved["approved_scope_export"]
    assert export["phase"] == "final"
    scope = json.loads((workspace / ".soul" / "approved_scope.json").read_text(encoding="utf-8"))
    assert scope["phase"] == "final"
    assert scope["task_id"] == final
    assert ".soul/artifacts/final/plan_review_final_report.md" in scope["scope"]["allowed_paths"]
    assert scope["reviewer_result"]["verdict"] == "pass"
    assert scope["completion"]["parent_task_ids"] == [reviewer]
    assert scope["reviewer_result"]["acceptance_checks"][0]["status"] == "pass"
    assert saved["review_status"] == "approved"
    assert saved["review_followup_tasks"] == []


def test_reviewer_export_backfills_final_evidence_when_metadata_omits_it(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
        implementer = kb.create_task(conn, title="implementer", assignee="backend-specialist", parents=[architect], workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, implementer, summary="implemented")
        reviewer = kb.create_task(conn, title="reviewer", assignee="backend-specialist", parents=[implementer], workspace_kind="dir", workspace_path=str(workspace))
        final = kb.create_task(conn, title="final", assignee="project_manager", parents=[reviewer], workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_review_fallback",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
                {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
            ],
            status="executed",
            design_status="approved",
            design_approved_at=123,
            design_approved_by="user",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
            created_followup_tasks=[
                {"key": "T2", "task_id": implementer, "status": "done", "parents": [architect]},
                {"key": "T3", "task_id": reviewer, "status": "ready", "parents": [implementer]},
                {"key": "T4", "task_id": final, "status": "todo", "parents": [reviewer]},
            ],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)

        assert kb.complete_task(
            conn,
            reviewer,
            summary="reviewed deliverable",
            metadata={
                "verdict": "pass",
                "summary": "All required checks passed.",
                "changed_files": ["review/bracket_validator_review.md"],
                "scope_status": "within_allowed_paths",
                "validated_artifacts": ["algorithms/bracket_validator.py"],
                "acceptance_checks": [{"criterion": "docs-only staged workflow test", "status": "pass"}],
                "unmet_requirements": [],
                "design_alignment_summary": "Implementation and deliverables match the approved design spec.",
            },
        )

    scope = json.loads((workspace / ".soul" / "approved_scope.json").read_text(encoding="utf-8"))
    assert scope["phase"] == "final"
    assert scope["task_id"] == final
    assert scope["reviewer_result"]["evidence"] == [
        "review/bracket_validator_review.md",
        ".soul/artifacts/verification/plan_review_fallback_verification.md",
    ]


def test_reviewer_concern_exports_final_scope_and_creates_followup_task(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
        implementer = kb.create_task(conn, title="implementer", assignee="backend-specialist", parents=[architect], workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, implementer, summary="implemented")
        reviewer = kb.create_task(conn, title="reviewer", assignee="backend-specialist", parents=[implementer], workspace_kind="dir", workspace_path=str(workspace))
        final = kb.create_task(conn, title="final", assignee="project_manager", parents=[reviewer], workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_review_concern",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
                {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
            ],
            status="executed",
            design_status="approved",
            design_approved_at=123,
            design_approved_by="user",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
            created_followup_tasks=[
                {"key": "T2", "task_id": implementer, "status": "done", "parents": [architect]},
                {"key": "T3", "task_id": reviewer, "status": "ready", "parents": [implementer]},
                {"key": "T4", "task_id": final, "status": "todo", "parents": [reviewer]},
            ],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)
        pm._export_approved_scope(plan, "reviewer", reviewer, approved_by="user", approved_at=123)

        assert kb.complete_task(
            conn,
            reviewer,
            summary="review concern",
            metadata={
                "verdict": "concern",
                "summary": "One follow-up item remains.",
                "evidence": [".soul/artifacts/verification/plan_review_concern_verification.md"],
                "changed_files": [".soul/artifacts/implementation/plan_review_concern_implementation.md"],
                "scope_status": "within_allowed_paths",
                "validated_artifacts": ["docs/specs/test-deliverable.md"],
                "acceptance_checks": [{"criterion": "docs-only staged workflow test", "status": "concern", "notes": "Add retry note."}],
                "unmet_requirements": ["Add retry edge-case note."],
                "design_alignment_summary": "Implementation matches the design but needs one follow-up note.",
                "escalation_target": "implementer",
                "escalation_reason": "Add retry edge-case note.",
            },
        )

    saved = _read_pm_plan(hermes_home, "plan_review_concern")
    assert saved["review_status"] == "concern"
    assert len(saved["review_followup_tasks"]) == 1
    followup_task_id = saved["review_followup_tasks"][0]["task_id"]
    scope = json.loads((workspace / ".soul" / "approved_scope.json").read_text(encoding="utf-8"))
    assert scope["phase"] == "final"
    assert scope["task_id"] == final
    assert scope["reviewer_result"]["verdict"] == "concern"
    assert scope["reviewer_result"]["followup_task_ids"] == [followup_task_id]
    with kb.connect() as conn:
        followup = kb.get_task(conn, followup_task_id)
        assert followup is not None
        assert followup.assignee == "backend-implementer"
        assert followup.status == "ready"
        assert "reviewer status is concern" in (followup.body or "")
        final_comments = kb.list_comments(conn, final)
        assert any(followup_task_id in comment.body for comment in final_comments)


def test_reviewer_blocked_verdict_does_not_export_final_scope(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
        implementer = kb.create_task(conn, title="implementer", assignee="backend-specialist", parents=[architect], workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, implementer, summary="implemented")
        reviewer = kb.create_task(conn, title="reviewer", assignee="backend-specialist", parents=[implementer], workspace_kind="dir", workspace_path=str(workspace))
        final = kb.create_task(conn, title="final", assignee="project_manager", parents=[reviewer], workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_review_blocked",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
                {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
            ],
            status="executed",
            design_status="approved",
            design_approved_at=123,
            design_approved_by="user",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
            created_followup_tasks=[
                {"key": "T2", "task_id": implementer, "status": "done", "parents": [architect]},
                {"key": "T3", "task_id": reviewer, "status": "ready", "parents": [implementer]},
                {"key": "T4", "task_id": final, "status": "todo", "parents": [reviewer]},
            ],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)
        pm._export_approved_scope(plan, "reviewer", reviewer, approved_by="user", approved_at=123)

        assert kb.complete_task(
            conn,
            reviewer,
            summary="review blocked",
                metadata={
                    "verdict": "blocked",
                    "summary": "Out-of-scope changes detected.",
                    "evidence": [".soul/artifacts/verification/plan_review_blocked_verification.md"],
                    "changed_files": [".soul/artifacts/implementation/plan_review_blocked_implementation.md"],
                    "scope_status": "out_of_scope",
                    "validated_artifacts": ["docs/specs/test-deliverable.md"],
                    "acceptance_checks": [{"criterion": "docs-only staged workflow test", "status": "blocked"}],
                    "unmet_requirements": ["Out-of-scope implementation changes must be removed."],
                    "design_alignment_summary": "Reviewer found changes outside the approved design scope.",
                    "escalation_target": "implementer",
                    "escalation_reason": "Out-of-scope implementation changes must be removed.",
                    "blocked_reason": "changed files exceeded approved scope",
                },
)

    saved = _read_pm_plan(hermes_home, "plan_review_blocked")
    assert saved["review_status"] == "blocked"
    assert saved["review_verdict"] == "blocked"
    assert len(saved["review_followup_tasks"]) == 1
    followup_task_id = saved["review_followup_tasks"][0]["task_id"]
    assert "approved_scope_export" not in saved
    scope = json.loads((workspace / ".soul" / "approved_scope.json").read_text(encoding="utf-8"))
    assert scope["phase"] == "reviewer"
    with kb.connect() as conn:
        followup = kb.get_task(conn, followup_task_id)
        assert followup is not None
        assert followup.assignee == "backend-implementer"
        assert followup.status == "ready"
        assert "reviewer status is blocked" in (followup.body or "")


def test_reviewer_followup_widens_scope_and_exports_implementer_gate_for_code_remediation(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    design_path = workspace / ".soul" / "artifacts" / "design" / "plan_review_scope_design.md"
    design_path.parent.mkdir(parents=True, exist_ok=True)
    design_path.write_text(
        "# Design\n\n- `LoginSample.sln`\n- `src/Host/Program.cs`\n- `src/Host/Host.csproj`\n",
        encoding="utf-8",
    )
    contract = {
        "design_artifact": ".soul/artifacts/design/plan_review_scope_design.md",
        "expected_deliverables": [
            "src/Host/Program.cs",
            "src/Host/CompositionRoot/ServiceCollectionExtensions.cs",
            "src/Host/Endpoints/EndpointRouteBuilderExtensions.cs",
        ],
        "implementation_paths": [
            "src/Host/Program.cs",
            "src/Host/CompositionRoot/ServiceCollectionExtensions.cs",
            "src/Host/Endpoints/EndpointRouteBuilderExtensions.cs",
        ],
        "phase_allowed_paths": {
            "architect": [".soul/artifacts/design/plan_review_scope_design.md"],
            "implementer": [
                "src/Host/Program.cs",
                "src/Host/CompositionRoot/ServiceCollectionExtensions.cs",
                "src/Host/Endpoints/EndpointRouteBuilderExtensions.cs",
                ".soul/artifacts/implementation/plan_review_scope_implementation.md",
            ],
            "reviewer": [".soul/artifacts/verification/plan_review_scope_verification.md"],
            "final": [".soul/artifacts/final/plan_review_scope_final_report.md"],
        },
        "required_evidence_by_phase": {
            "architect": [".soul/artifacts/design/plan_review_scope_design.md"],
            "implementation": [
                ".soul/artifacts/design/plan_review_scope_design.md",
                "src/Host/Program.cs",
                "src/Host/CompositionRoot/ServiceCollectionExtensions.cs",
                "src/Host/Endpoints/EndpointRouteBuilderExtensions.cs",
            ],
            "reviewer": [
                ".soul/artifacts/design/plan_review_scope_design.md",
                "src/Host/Program.cs",
                "src/Host/CompositionRoot/ServiceCollectionExtensions.cs",
                "src/Host/Endpoints/EndpointRouteBuilderExtensions.cs",
                ".soul/artifacts/verification/plan_review_scope_verification.md",
            ],
            "final": [
                ".soul/artifacts/design/plan_review_scope_design.md",
                "src/Host/Program.cs",
                "src/Host/CompositionRoot/ServiceCollectionExtensions.cs",
                "src/Host/Endpoints/EndpointRouteBuilderExtensions.cs",
                ".soul/artifacts/verification/plan_review_scope_verification.md",
                ".soul/artifacts/final/plan_review_scope_final_report.md",
            ],
        },
    }
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
        implementer = kb.create_task(conn, title="implementer", assignee="backend-specialist", parents=[architect], workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, implementer, summary="implemented")
        reviewer = kb.create_task(conn, title="reviewer", assignee="backend-specialist", parents=[implementer], workspace_kind="dir", workspace_path=str(workspace))
        final = kb.create_task(conn, title="final", assignee="project_manager", parents=[reviewer], workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_review_scope",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
                {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
            ],
            status="executed",
            design_status="approved",
            design_approved_at=123,
            design_approved_by="user",
            contract=contract,
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
            created_followup_tasks=[
                {"key": "T2", "task_id": implementer, "status": "done", "parents": [architect]},
                {"key": "T3", "task_id": reviewer, "status": "ready", "parents": [implementer]},
                {"key": "T4", "task_id": final, "status": "todo", "parents": [reviewer]},
            ],
        )
        contract_write = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract_write["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)
        pm._export_approved_scope(plan, "reviewer", reviewer, approved_by="user", approved_at=123)

        assert kb.complete_task(
            conn,
            reviewer,
            summary="review concern",
            metadata={
                "verdict": "concern",
                "summary": "Host entrypoint is not backed by src/Host/Host.csproj, so the tree is not buildable.",
                "evidence": [".soul/artifacts/verification/plan_review_scope_verification.md"],
                "changed_files": [".soul/artifacts/verification/plan_review_scope_verification.md"],
                "scope_status": "within_allowed_paths",
                "validated_artifacts": ["src/Host/Program.cs"],
                "acceptance_checks": [{"criterion": "host entrypoint buildability", "status": "concern", "notes": "root solution wiring is also missing"}],
                "unmet_requirements": ["`src/Host/Host.csproj` and root solution wiring are absent."],
                "design_alignment_summary": "Host composition files exist, but the buildable host project layer is incomplete.",
                "escalation_target": "implementer",
                "escalation_reason": "Add `src/Host/Host.csproj` and the matching solution wiring from the approved topology.",
            },
        )

    saved = _read_pm_plan(hermes_home, "plan_review_scope")
    followup_task_id = saved["review_followup_tasks"][0]["task_id"]
    followup_scope = json.loads((workspace / ".soul" / "approved_scope.json").read_text(encoding="utf-8"))
    assert followup_scope["phase"] == "implementer"
    assert followup_scope["task_id"] == followup_task_id
    assert "src/Host/Host.csproj" in followup_scope["scope"]["allowed_paths"]
    assert "LoginSample.sln" in followup_scope["scope"]["allowed_paths"]

    written_contract = json.loads((workspace / ".soul" / "workflows" / "plan_review_scope" / "contract.json").read_text(encoding="utf-8"))
    assert "src/Host/Host.csproj" in written_contract["implementation_paths"]
    assert "LoginSample.sln" in written_contract["implementation_paths"]


def test_final_completion_updates_plan_with_structured_report(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / ".soul" / "artifacts" / "final").mkdir(parents=True, exist_ok=True)
    (workspace / ".soul" / "artifacts" / "verification").mkdir(parents=True, exist_ok=True)
    (workspace / ".soul" / "artifacts" / "final" / "plan_final_done_final_report.md").write_text("""# Final Report\n\n## Summary\nOK\n\n## Delivered Artifacts\n- .soul/artifacts/final/plan_final_done_final_report.md\n- .soul/artifacts/verification/plan_final_done_verification.md\n\n## Open Risks\n- None\n""", encoding="utf-8")
    (workspace / ".soul" / "artifacts" / "verification" / "plan_final_done_verification.md").write_text("verification report", encoding="utf-8")
    with kb.connect() as conn:
        final = kb.create_task(conn, title="final", assignee="project_manager", workspace_kind="dir", workspace_path=str(workspace))

    plan = _base_plan(
        "plan_final_done",
        str(workspace),
        tasks=[
            {"key": "T4", "title": "final", "assignee": "project_manager", "pm_phase": "final_synthesis", "terminal_task": True, "parents": ["T3"]},
        ],
        status="review_approved",
        review_status="approved",
        created_followup_tasks=[{"key": "T4", "task_id": final, "status": "running", "parents": []}],
    )
    contract = pm._write_workflow_contract(plan)
    plan["workflow_contract"] = {"path": contract["path"], "ok": True}
    _write_pm_plan(hermes_home, plan)

    with kb.connect() as conn:
        reviewer = kb.create_task(conn, title="reviewer", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        pm._export_approved_scope(plan, "reviewer", reviewer, approved_by="user", approved_at=123)
        kb.complete_task(
            conn,
            reviewer,
            summary="reviewed deliverable",
            metadata={
                "verdict": "pass",
                "summary": "Reviewer approved the result.",
                "evidence": [".soul/artifacts/verification/plan_final_done_verification.md"],
                "changed_files": [".soul/artifacts/implementation/plan_final_done_implementation.md"],
                "scope_status": "within_allowed_paths",
                "validated_artifacts": ["algorithms/bracket_validator.py"],
                "acceptance_checks": [{"criterion": "docs-only staged workflow test", "status": "pass"}],
                "unmet_requirements": [],
                "design_alignment_summary": "Implementation and deliverables match the approved design spec.",
            },
        )
        kb.link_tasks(conn, reviewer, final)
        (workspace / ".soul" / "approved_scope.json").write_text(
            json.dumps(
                {
                    "phase": "final",
                    "scope": {"allowed_paths": [".soul/artifacts/final/", ".soul/artifacts/verification/"]},
                    "final_report": {
                        "required_fields": ["report_summary", "user_report", "delivered_artifacts", "open_risks"],
                        "allowed_statuses": ["completed", "needs_followup"],
                        "required_artifacts": [".soul/artifacts/final/plan_final_done_final_report.md"],
                        "required_sections": ["# Final Report", "## Summary", "## Delivered Artifacts", "## Open Risks"],
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        assert kb.complete_task(
            conn,
            final,
            summary="final report delivered",
            metadata={
                "report_summary": "Workflow changes completed and validated.",
                "user_report": "All requested work is complete. Reviewer approval and evidence are attached. Implementation and deliverables match the approved design spec.",
                "delivered_artifacts": [
                    ".soul/artifacts/final/plan_final_done_final_report.md",
                    ".soul/artifacts/verification/plan_final_done_verification.md",
                ],
                "open_risks": [],
                "completion_status": "completed",
            },
        )

    saved = _read_pm_plan(hermes_home, "plan_final_done")
    assert saved["status"] == "completed"
    assert saved["final_status"] == "completed"
    assert saved["final_summary"] == "Workflow changes completed and validated."
    assert saved["final_report_metadata"]["completion_status"] == "completed"
    assert saved["final_report_metadata"]["delivered_artifacts"][0] == ".soul/artifacts/final/plan_final_done_final_report.md"


def test_final_completion_hydrates_reviewer_result_from_parent_metadata(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / ".soul" / "artifacts" / "final").mkdir(parents=True, exist_ok=True)
    (workspace / ".soul" / "artifacts" / "verification").mkdir(parents=True, exist_ok=True)
    final_report = workspace / ".soul" / "artifacts" / "final" / "plan_final_fallback_final_report.md"
    verification = workspace / ".soul" / "artifacts" / "verification" / "plan_final_fallback_verification.md"
    final_report.write_text("""# Final Report\n\n## Summary\nValidated fallback path.\n\n## Delivered Artifacts\n- .soul/artifacts/final/plan_final_fallback_final_report.md\n- .soul/artifacts/verification/plan_final_fallback_verification.md\n\n## Open Risks\n- None\n""", encoding="utf-8")
    verification.write_text("verification report", encoding="utf-8")

    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
        implementer = kb.create_task(conn, title="implementer", assignee="backend-specialist", parents=[architect], workspace_kind="dir", workspace_path=str(workspace))
        _complete_with_claude(conn, workspace, "plan_impl", implementer, "implemented deliverable", "implementer")
        reviewer = kb.create_task(conn, title="reviewer", assignee="backend-specialist", parents=[implementer], workspace_kind="dir", workspace_path=str(workspace))
        final = kb.create_task(conn, title="final", assignee="project_manager", parents=[reviewer], workspace_kind="dir", workspace_path=str(workspace))

        plan = _base_plan(
            "plan_final_fallback",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
                {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
            ],
            status="executed",
            design_status="approved",
            design_approved_at=123,
            design_approved_by="user",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
            created_followup_tasks=[
                {"key": "T2", "task_id": implementer, "status": "done", "parents": [architect]},
                {"key": "T3", "task_id": reviewer, "status": "ready", "parents": [implementer]},
                {"key": "T4", "task_id": final, "status": "todo", "parents": [reviewer]},
            ],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)
        pm._export_approved_scope(plan, "reviewer", reviewer, approved_by="user", approved_at=123)
        assert kb.complete_task(
            conn,
            reviewer,
            summary="reviewed deliverable",
            metadata={
                "verdict": "pass",
                "summary": "Reviewer approved the result.",
                    "evidence": [".soul/artifacts/verification/plan_final_fallback_verification.md"],
                    "changed_files": [".soul/artifacts/implementation/plan_final_fallback_implementation.md"],
                    "scope_status": "within_allowed_paths",
                    "validated_artifacts": ["docs/specs/test-deliverable.md"],
                    "acceptance_checks": [{"criterion": "bounded validation", "status": "pass"}],
                    "unmet_requirements": [],
                    "design_alignment_summary": "Implementation and deliverables match the approved design spec.",
                    "escalation_target": "none",
                    "escalation_reason": "",
                },
            )

        (workspace / ".soul" / "approved_scope.json").write_text(
            json.dumps(
                {
                    "phase": "final",
                    "scope": {"allowed_paths": [".soul/artifacts/final/", ".soul/artifacts/verification/", "review/"]},
                    "final_report": {
                        "required_fields": ["report_summary", "user_report", "delivered_artifacts", "open_risks"],
                        "allowed_statuses": ["completed", "needs_followup"],
                        "required_artifacts": [".soul/artifacts/final/plan_final_fallback_final_report.md"],
                        "required_sections": ["# Final Report", "## Summary", "## Delivered Artifacts", "## Open Risks"],
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        assert kb.complete_task(
            conn,
            final,
            summary="final report delivered",
            metadata={
                "report_summary": "Workflow changes completed and validated.",
                "user_report": "Fallback reviewer hydration path works correctly. Reviewer approved the result. Implementation and deliverables match the approved design spec.",
                "delivered_artifacts": [
                    ".soul/artifacts/final/plan_final_fallback_final_report.md",
                    ".soul/artifacts/verification/plan_final_fallback_verification.md",
                ],
                "open_risks": [],
                "completion_status": "completed",
            },
        )

    saved = _read_pm_plan(hermes_home, "plan_final_fallback")
    assert saved["status"] == "completed"
    assert saved["final_status"] == "completed"
    assert saved["final_report_metadata"]["completion_status"] == "completed"
    assert saved["final_summary"] == "Workflow changes completed and validated."


def test_reviewer_scope_export_includes_review_contract(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-specialist", workspace_kind="dir", workspace_path=str(workspace))
        kb.complete_task(conn, architect, summary="design done")
        implementer = kb.create_task(conn, title="implementer", assignee="backend-specialist", parents=[architect], workspace_kind="dir", workspace_path=str(workspace))
        reviewer = kb.create_task(conn, title="reviewer", assignee="backend-specialist", parents=[implementer], workspace_kind="dir", workspace_path=str(workspace))
        final = kb.create_task(conn, title="final", assignee="project_manager", parents=[reviewer], workspace_kind="dir", workspace_path=str(workspace))
        plan = _base_plan(
            "plan_review_scope",
            str(workspace),
            tasks=[
                {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
                {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
                {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
                {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
            ],
            contract={
                "expected_deliverables": ["docs/specs/review-note.md"],
                "implementation_paths": ["docs/specs/review-note.md"],
            },
            status="executed",
            design_status="approved",
            design_approved_at=123,
            design_approved_by="user",
            created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
            created_followup_tasks=[
                {"key": "T2", "task_id": implementer, "status": "ready", "parents": [architect]},
                {"key": "T3", "task_id": reviewer, "status": "todo", "parents": [implementer]},
                {"key": "T4", "task_id": final, "status": "todo", "parents": [reviewer]},
            ],
        )
        contract = pm._write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract["path"], "ok": True}
        _write_pm_plan(hermes_home, plan)

        assert _complete_with_claude(conn, workspace, "plan_review_scope", implementer, "implemented deliverable", "implementer")

    scope = json.loads((workspace / ".soul" / "approved_scope.json").read_text(encoding="utf-8"))
    assert scope["phase"] == "reviewer"
    assert scope["review"]["mode"] == "docs"
    assert scope["review"]["required_verdict_fields"] == [
        "verdict",
        "summary",
        "evidence",
        "changed_files",
        "scope_status",
        "validated_artifacts",
        "acceptance_checks",
        "unmet_requirements",
        "design_alignment_summary",
        "escalation_target",
        "escalation_reason",
    ]
    assert scope["review"]["allowed_escalation_targets"] == ["implementer", "architect", "project_manager", "none"]
    assert "deliverable_exists" in scope["review"]["checks"]
    assert scope["review"]["design_artifact"] == ".soul/artifacts/design/plan_review_scope_design.md"


def test_explicit_contract_deliverables_override_path_inference(tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_contract",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
        contract={
            "expected_deliverables": ["docs/specs/explicit-note.md"],
            "implementation_paths": ["docs/specs/explicit-note.md"],
            "risk_level": "medium",
        },
    )
    built = pm._build_workflow_contract(plan)
    assert built["version"] == 3
    assert built["expected_deliverables"] == ["docs/specs/explicit-note.md"]
    assert built["risk_level"] == "medium"
    assert built["review"]["mode"] == "docs"
    assert built["review"]["required_verdict_fields"] == [
        "verdict",
        "summary",
        "evidence",
        "changed_files",
        "scope_status",
        "validated_artifacts",
        "acceptance_checks",
        "unmet_requirements",
        "design_alignment_summary",
        "escalation_target",
        "escalation_reason",
    ]
    assert built["approval_stages"]["pm_plan"]["status"] == "awaiting_pm_approval"
    assert "line_count_limit" in built["review"]["checks"]
    assert built["review"]["design_artifact"] == ".soul/artifacts/design/plan_contract_design.md"


def test_code_deliverable_contract_uses_code_review_mode(tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_code_contract",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
        contract={"expected_deliverables": ["src/app/service.py"]},
    )
    built = pm._build_workflow_contract(plan)
    assert built["review"]["mode"] == "code"
    assert "build_or_static_validation_run" in built["review"]["checks"]
    assert "implementation_matches_design" in built["review"]["checks"]
    assert "deliverable_exists" in built["review"]["checks"]


def test_contract_implementation_paths_are_canonical_for_code_scope(tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_impl_paths",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "implementation mapping", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
        contract={
            "expected_deliverables": [
                ".soul/artifacts/design/plan_impl_paths_design.md",
                ".soul/artifacts/reports/order-detail-summary-validation.md",
            ],
            "implementation_paths": [
                "src/Orders/OrderDetailContracts.cs",
                "src/Orders/OrderDetailSummaryCalculator.cs",
                "tests/Orders.Tests/OrderDetailSummaryCalculatorTests.cs",
            ],
        },
    )
    built = pm._build_workflow_contract(plan)
    assert built["implementation_paths"] == [
        "src/Orders/OrderDetailContracts.cs",
        "src/Orders/OrderDetailSummaryCalculator.cs",
        "tests/Orders.Tests/OrderDetailSummaryCalculatorTests.cs",
    ]
    assert built["phase_allowed_paths"]["implementer"] == [
        "src/Orders/OrderDetailContracts.cs",
        "src/Orders/OrderDetailSummaryCalculator.cs",
        "tests/Orders.Tests/OrderDetailSummaryCalculatorTests.cs",
        ".soul/artifacts/implementation/plan_impl_paths_implementation.md",
    ]
    assert built["required_evidence_by_phase"]["implementation"] == [
        ".soul/artifacts/design/plan_impl_paths_design.md",
        "src/Orders/OrderDetailContracts.cs",
        "src/Orders/OrderDetailSummaryCalculator.cs",
        "tests/Orders.Tests/OrderDetailSummaryCalculatorTests.cs",
    ]
    assert built["review"]["mode"] == "code"
    assert built["review"]["expected_deliverables"] == [
        "src/Orders/OrderDetailContracts.cs",
        "src/Orders/OrderDetailSummaryCalculator.cs",
        "tests/Orders.Tests/OrderDetailSummaryCalculatorTests.cs",
    ]


def test_build_workflow_contract_derives_implementation_paths_from_design_artifact(tmp_path):
    workspace = _workspace(tmp_path)
    design_path = workspace / ".soul" / "artifacts" / "design" / "plan_design_scope_design.md"
    design_path.parent.mkdir(parents=True, exist_ok=True)
    design_path.write_text(
        """
# Design

- 구현 대상: src/Auth/LoginService.cs
- DTO 변경: src/Auth/LoginResponseDto.cs
- 테스트 반영: tests/Auth.Tests/LoginServiceTests.cs
""".strip(),
        encoding="utf-8",
    )
    plan = _base_plan(
        "plan_design_scope",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
        contract={},
        design_artifact_path=".soul/artifacts/design/plan_design_scope_design.md",
        design_ready_at=123,
    )
    built = pm._build_workflow_contract(plan)
    assert built["expected_deliverables"] == [
        "src/Auth/LoginService.cs",
        "src/Auth/LoginResponseDto.cs",
        "tests/Auth.Tests/LoginServiceTests.cs",
    ]
    assert built["implementation_paths"] == [
        "src/Auth/LoginService.cs",
        "src/Auth/LoginResponseDto.cs",
        "tests/Auth.Tests/LoginServiceTests.cs",
    ]


def test_build_workflow_contract_uses_completed_architect_artifact_when_default_is_empty(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    design_path = workspace / ".soul" / "artifacts" / "design" / "t_arch_get_books_design.md"
    design_path.parent.mkdir(parents=True, exist_ok=True)
    design_path.write_text(
        """
# GET /books design

- 영향 파일: src/Host/Extensions/EndpointRouteBuilderExtensions.cs
- 계약 파일: src/Application/Abstractions/IBookRepository.cs
- 저장소 파일: src/Infrastructure/Returns/InMemoryBookRepository.cs
- 테스트 파일: tests/Books.Tests/BookEndpointTests.cs
""".strip(),
        encoding="utf-8",
    )
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-architect", workspace_kind="dir", workspace_path=str(workspace))
        assert kb.complete_task(
            conn,
            architect,
            summary="design ready",
            metadata={"artifacts": [str(design_path)]},
        )
    plan = _base_plan(
        "plan_completed_arch_artifact",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-architect", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-implementer", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-reviewer", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
        contract={},
        created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
        design_ready_at=123,
    )

    built = pm._build_workflow_contract(plan)

    assert built["design_artifact"] == ".soul/artifacts/design/t_arch_get_books_design.md"
    assert built["implementation_paths"] == [
        "src/Host/Extensions/EndpointRouteBuilderExtensions.cs",
        "src/Application/Abstractions/IBookRepository.cs",
        "src/Infrastructure/Returns/InMemoryBookRepository.cs",
        "tests/Books.Tests/BookEndpointTests.cs",
    ]
    assert built["phase_allowed_paths"]["implementer"] == [
        "src/Host/Extensions/EndpointRouteBuilderExtensions.cs",
        "src/Application/Abstractions/IBookRepository.cs",
        "src/Infrastructure/Returns/InMemoryBookRepository.cs",
        "tests/Books.Tests/BookEndpointTests.cs",
        ".soul/artifacts/implementation/plan_completed_arch_artifact_implementation.md",
    ]


def test_approve_design_rejects_followup_execution_when_scope_export_has_no_concrete_paths(hermes_home, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="architect", assignee="backend-architect", workspace_kind="dir", workspace_path=str(workspace))
        assert kb.complete_task(conn, architect, summary="design ready without concrete paths")

    plan = _base_plan(
        "plan_missing_concrete_scope",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-architect", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-implementer", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-reviewer", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
        contract={},
        status="awaiting_design_approval",
        design_status="awaiting_approval",
        design_ready_at=123,
        approved_at=111,
        approved_by="user",
        created_design_tasks=[{"key": "T1", "task_id": architect, "status": "done"}],
    )
    pm._save_plans({"plans": {plan["plan_id"]: plan}})
    monkeypatch.setattr(pm, "pm_create_kanban_workflow", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("follow-up cards must not be created")))

    result = json.loads(pm.pm_approve_design_and_execute("plan_missing_concrete_scope", approved_by="user"))

    assert result["ok"] is False
    assert result["error"] == "workflow contract is not executable after design approval"
    assert any("implementer tasks require at least one non-.soul allowed path" in error for error in result["validation_errors"])
    saved = pm._load_plans()["plans"]["plan_missing_concrete_scope"]
    assert saved["status"] == "awaiting_design_approval"
    assert saved["created_followup_tasks"] == []


def test_export_approved_scope_uses_contract_implementation_paths(tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_scope_impl_paths",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "implementation mapping", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "assignee": "project_manager", "parents": ["T3"]},
        ],
        design_status="approved",
        design_approved_at=123,
        design_approved_by="user",
        contract={
            "expected_deliverables": [
                ".soul/artifacts/design/plan_scope_impl_paths_design.md",
                ".soul/artifacts/reports/order-detail-summary-validation.md",
            ],
            "implementation_paths": [
                "src/Orders/OrderDetailContracts.cs",
                "tests/Orders.Tests/OrderDetailSummaryCalculatorTests.cs",
            ],
        },
    )
    exported = pm._export_approved_scope(plan, "implementer", "t_impl", approved_by="user", approved_at=123)
    assert exported["ok"] is True
    scope = exported["scope"]
    assert scope["scope"]["allowed_paths"] == [
        "src/Orders/OrderDetailContracts.cs",
        "tests/Orders.Tests/OrderDetailSummaryCalculatorTests.cs",
        ".soul/artifacts/implementation/plan_scope_impl_paths_implementation.md",
    ]
    assert scope["design_spec"]["expected_deliverables"] == [
        ".soul/artifacts/design/plan_scope_impl_paths_design.md",
        ".soul/artifacts/reports/order-detail-summary-validation.md",
    ]
    assert scope["design_spec"]["implementation_paths"] == [
        "src/Orders/OrderDetailContracts.cs",
        "tests/Orders.Tests/OrderDetailSummaryCalculatorTests.cs",
    ]


def test_final_phase_excludes_design_approval_pm_task(tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_pm_phase_split",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "design approval request", "assignee": "project_manager", "parents": ["T1"]},
            {"key": "T3", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T2"]},
            {"key": "T4", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T3"]},
            {"key": "T5", "title": "final synthesis", "assignee": "project_manager", "parents": ["T4"]},
        ],
        contract={"expected_deliverables": ["algorithms/bracket_validator.py"]},
    )
    built = pm._build_workflow_contract(plan)
    assert built["required_tasks_by_phase"]["final"] == ["T5"]
    assert pm._plan_mode_for_key(plan, "T2") == "design_summary"
    assert pm._plan_mode_for_key(plan, "T5") == "final"


def test_pm_supersede_plan_marks_plan_stale(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_stale",
        str(workspace),
        tasks=[{"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []}],
    )
    _write_pm_plan(hermes_home, plan)
    result = json.loads(pm.pm_supersede_plan("plan_stale", "obsolete test", superseded_by="plan_new"))
    assert result["ok"] is True
    saved = _read_pm_plan(hermes_home, "plan_stale")
    assert saved["status"] == "superseded"
    assert saved["design_status"] == "superseded"
    assert saved["superseded_by"] == "plan_new"


def test_pm_get_plan_status_normalizes_legacy_null_workflow_fields(hermes_home, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    plan = {
        "plan_id": "plan_legacy_nulls",
        "request": "legacy workflow status normalization",
        "summary": "legacy null workflow fields",
        "project_path": str(workspace),
        "status": "executed",
        "plan_kind": None,
        "design_status": None,
        "review_status": None,
        "final_status": None,
        "execution_phase": None,
        "tasks": [
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
        ],
    }
    _write_pm_plan(hermes_home, plan)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home / "profiles" / "project_manager"))

    result = json.loads(pm.pm_get_plan_status("plan_legacy_nulls"))

    assert result["ok"] is True
    saved = result["plan"]
    assert saved["plan_kind"] == "staged"
    assert saved["design_status"] == "not_started"
    assert saved["review_status"] == "not_started"
    assert saved["final_status"] == "not_started"
    assert saved["execution_phase"] == "plan"


def test_pm_create_plan_rejects_noncanonical_contract_phase_keys(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="Create docs/specs/live-smoke-test-note-v9.md only.",
        summary="bad explicit contract",
        tasks=[
            {"key": "T1", "title": "architect", "body": "design", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "body": "write", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "body": "review", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T3"]},
        ],
        contract={
            "expected_deliverables": ["docs/specs/live-smoke-test-note-v9.md"],
            "required_tasks_by_phase": {"design": ["T1"], "implementation": ["T2"], "review": ["T3"], "final": ["T4"]},
        },
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert "contract" in result["error"].lower()
    assert any("required_tasks_by_phase" in err for err in result.get("validation_errors", []))



def test_pm_create_plan_rejects_nonpath_expected_deliverables(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="Create docs/specs/live-smoke-test-note-v9.md only.",
        summary="bad deliverables",
        tasks=[
            {"key": "T1", "title": "architect", "body": "design", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "body": "write", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "body": "review", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T3"]},
        ],
        contract={
            "expected_deliverables": ["Implementation artifact: docs/specs/live-smoke-test-note-v9.md"],
        },
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert "contract" in result["error"].lower()
    assert any("expected_deliverables" in err for err in result.get("validation_errors", []))


def test_pm_create_plan_allows_staged_plan_without_explicit_deliverable_paths_before_architect_scope(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="Bracket validator algorithm implementation.",
        summary="missing concrete code path",
        tasks=[
            {"key": "T1", "title": "architect", "body": "design", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "body": "implement single source file", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "body": "review", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T3"]},
        ],
        project_path=str(workspace),
    ))
    assert result["ok"] is True
    assert result["status"] == "awaiting_pm_approval"
    saved = pm._load_plans()["plans"][result["plan_id"]]
    built = pm._build_workflow_contract(saved)
    assert built["expected_deliverables"] == []
    assert built["implementation_paths"] == []
    assert built["approval_stages"]["pm_plan"]["status"] == "awaiting_pm_approval"


def test_pm_create_plan_allows_mixed_mapping_and_execution_plan_before_architect_scope_is_locked(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="승인된 설계안(.soul/artifacts/design/plan_f7927906_design.md)을 기준으로 주문 상세 조회 응답에 summary(totalItemCount, totalAmount, 조건부 finalAmount)를 구현하고 테스트/검증 결과까지 보고",
        summary="승인된 설계안을 구현 가능한 구체 파일 경로로 매핑한 뒤 주문 상세 summary 응답을 구현/검증하는 후속 실행 계획",
        tasks=[
            {"key": "T1", "title": "구현 경로 매핑 설계", "body": "승인된 설계안을 기준으로 실제 구현 대상 파일과 테스트 파일을 확정한다.", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "주문 상세 응답 summary 구현", "body": "T1에서 식별한 실제 구현 경로에 따라 주문 상세 조회 응답에 summary를 추가한다.", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "테스트 및 회귀 검증", "body": "주문 상세 응답 계약 테스트와 하위 호환성 검증 결과를 .soul/artifacts/reports/order-detail-summary-validation.md에 정리한다.", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "최종 보고", "body": "최종 결과를 보고한다.", "assignee": "project_manager", "parents": ["T3"]},
        ],
        contract={
            "expected_deliverables": [
                ".soul/artifacts/design/plan_f7927906_design.md",
                ".soul/artifacts/reports/order-detail-summary-validation.md",
            ],
        },
        project_path=str(workspace),
    ))
    assert result["ok"] is True
    assert result["status"] == "awaiting_pm_approval"
    saved = pm._load_plans()["plans"][result["plan_id"]]
    built = pm._build_workflow_contract(saved)
    assert built["implementation_paths"] == []
    assert built["expected_deliverables"] == [
        ".soul/artifacts/design/plan_f7927906_design.md",
        ".soul/artifacts/reports/order-detail-summary-validation.md",
    ]


def test_pm_create_plan_rejects_design_plan_with_execution_tasks(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="설계만 먼저 진행",
        summary="design-only plan should not contain implementer tasks",
        plan_kind="design",
        tasks=[
            {"key": "T1", "title": "architect", "body": "design", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "body": "implement", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
        ],
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert "plan kind" in result["error"].lower()
    assert any("design plan" in err.lower() for err in result.get("validation_errors", []))


def test_pm_create_plan_rejects_staged_plan_without_execution_roles(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="설계 승인 후 구현까지 이어지는 staged workflow",
        summary="staged plan must already contain implementation/review/final roles",
        tasks=[
            {"key": "T1", "title": "architect", "body": "design", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "설계 요약 및 승인 준비", "body": "approve design", "assignee": "project_manager", "parents": ["T1"]},
        ],
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert "staged workflow contract" in result["error"]
    assert "staged workflow requires at least one backend-implementer implementer task after design approval" in result.get("validation_errors", [])
    assert "staged workflow requires at least one backend-reviewer reviewer task after implementation" in result.get("validation_errors", [])
    assert "staged workflow requires at least one terminal project_manager final/synthesis task after reviewer completion" in result.get("validation_errors", [])


def test_pm_create_kanban_workflow_allows_multi_architect_design_phase_without_pm_finalizer(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_kanban_workflow(
        request="인증/사용자 API 설계 단계",
        tasks=[
            {"key": "T1", "title": "요구사항 및 영향 범위 분석", "body": "현재 인증/사용자 흐름을 분석한다.", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "API/도메인 설계안 작성", "body": "상세 설계안을 작성한다.", "assignee": "backend-specialist", "mode": "architect", "parents": ["T1"]},
        ],
        project_path=str(workspace),
    ))
    assert result["ok"] is True
    created = {item["key"]: item for item in result["created_tasks"]}
    assert set(created) == {"T1", "T2"}
    assert created["T1"]["assignee"] == "backend-specialist"
    assert created["T2"]["parents"] == [created["T1"]["task_id"]]


def test_pm_execute_plan_allows_design_phase_with_multiple_architect_tasks(hermes_home, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(pm, "_run_worker_preflight", lambda assignees: {"ok": True, "checked": list(assignees)})
    result = json.loads(pm.pm_create_plan(
        request="로그인 응답에 lastLoginAt 추가, 로그아웃 기능 추가, 비밀번호 변경 기능 추가, 사용자 조회 응답에 status 필드 추가",
        summary="인증/사용자 API 확장",
        plan_kind="design",
        tasks=[
            {"key": "T1", "title": "요구사항 및 영향 범위 분석", "body": "현재 인증/사용자 흐름을 분석한다.", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "API/도메인 설계안 작성", "body": "상세 설계안을 작성한다.", "assignee": "backend-specialist", "mode": "architect", "parents": ["T1"]},
        ],
        project_path=str(workspace),
    ))
    assert result["ok"] is True

    executed = json.loads(pm.pm_execute_plan(result["plan_id"], approved_by="user"))
    assert executed["ok"] is True
    assert executed["phase"] == "design"
    assert executed["plan_status"] == "design_in_progress"
    assert len(executed["created_design_tasks"]) == 2

def test_pm_create_plan_rejects_execution_plan_without_design_artifact_reference(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="src/Orders/OrderDetailService.cs와 tests/Orders.Tests/OrderDetailServiceTests.cs를 수정",
        summary="execution plan requires approved design reference",
        plan_kind="execution",
        tasks=[
            {"key": "T1", "title": "implementer", "body": "implement", "assignee": "backend-specialist", "mode": "implementer", "parents": []},
            {"key": "T2", "title": "reviewer", "body": "review", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T1"]},
            {"key": "T3", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T2"]},
        ],
        contract={
            "implementation_paths": [
                "src/Orders/OrderDetailService.cs",
                "tests/Orders.Tests/OrderDetailServiceTests.cs",
            ],
        },
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert "plan kind" in result["error"].lower()
    assert any("design_artifact" in err for err in result.get("validation_errors", []))


def test_pm_create_plan_rejects_execution_plan_with_architect_task(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="approved design 기준으로 src/Orders/OrderDetailService.cs 구현",
        summary="execution plan cannot own architect phase",
        plan_kind="execution",
        tasks=[
            {"key": "T1", "title": "architect", "body": "mapping", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "body": "implement", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "body": "review", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T3"]},
        ],
        contract={
            "design_artifact": ".soul/artifacts/design/approved_order_detail_design.md",
            "implementation_paths": ["src/Orders/OrderDetailService.cs"],
        },
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert "plan kind" in result["error"].lower()
    assert any("execution plan" in err.lower() and "architect" in err.lower() for err in result.get("validation_errors", []))


def test_execution_plan_contract_uses_explicit_design_artifact_reference(tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_execution_kind_contract",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": []},
            {"key": "T2", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T1"]},
            {"key": "T3", "title": "final", "assignee": "project_manager", "parents": ["T2"]},
        ],
        plan_kind="execution",
        contract={
            "design_artifact": ".soul/artifacts/design/approved_order_detail_design.md",
            "implementation_paths": [
                "src/Orders/OrderDetailContracts.cs",
                "tests/Orders.Tests/OrderDetailSummaryCalculatorTests.cs",
            ],
        },
    )
    built = pm._build_workflow_contract(plan)
    assert built["plan_kind"] == "execution"
    assert built["design_artifact"] == ".soul/artifacts/design/approved_order_detail_design.md"
    assert built["review"]["design_artifact"] == ".soul/artifacts/design/approved_order_detail_design.md"


def test_pm_create_plan_rejects_execution_plan_when_design_artifact_code_family_mismatches_scope(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    design_path = workspace / ".soul" / "artifacts" / "design" / "approved_auth_design.md"
    design_path.parent.mkdir(parents=True, exist_ok=True)
    design_path.write_text(
        """
        승인된 설계 근거
        - src/Auth/LoginService.cs
        - tests/Auth.Tests/LoginServiceTests.cs
        """.strip(),
        encoding="utf-8",
    )
    result = json.loads(pm.pm_create_plan(
        request="승인된 설계를 기준으로 인증 기능을 구현",
        summary="design artifact와 다른 코드 family를 scope로 내보내면 안 된다",
        plan_kind="execution",
        tasks=[
            {"key": "T1", "title": "implementer", "body": "implement", "assignee": "backend-specialist", "mode": "implementer", "parents": []},
            {"key": "T2", "title": "reviewer", "body": "review", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T1"]},
            {"key": "T3", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T2"]},
        ],
        contract={
            "design_artifact": ".soul/artifacts/design/approved_auth_design.md",
            "implementation_paths": [
                "src/auth/auth.controller.ts",
                "test/auth/auth.integration.spec.ts",
            ],
        },
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert any("extension family" in err for err in result.get("validation_errors", []))


def test_export_approved_scope_rejects_unknown_phase(tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_invalid_phase_export",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
        ],
        plan_kind="design",
        design_status="approved",
        design_approved_at=123,
        design_approved_by="user",
        design_artifact_path=".soul/artifacts/design/plan_invalid_phase_export_design.md",
    )
    result = pm._export_approved_scope(plan, "bogus_phase", "t1", approved_by="user", approved_at=123)
    assert result["ok"] is False
    assert "phase must be one of" in result["error"]


def test_export_approved_scope_requires_design_approval_for_implementer_phase(tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_missing_design_approval_export",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": []},
            {"key": "T2", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T1"]},
            {"key": "T3", "title": "final", "assignee": "project_manager", "parents": ["T2"]},
        ],
        plan_kind="execution",
        contract={
            "design_artifact": ".soul/artifacts/design/approved_order_detail_design.md",
            "implementation_paths": [
                "src/Orders/OrderDetailService.cs",
                "tests/Orders.Tests/OrderDetailServiceTests.cs",
            ],
        },
    )
    result = pm._export_approved_scope(plan, "implementer", "t1", approved_by="user", approved_at=123)
    assert result["ok"] is False
    assert "requires design_approved_at" in result["error"]


def test_export_approved_scope_rejects_implementer_phase_without_concrete_allowed_paths(tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_export_docs_only_scope",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": []},
            {"key": "T2", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T1"]},
            {"key": "T3", "title": "final", "assignee": "project_manager", "parents": ["T2"]},
        ],
        plan_kind="execution",
        design_status="approved",
        design_approved_at=123,
        design_approved_by="user",
        contract={
            "design_artifact": ".soul/artifacts/design/approved_order_detail_design.md",
            "implementation_paths": ["src/Orders/OrderDetailService.cs"],
            "phase_allowed_paths": {
                "architect": [".soul/artifacts/design/approved_order_detail_design.md"],
                "implementer": ["docs/design/order-detail.md", ".soul/artifacts/implementation/plan_export_docs_only_scope_implementation.md"],
                "reviewer": [".soul/artifacts/verification/plan_export_docs_only_scope_verification.md"],
                "final": [".soul/artifacts/final/plan_export_docs_only_scope_final_report.md"],
            },
        },
    )
    result = pm._export_approved_scope(plan, "implementer", "t1", approved_by="user", approved_at=123)
    assert result["ok"] is False
    assert "concrete non-doc allowed path" in result["error"]


def test_export_approved_scope_requires_non_empty_required_evidence_for_implementer_phase(tmp_path):
    workspace = _workspace(tmp_path)
    plan = _base_plan(
        "plan_export_missing_required_evidence",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "implementer", "assignee": "backend-specialist", "mode": "implementer", "parents": []},
            {"key": "T2", "title": "reviewer", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T1"]},
            {"key": "T3", "title": "final", "assignee": "project_manager", "parents": ["T2"]},
        ],
        plan_kind="execution",
        design_status="approved",
        design_approved_at=123,
        design_approved_by="user",
        contract={
            "design_artifact": "",
            "implementation_paths": ["src/Orders/OrderDetailService.cs"],
            "required_evidence_by_phase": {
                "architect": [],
                "implementation": [],
                "reviewer": [".soul/artifacts/verification/plan_export_missing_required_evidence_verification.md"],
                "final": [".soul/artifacts/final/plan_export_missing_required_evidence_final_report.md"],
            },
        },
    )
    result = pm._export_approved_scope(plan, "implementer", "t1", approved_by="user", approved_at=123)
    assert result["ok"] is False
    assert "approved_scope export blocked by workflow contract invariants" in result["error"]


def test_pm_create_plan_rejects_mapping_plan_without_source_plan_id(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="승인된 설계를 실제 구현 파일로 매핑",
        summary="mapping plan requires approved source",
        plan_kind="mapping",
        tasks=[
            {"key": "T1", "title": "mapping architect", "body": "map approved design to files", "assignee": "backend-specialist", "mode": "architect", "parents": []},
        ],
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert "plan kind" in result["error"].lower()
    assert any("source_plan_id" in err for err in result.get("validation_errors", []))


def test_pm_create_plan_rejects_mapping_plan_with_implementer_task(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    data = pm._load_plans()
    source_plan = _base_plan(
        "plan_design_source",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
        ],
        plan_kind="design",
        status="awaiting_design_approval",
        design_status="approved",
        design_approved_at=1710000000,
        design_approved_by="user",
        design_artifact_path=".soul/artifacts/design/approved_design_source.md",
    )
    data.setdefault("plans", {})[source_plan["plan_id"]] = source_plan
    pm._save_plans(data)

    result = json.loads(pm.pm_create_plan(
        request="승인된 설계를 실제 구현 파일로 매핑",
        summary="mapping plan must stay architect-only",
        plan_kind="mapping",
        tasks=[
            {"key": "T1", "title": "mapping architect", "body": "map approved design to files", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "body": "implement", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
        ],
        contract={"source_plan_id": "plan_design_source"},
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert "plan kind" in result["error"].lower()
    assert any("mapping plan" in err.lower() and "implementer" in err.lower() for err in result.get("validation_errors", []))


def test_pm_create_plan_rejects_execution_plan_with_unapproved_source_plan(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    data = pm._load_plans()
    source_plan = _base_plan(
        "plan_unapproved_design_source",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
        ],
        plan_kind="design",
        status="awaiting_approval",
        design_status="not_started",
        design_artifact_path=".soul/artifacts/design/plan_unapproved_design_source_design.md",
    )
    data.setdefault("plans", {})[source_plan["plan_id"]] = source_plan
    pm._save_plans(data)

    result = json.loads(pm.pm_create_plan(
        request="승인되지 않은 설계안으로 실행 계획 생성",
        summary="execution should reject unapproved source plan",
        plan_kind="execution",
        tasks=[
            {"key": "T1", "title": "implementer", "body": "implement", "assignee": "backend-specialist", "mode": "implementer", "parents": []},
            {"key": "T2", "title": "reviewer", "body": "review", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T1"]},
            {"key": "T3", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T2"]},
        ],
        contract={"source_plan_id": "plan_unapproved_design_source"},
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert "plan kind" in result["error"].lower()
    assert any("approved" in err.lower() and "source_plan_id" in err for err in result.get("validation_errors", []))


def test_execution_plan_inherits_design_and_implementation_scope_from_mapping_source(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    data = pm._load_plans()
    source_plan = _base_plan(
        "plan_mapping_source",
        str(workspace),
        tasks=[
            {"key": "T1", "title": "mapping architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
        ],
        plan_kind="mapping",
        status="awaiting_design_approval",
        design_status="approved",
        design_approved_at=1710000000,
        design_approved_by="user",
        design_artifact_path=".soul/artifacts/design/approved_order_detail_mapping.md",
        contract={
            "source_plan_id": "plan_design_source",
            "implementation_paths": [
                "src/Orders/OrderDetailService.cs",
                "tests/Orders.Tests/OrderDetailServiceTests.cs",
            ],
        },
    )
    data.setdefault("plans", {})[source_plan["plan_id"]] = source_plan
    pm._save_plans(data)

    result = json.loads(pm.pm_create_plan(
        request="승인된 매핑을 기준으로 주문 상세 summary 구현",
        summary="execution plan should inherit mapping scope",
        plan_kind="execution",
        tasks=[
            {"key": "T1", "title": "implementer", "body": "implement approved mapping", "assignee": "backend-specialist", "mode": "implementer", "parents": []},
            {"key": "T2", "title": "reviewer", "body": "review approved mapping", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T1"]},
            {"key": "T3", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T2"]},
        ],
        contract={"source_plan_id": "plan_mapping_source"},
        project_path=str(workspace),
    ))
    assert result["ok"] is True
    plan = pm._load_plans()["plans"][result["plan_id"]]
    built = pm._build_workflow_contract(plan)
    assert built["design_artifact"] == ".soul/artifacts/design/approved_order_detail_mapping.md"
    assert built["implementation_paths"] == [
        "src/Orders/OrderDetailService.cs",
        "tests/Orders.Tests/OrderDetailServiceTests.cs",
    ]
    assert plan["contract"]["source_plan_id"] == "plan_mapping_source"


def test_pm_create_plan_rejects_code_reviewer_contract_without_concrete_implementation_paths(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="Update src/Orders/OrderDetailService.cs and validate the contract.",
        summary="bad code review contract",
        tasks=[
            {"key": "T1", "title": "architect", "body": "design", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "implementer", "body": "implement", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T1"]},
            {"key": "T3", "title": "reviewer", "body": "code review", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T2"]},
            {"key": "T4", "title": "final", "body": "report", "assignee": "project_manager", "parents": ["T3"]},
        ],
        contract={
            "expected_deliverables": ["docs/specs/order-detail-summary.md"],
            "implementation_paths": [".soul/artifacts/reports/order-detail-summary-validation.md"],
        },
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert "staged workflow contract" in result["error"]
    assert any("non-.soul allowed path" in err or "implementation_paths" in err for err in result.get("validation_errors", []))


def test_pm_create_plan_rejects_design_approval_pm_task_reused_as_final(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="Implement bracket validator at algorithms/bracket_validator.py.",
        summary="pm final misclassification",
        tasks=[
            {"key": "T1", "title": "architect", "body": "design", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "design approval request", "body": "summarize design for user approval", "assignee": "project_manager", "parents": ["T1"]},
            {"key": "T3", "title": "implementer", "body": "implement", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T2"]},
            {"key": "T4", "title": "reviewer", "body": "review", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T3"]},
        ],
        contract={"expected_deliverables": ["algorithms/bracket_validator.py"]},
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert "staged workflow contract" in result["error"]
    assert any("separate project_manager final/synthesis task downstream of reviewer completion" in err for err in result.get("validation_errors", []))


def test_final_phase_task_keys_skip_preimplementation_pm_summary_with_downstream_work():
    tasks = [
        {"key": "T1", "title": "architect", "assignee": "backend-specialist", "mode": "architect", "parents": []},
        {"key": "T2", "title": "design review", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T1"]},
        {"key": "T3", "title": "Final PM synthesis after reviewer output", "assignee": "project_manager", "parents": ["T2"]},
        {"key": "T4", "title": "implement", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T3"]},
        {"key": "T5", "title": "review impl", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T4"]},
        {"key": "T6", "title": "true final", "assignee": "project_manager", "parents": ["T5"]},
    ]
    assert pm._final_phase_task_keys(tasks, ["src/Auth/LoginService.cs"]) == ["T6"]



def test_pm_create_plan_rejects_nonterminal_pm_task_marked_final_synthesis(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_plan(
        request="Extend existing login sample at src/Auth/LoginService.cs.",
        summary="bad explicit PM phase",
        tasks=[
            {"key": "T1", "title": "architect", "body": "design", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "review design", "body": "review", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T1"]},
            {"key": "T3", "title": "premature final", "body": "summary", "assignee": "project_manager", "pm_phase": "final_synthesis", "parents": ["T2"]},
            {"key": "T4", "title": "implement", "body": "implement", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T3"]},
            {"key": "T5", "title": "review impl", "body": "review impl", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T4"]},
            {"key": "T6", "title": "real final", "body": "close", "assignee": "project_manager", "pm_phase": "final_synthesis", "terminal_task": True, "parents": ["T5"]},
        ],
        contract={"expected_deliverables": ["src/Auth/LoginService.cs"]},
        project_path=str(workspace),
    ))
    assert result["ok"] is False
    assert any("pm_phase=final_synthesis tasks must be terminal" in err for err in result.get("validation_errors", []))



def test_pm_create_kanban_workflow_writes_detailed_korean_implementer_body(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_kanban_workflow(
        request="기존 로그인 샘플의 src/Auth/LoginService.cs와 tests/Auth.Tests/LoginServiceTests.cs를 수정해 비밀번호 변경 기능을 추가한다.",
        tasks=[
            {"key": "T1", "title": "구현", "body": "src/Auth/LoginService.cs와 tests/Auth.Tests/LoginServiceTests.cs를 수정해 비밀번호 변경 기능을 구현한다.", "assignee": "backend-specialist", "mode": "implementer", "parents": []},
        ],
        project_path=str(workspace),
    ))
    assert result["ok"] is True
    task_id = result["created_tasks"][0]["task_id"]
    with kb.connect() as conn:
        row = kb.get_task(conn, task_id)
    assert row is not None
    assert "--- PM 작업 지시서 ---" in row.body
    assert "[완료 조건]" in row.body
    assert "[완료 보고서 필수 항목]" in row.body
    assert "src/Auth/LoginService.cs" in row.body
    assert "tests/Auth.Tests/LoginServiceTests.cs" in row.body
    assert "핵심 로직 변경 내용" in row.body
    assert "한국어로 상세히 설명" in result["required_user_report"]



def test_pm_create_kanban_workflow_writes_detailed_korean_pm_design_gate_body(hermes_home, tmp_path):
    workspace = _workspace(tmp_path)
    result = json.loads(pm.pm_create_kanban_workflow(
        request="비밀번호 변경 기능 설계 후 사용자 승인 게이트를 거친다.",
        tasks=[
            {"key": "T1", "title": "설계", "body": "src/Auth/LoginService.cs 기준으로 설계를 작성한다.", "assignee": "backend-specialist", "mode": "architect", "parents": []},
            {"key": "T2", "title": "설계 승인 요약", "body": "설계 결과를 요약하고 사용자 승인 판단 포인트를 정리한다.", "assignee": "project_manager", "parents": ["T1"]},
            {"key": "T3", "title": "구현", "body": "승인 후 구현한다.", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T2"]},
        ],
        project_path=str(workspace),
    ))
    assert result["ok"] is True
    gate_task_id = next(item["task_id"] for item in result["created_tasks"] if item["key"] == "T2")
    with kb.connect() as conn:
        row = kb.get_task(conn, gate_task_id)
    assert row is not None
    assert "PM 단계: design_summary" in row.body
    assert "최종 완료 보고처럼 작성 금지" in row.body
    assert "승인 판단에 필요한 핵심 변경점/영향 범위" in row.body



def test_staged_workflow_full_path_reaches_completed_with_real_scope_exports(hermes_home, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    (workspace / ".soul" / "artifacts" / "final").mkdir(parents=True, exist_ok=True)
    (workspace / ".soul" / "artifacts" / "verification").mkdir(parents=True, exist_ok=True)
    (workspace / "docs" / "specs").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HERMES_HOME", str(hermes_home / "profiles" / "project_manager"))
    monkeypatch.setattr(pm, "_run_worker_preflight", lambda assignees: {"ok": True, "checked": list(assignees)})
    monkeypatch.setattr(pm, "_attempt_preflight_repair", lambda result: result)

    created = json.loads(pm.pm_create_plan(
        request="docs-only staged workflow full e2e",
        summary="staged workflow full e2e smoke",
        tasks=[
            {"key": "T1", "title": "architect", "body": "write design", "assignee": "backend-specialist", "mode": "architect"},
            {"key": "T2", "title": "설계 승인 게이트 확인 및 구현 진행 관리", "body": "wait for user approval then release implementation", "assignee": "project_manager", "parents": ["T1"]},
            {"key": "T3", "title": "implementer", "body": "implement approved change", "assignee": "backend-specialist", "mode": "implementer", "parents": ["T2"]},
            {"key": "T4", "title": "문서 변경 검토 및 제약 준수 확인", "body": "verify approved change", "assignee": "backend-specialist", "mode": "reviewer", "parents": ["T3"]},
            {"key": "T5", "title": "최종 결과 정리 및 사용자 보고", "body": "report final result", "assignee": "project_manager", "parents": ["T4"]},
        ],
        contract={
            "expected_deliverables": ["docs/specs/test-deliverable.md"],
            "implementation_paths": ["docs/specs/test-deliverable.md"],
        },
        acceptance_criteria=["deliverable created"],
        validation_plan=["reviewer verifies deliverable"],
        project_path=str(workspace),
        plan_kind="staged",
    ))
    assert created["ok"] is True

    plan_id = created["plan_id"]
    phase1 = json.loads(pm.pm_execute_plan(plan_id, approved_by="user"))
    assert phase1["ok"] is True
    assert phase1["plan_status"] == "design_in_progress"

    architect = next(item for item in phase1["created_design_tasks"] if item["key"] == "T1")
    with kb.connect() as conn:
        assert _complete_with_claude(conn, workspace, plan_id, architect["task_id"], "design artifact ready")

    after_design = pm._load_plans()["plans"][plan_id]
    assert after_design["status"] == "awaiting_design_approval"
    assert after_design["design_status"] == "awaiting_approval"

    phase2 = json.loads(pm.pm_approve_design_and_execute(
        plan_id,
        approved_by="user",
        design_artifact_path=".soul/artifacts/design/e2e_design.md",
    ))
    assert phase2["ok"] is True
    assert phase2["plan_status"] == "executed"
    assert phase2["design_gate_completion"]["ok"] is True

    followups = {item["key"]: item for item in phase2["created_followup_tasks"]}
    with kb.connect() as conn:
        assert kb.get_task(conn, followups["T2"]["task_id"]).status == "done"
        assert kb.get_task(conn, followups["T3"]["task_id"]).status == "ready"
        assert kb.get_task(conn, followups["T4"]["task_id"]).status == "todo"
        assert kb.get_task(conn, followups["T5"]["task_id"]).status == "todo"

        deliverable = workspace / "docs" / "specs" / "test-deliverable.md"
        deliverable.write_text("implemented\n", encoding="utf-8")
        verification = workspace / ".soul" / "artifacts" / "verification" / f"{plan_id}_verification.md"
        verification.write_text("# Verification\n\npass\n", encoding="utf-8")

        assert _complete_with_claude(conn, workspace, plan_id, followups["T3"]["task_id"], "implemented deliverable", "implementer")

        after_impl = pm._load_plans()["plans"][plan_id]
        assert after_impl["approved_scope_export"]["ok"] is True
        assert after_impl["approved_scope_export"]["phase"] == "reviewer"

        assert kb.complete_task(
            conn,
            followups["T4"]["task_id"],
            summary="reviewed deliverable",
            metadata={
                "verdict": "pass",
                "summary": "All required checks passed.",
                "evidence": [f".soul/artifacts/verification/{plan_id}_verification.md"],
                "changed_files": ["docs/specs/test-deliverable.md"],
                "scope_status": "within_allowed_paths",
                "validated_artifacts": ["docs/specs/test-deliverable.md"],
                "acceptance_checks": [{"criterion": "deliverable created", "status": "pass"}],
                "unmet_requirements": [],
                "design_alignment_summary": "Implementation and deliverables match the approved design spec.",
                "escalation_target": "none",
                "escalation_reason": "",
            },
        )

        after_review = pm._load_plans()["plans"][plan_id]
        assert after_review["approved_scope_export"]["ok"] is True
        assert after_review["approved_scope_export"]["phase"] == "final"

        final_report_rel = f".soul/artifacts/final/{plan_id}_final_report.md"
        final_report = workspace / final_report_rel
        final_report.write_text(
            "# Final Report\n\n## Summary\ncompleted\n\n## Delivered Artifacts\n- docs/specs/test-deliverable.md\n\n## Open Risks\n- none\n",
            encoding="utf-8",
        )

        assert kb.complete_task(
            conn,
            followups["T5"]["task_id"],
            summary="final report delivered",
            metadata={
                "report_summary": "Workflow changes completed and validated.",
                "user_report": "All requested work is complete. Reviewer approval and evidence are attached. Implementation and deliverables match the approved design spec.",
                "delivered_artifacts": [
                    final_report_rel,
                    f".soul/artifacts/verification/{plan_id}_verification.md",
                ],
                "open_risks": [],
                "completion_status": "completed",
            },
        )

        assert kb.get_task(conn, followups["T5"]["task_id"]).status == "done"

    saved = pm._load_plans()["plans"][plan_id]
    assert saved["status"] == "completed"
    assert saved["design_status"] == "approved"
    assert saved["review_status"] == "approved"
    assert saved["final_status"] == "completed"
    assert saved["final_summary"] == "Workflow changes completed and validated."
