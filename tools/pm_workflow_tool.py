"""PM workflow enforcement tools for Slack-facing project_manager profiles.

This module enforces a staged PM workflow:
1. pm_create_plan: store a PM plan awaiting explicit user plan approval without creating Kanban cards.
2. pm_execute_plan: after PM plan approval, create architect/design Kanban cards only.
3. pm_mark_design_ready: persist architect design evidence and move to user design approval.
4. pm_approve_design_and_execute: after user design approval, create implementation/review/final Kanban cards.

The raw Kanban creator remains an internal helper and is intentionally registered
under an internal toolset so Slack PM sessions cannot bypass plan approval.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tools.registry import registry

_DEDICATED_WORKER_MODES_BY_ASSIGNEE = {
    "backend-architect": {"architect"},
    "backend-implementer": {"implementer", "debugger"},
    "backend-reviewer": {"reviewer"},
}
_LEGACY_MULTI_MODE_WORKER = "backend-specialist"
_WORKER_ASSIGNEES = set(_DEDICATED_WORKER_MODES_BY_ASSIGNEE) | {_LEGACY_MULTI_MODE_WORKER}
_ALLOWED_ASSIGNEES = _WORKER_ASSIGNEES | {"project_manager"}
_ACTIVE_STATUSES = ("running", "ready", "todo", "blocked")
_INTERNAL_TOOLSET = "pm_workflow_internal"
_PUBLIC_TOOLSET = "pm_workflow"
_WORKER_MODES = {"architect", "implementer", "debugger", "reviewer"}
_PM_TASK_PHASES = {"design_summary", "design_approval_gate", "final_synthesis"}
_PLAN_KINDS = {"staged", "design", "mapping", "execution"}
# Assignees that require a live worker preflight before card creation.
_PREFLIGHT_REQUIRED_ASSIGNEES = set(_WORKER_ASSIGNEES)


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _error(message: str, **extra: Any) -> str:
    return _json({"ok": False, "error": message, **extra})


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def _plans_path(profile: str | None = None) -> Path:
    if profile:
        return _profile_home(profile) / "state" / "pm_plans.json"
    return _hermes_home() / "state" / "pm_plans.json"


def _load_plans(profile: str | None = None) -> dict[str, Any]:
    path = _plans_path(profile)
    if not path.exists():
        return {"plans": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"plans": {}}
        plans = data.get("plans")
        if not isinstance(plans, dict):
            data["plans"] = {}
        return data
    except Exception:
        return {"plans": {}}


def _save_plans(data: dict[str, Any], profile: str | None = None) -> None:
    path = _plans_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _new_plan_id() -> str:
    return f"plan_{secrets.token_hex(4)}"


def _load_terminal_cwd() -> str | None:
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        cwd = (cfg.get("terminal") or {}).get("cwd")
        if cwd and str(cwd).strip() != ".":
            return str(cwd).strip()
    except Exception:
        pass
    return None


def _hermes_root() -> Path:
    home = _hermes_home()
    if home.parent.name == "profiles":
        return home.parent.parent
    if home.name == ".hermes":
        return home
    return Path.home() / ".hermes"


def _profile_home(profile: str) -> Path:
    profile = str(profile or "").strip()
    root = _hermes_root()
    if not profile or profile == "default":
        return root
    return root / "profiles" / profile


def _resolve_hermes_argv() -> list[str]:
    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        return [hermes_bin]
    return [sys.executable, "-m", "hermes_cli.main"]


def _profile_env(profile: str) -> dict[str, str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(_profile_home(profile))
    env["HERMES_PROFILE"] = str(profile)
    return env


def _run_profile_command(profile: str, args: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_resolve_hermes_argv(), "-p", profile, *args],
        env=_profile_env(profile),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _auth_snapshot(assignee: str) -> dict[str, Any]:
    """Advisory auth snapshot for a worker. Never decides pass/fail."""
    path = _profile_home(assignee) / "auth.json"
    if not path.exists():
        return {
            "assignee": assignee,
            "auth_ok": False,
            "auth_file": str(path),
            "error": "auth.json not found",
        }
    data = _read_json_file(path)
    if not isinstance(data, dict):
        return {
            "assignee": assignee,
            "auth_ok": False,
            "auth_file": str(path),
            "error": "auth.json unreadable",
        }
    providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
    pool = data.get("credential_pool") if isinstance(data.get("credential_pool"), dict) else {}
    entries = pool.get("openai-codex") if isinstance(pool.get("openai-codex"), list) else []
    invalidated = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or entry.get("state") or "").lower()
        code = str(entry.get("error_code") or entry.get("code") or "").lower()
        if "invalid" in status or "revok" in status or "invalid" in code or "revok" in code:
            invalidated += 1
    auth_ok = bool(providers.get("openai-codex") or entries)
    return {
        "assignee": assignee,
        "auth_ok": auth_ok,
        "auth_file": str(path),
        "active_provider": data.get("active_provider"),
        "pool_entries": len(entries),
        "invalidated_entries": invalidated,
    }


def _quick_chat_probe(assignee: str) -> dict[str, Any]:
    """Quick chat probe -- source of truth for worker readiness."""
    try:
        result = _run_profile_command(
            assignee,
            ["chat", "-q", "짧게 OK만 답해.", "--quiet"],
            timeout=180,
        )
    except Exception as exc:
        return {"assignee": assignee, "ok": False, "reason": str(exc)}
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    ok = result.returncode == 0 and bool(stdout)
    return {
        "assignee": assignee,
        "ok": ok,
        "returncode": result.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "reason": None if ok else (stderr or stdout or f"exit_code={result.returncode}"),
    }


def _gateway_health_check(assignee: str | None = None) -> dict[str, Any]:
    """Gateway restart/status/health probe for one profile."""
    profile = str(assignee or "").strip()
    if not profile:
        return {"ok": False, "pid": None, "reason": "assignee is required"}
    restart_stdout = ""
    restart_stderr = ""
    status_stdout = ""
    status_stderr = ""
    restart_code = None
    status_code = None
    try:
        restarted = _run_profile_command(profile, ["gateway", "restart"], timeout=240)
        restart_code = restarted.returncode
        restart_stdout = (restarted.stdout or "").strip()
        restart_stderr = (restarted.stderr or "").strip()
    except Exception as exc:
        restart_stderr = str(exc)
    try:
        status = _run_profile_command(profile, ["gateway", "status"], timeout=120)
        status_code = status.returncode
        status_stdout = (status.stdout or "").strip()
        status_stderr = (status.stderr or "").strip()
    except Exception as exc:
        status_stderr = str(exc)
    runtime = _read_json_file(_profile_home(profile) / "gateway_state.json") or {}
    platforms = runtime.get("platforms") if isinstance(runtime.get("platforms"), dict) else {}
    fatal_platforms = sorted(name for name, pdata in platforms.items() if isinstance(pdata, dict) and pdata.get("state") == "fatal")
    gateway_state = runtime.get("gateway_state")
    ok = restart_code == 0 and status_code == 0 and gateway_state == "running" and not fatal_platforms
    reason_parts = []
    if restart_code not in (None, 0):
        reason_parts.append(f"restart exit {restart_code}")
    if status_code not in (None, 0):
        reason_parts.append(f"status exit {status_code}")
    if gateway_state and gateway_state != "running":
        reason_parts.append(f"gateway_state={gateway_state}")
    if fatal_platforms:
        reason_parts.append(f"fatal platforms: {', '.join(fatal_platforms)}")
    return {
        "assignee": profile,
        "ok": ok,
        "pid": runtime.get("pid"),
        "gateway_state": gateway_state,
        "fatal_platforms": fatal_platforms,
        "runtime": runtime,
        "restart_exit_code": restart_code,
        "status_exit_code": status_code,
        "restart_stdout": restart_stdout,
        "restart_stderr": restart_stderr,
        "status_stdout": status_stdout,
        "status_stderr": status_stderr,
        "reason": None if ok else ("; ".join(reason_parts) or status_stderr or restart_stderr or "gateway unhealthy"),
    }


def _restart_gateway_only(assignee: str) -> dict[str, Any]:
    try:
        result = _run_profile_command(assignee, ["gateway", "restart"], timeout=240)
        return {
            "assignee": assignee,
            "attempted": True,
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
        }
    except Exception as exc:
        return {"assignee": assignee, "attempted": True, "ok": False, "error": str(exc)}


def _sync_codex_auth_from_default(assignee: str) -> dict[str, Any]:
    if assignee not in _WORKER_ASSIGNEES:
        return {"assignee": assignee, "attempted": False, "reason": "not a repairable worker profile"}
    src = _profile_home("default") / "auth.json"
    dst = _profile_home(assignee) / "auth.json"
    src_data = _read_json_file(src)
    if not isinstance(src_data, dict):
        return {"assignee": assignee, "attempted": False, "reason": "default auth store missing or unreadable"}
    src_provider = (src_data.get("providers") or {}).get("openai-codex")
    src_pool = (src_data.get("credential_pool") or {}).get("openai-codex")
    if not src_provider and not src_pool:
        return {"assignee": assignee, "attempted": False, "reason": "default profile has no openai-codex auth to copy"}
    dst_data = _read_json_file(dst) if dst.exists() else {}
    if not isinstance(dst_data, dict):
        dst_data = {}
    backup = None
    if dst.exists():
        backup = dst.with_name(f"auth.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(dst, backup)
    providers = dst_data.setdefault("providers", {})
    pool = dst_data.setdefault("credential_pool", {})
    if src_provider is not None:
        providers["openai-codex"] = src_provider
    if src_pool is not None:
        pool["openai-codex"] = src_pool
    dst_data["active_provider"] = "openai-codex"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(dst_data, ensure_ascii=False, indent=2), encoding="utf-8")
    dst.chmod(0o600)
    return {
        "assignee": assignee,
        "attempted": True,
        "ok": True,
        "auth_file": str(dst),
        "backup": str(backup) if backup else None,
    }


def _run_worker_preflight(assignees: list[str]) -> dict[str, Any]:
    """Run preflight checks for required assignees.

    Quick chat is the source of truth for worker readiness. Gateway health is
    captured as advisory context because Kanban workers are spawned as profile
    chat processes and dedicated worker profiles do not need their own Slack
    gateway identity.
    """
    required = sorted({a for a in assignees if a in _WORKER_ASSIGNEES})
    if not required:
        return {"ok": True, "required": [], "results": {}, "gateway": {"ok": True}}

    results: dict[str, dict[str, Any]] = {}
    all_ok = True
    gateway_ok = True
    for assignee in required:
        auth = _auth_snapshot(assignee)
        chat = _quick_chat_probe(assignee)
        if assignee == _LEGACY_MULTI_MODE_WORKER:
            gateway = _gateway_health_check(assignee)
        else:
            gateway = {
                "assignee": assignee,
                "ok": True,
                "attempted": False,
                "reason": "dedicated Kanban worker profile: gateway not required for profile chat dispatch",
            }
        worker_ok = bool(chat.get("ok"))
        if not worker_ok:
            all_ok = False
        if not gateway.get("ok"):
            gateway_ok = False
        results[assignee] = {
            "auth_snapshot": auth,
            "chat_probe": chat,
            "gateway": gateway,
            "ok": worker_ok,
        }

    return {
        "ok": all_ok,
        "required": required,
        "results": results,
        "gateway": {"ok": gateway_ok},
    }


def _attempt_preflight_repair(preflight: dict[str, Any]) -> dict[str, Any]:
    """Best-effort repair for failed preflight, then re-verify."""
    failed = [a for a, r in preflight.get("results", {}).items() if not r.get("ok")]
    if not failed:
        repaired = dict(preflight)
        repaired.setdefault("repair_actions", [])
        return repaired

    repair_actions: list[dict[str, Any]] = []
    for assignee in failed:
        if assignee == _LEGACY_MULTI_MODE_WORKER:
            repair_actions.append({"gateway_restart": _restart_gateway_only(assignee)})
        else:
            repair_actions.append({
                "gateway_restart": {
                    "assignee": assignee,
                    "attempted": False,
                    "reason": "dedicated Kanban worker profile: gateway repair not required",
                }
            })
        worker = preflight.get("results", {}).get(assignee, {})
        auth = worker.get("auth_snapshot", {})
        chat = worker.get("chat_probe", {})
        if not chat.get("ok") or not auth.get("auth_ok"):
            repair_actions.append({"auth_sync": _sync_codex_auth_from_default(assignee)})

    reverify = _run_worker_preflight(failed)
    reverify["repair_actions"] = repair_actions
    reverify["original_failed"] = failed
    return reverify


def _collect_workload(kb: Any, conn: Any, assignees: list[str]) -> dict[str, Any]:
    workload: dict[str, Any] = {}
    for assignee in assignees:
        items: list[dict[str, Any]] = []
        counts = {s: 0 for s in _ACTIVE_STATUSES}
        for status in _ACTIVE_STATUSES:
            tasks = kb.list_tasks(conn, assignee=assignee, status=status, limit=20)
            counts[status] = len(tasks)
            for t in tasks[:8]:
                items.append({"id": t.id, "title": t.title, "status": t.status})
        workload[assignee] = {"counts": counts, "active_sample": items[:12]}
    return workload


def _is_worker_assignee(assignee: str) -> bool:
    return str(assignee or "").strip() in _WORKER_ASSIGNEES


def _task_mode_allowed_for_assignee(assignee: str, mode: str) -> tuple[bool, str | None]:
    assignee = str(assignee or "").strip()
    mode = str(mode or "").strip()
    allowed = _DEDICATED_WORKER_MODES_BY_ASSIGNEE.get(assignee)
    if allowed and mode not in allowed:
        return False, f"{assignee} tasks must use mode in {sorted(allowed)}; got {mode!r}"
    return True, None


def _is_architect_task(task: dict[str, Any]) -> bool:
    return _is_worker_assignee(str(task.get("assignee") or "")) and str(task.get("mode") or "").strip() == "architect"


def _split_design_tasks(tasks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    design_tasks: list[dict[str, Any]] = []
    followup_tasks: list[dict[str, Any]] = []
    for task in tasks:
        cloned = dict(task)
        if _is_architect_task(cloned):
            design_tasks.append(cloned)
        else:
            followup_tasks.append(cloned)
    return design_tasks, followup_tasks


def _normalize_plan_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "staged"
    return text


def _plan_kind(plan: dict[str, Any]) -> str:
    return _normalize_plan_kind(plan.get("plan_kind"))


def _explicit_design_artifact(plan: dict[str, Any]) -> str:
    contract = plan.get("contract") if isinstance(plan.get("contract"), dict) else {}
    return str(plan.get("design_artifact_path") or contract.get("design_artifact") or "").strip()


def _project_relative_path(project_path: str, path: str) -> str:
    """Return a normalized project-relative path when ``path`` is inside ``project_path``."""
    text = str(path or "").strip()
    if not text:
        return ""
    root_text = str(project_path or "").strip()
    candidate = Path(text)
    if candidate.is_absolute() and root_text:
        try:
            return _normalize_contract_path(candidate.resolve().relative_to(Path(root_text).resolve()).as_posix())
        except Exception:
            return ""
    return _normalize_contract_path(text)


def _completed_design_task_artifact_paths(plan: dict[str, Any]) -> list[str]:
    """Find design artifacts recorded by completed architect Kanban task runs."""
    project_path = str(plan.get("project_path") or "").strip()
    task_ids = [
        str(item.get("task_id") or "").strip()
        for item in (plan.get("created_design_tasks") or [])
        if isinstance(item, dict) and str(item.get("task_id") or "").strip()
    ]
    if not task_ids:
        return []
    try:
        from hermes_cli import kanban_db as kb

        with contextlib.closing(kb.connect()) as conn:
            candidates: list[str] = []
            for task_id in task_ids:
                for run in kb.list_runs(conn, task_id, include_active=False, state_type="outcome", state_name="completed"):
                    metadata = run.metadata if isinstance(run.metadata, dict) else {}
                    artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), list) else []
                    for artifact in artifacts:
                        rel = _project_relative_path(project_path, str(artifact))
                        if rel and rel.startswith(".soul/artifacts/design/"):
                            candidates.append(rel)
            return _normalize_contract_paths(candidates)
    except Exception:
        return []


def _source_plan_id(plan: dict[str, Any]) -> str:
    contract = plan.get("contract") if isinstance(plan.get("contract"), dict) else {}
    return str(contract.get("source_plan_id") or "").strip()


def _source_plan_is_approved(plan: dict[str, Any]) -> bool:
    plan = _ensure_plan_workflow_fields(dict(plan))
    if str(plan.get("design_status") or "") == "approved":
        return True
    if plan.get("design_approved_at"):
        return True
    if str(plan.get("status") or "") == "executed":
        return True
    return False


def _resolve_source_plan(plan: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    source_plan_id = _source_plan_id(plan)
    if not source_plan_id:
        return None, []
    data = _load_plans()
    source = data.get("plans", {}).get(source_plan_id)
    if not isinstance(source, dict):
        return None, [f"contract.source_plan_id {source_plan_id!r} was not found in PM plan state"]
    source = _ensure_plan_workflow_fields(dict(source))
    source_kind = _plan_kind(source)
    target_kind = _plan_kind(plan)
    allowed_sources = {
        "mapping": {"design", "staged"},
        "execution": {"design", "mapping", "staged"},
    }
    allowed = allowed_sources.get(target_kind)
    if allowed and source_kind not in allowed:
        return None, [f"contract.source_plan_id must reference one of {sorted(allowed)} plans for {target_kind} plans; got {source_kind!r}"]
    if target_kind in {"mapping", "execution"} and not _source_plan_is_approved(source):
        return None, [f"contract.source_plan_id must reference an approved source plan before {target_kind} planning can begin"]
    return source, []


def _inherit_contract_from_source_plan(plan: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    contract = dict(plan.get("contract") or {})
    source_plan, errors = _resolve_source_plan({**plan, "contract": contract})
    if errors or not source_plan:
        return contract, errors
    source_contract = _build_workflow_contract(source_plan)
    if not contract.get("design_artifact"):
        inherited_design = str(source_contract.get("design_artifact") or source_plan.get("design_artifact_path") or "").strip()
        if inherited_design:
            contract["design_artifact"] = inherited_design
    if not contract.get("implementation_paths"):
        inherited_paths = _normalize_contract_paths(source_contract.get("implementation_paths") or [])
        if inherited_paths:
            contract["implementation_paths"] = inherited_paths
    if not contract.get("expected_deliverables"):
        inherited_expected = _normalize_contract_paths(source_contract.get("expected_deliverables") or [])
        if inherited_expected:
            contract["expected_deliverables"] = inherited_expected
    return contract, []


def _resolve_followup_task_parents(
    tasks: list[dict[str, Any]],
    prior_key_to_id: dict[str, str],
) -> tuple[list[dict[str, Any]], str | None]:
    remaining_keys = {str(t.get("key") or "").strip() for t in tasks}
    created_so_far: set[str] = set()
    resolved: list[dict[str, Any]] = []
    for task in tasks:
        cloned = dict(task)
        parents: list[str] = []
        for parent in cloned.get("parents") or []:
            parent = str(parent).strip()
            if not parent:
                continue
            if parent in prior_key_to_id:
                parents.append(prior_key_to_id[parent])
                continue
            if parent in remaining_keys:
                if parent not in created_so_far:
                    return [], f"{cloned.get('key')}: unresolved parent {parent!r}; design-phase parents must be created before follow-up task creation"
                parents.append(parent)
                continue
            parents.append(parent)
        cloned["parents"] = parents
        resolved.append(cloned)
        created_so_far.add(str(cloned.get("key") or "").strip())
    return resolved, None


def _runtime_requires_pm_finalizer(tasks: list[dict[str, Any]]) -> bool:
    if len(tasks) <= 1:
        return False
    for task in tasks:
        assignee = str(task.get("assignee") or "").strip()
        mode = str(task.get("mode") or "").strip()
        if assignee == "project_manager" or mode in {"implementer", "reviewer", "debugger"}:
            return True
    return False


def _normalize_tasks(raw_tasks: Any, *, require_pm_finalizer: bool = True) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return [], "tasks must be a non-empty list"
    seen: set[str] = set()
    tasks: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_tasks, start=1):
        if not isinstance(raw, dict):
            return [], f"tasks[{idx}] must be an object"
        key = str(raw.get("key") or f"T{idx}").strip()
        if not key:
            return [], f"tasks[{idx}].key is empty"
        if key in seen:
            return [], f"duplicate task key: {key}"
        seen.add(key)
        title = str(raw.get("title") or "").strip()
        body = str(raw.get("body") or "").strip()
        assignee = str(raw.get("assignee") or "").strip()
        mode = str(raw.get("mode") or "").strip()
        pm_phase = str(raw.get("pm_phase") or "").strip()
        terminal_task = raw.get("terminal_task")
        if not title:
            return [], f"{key}: title is required"
        if assignee not in _ALLOWED_ASSIGNEES:
            return [], f"{key}: assignee must be one of {sorted(_ALLOWED_ASSIGNEES)}, got {assignee!r}"
        if mode and mode not in _WORKER_MODES:
            return [], f"{key}: mode must be one of {sorted(_WORKER_MODES)}, got {mode!r}"
        if assignee in _WORKER_ASSIGNEES and not mode:
            return [], f"{key}: {assignee} tasks require mode={sorted(_WORKER_MODES)}"
        if assignee in _WORKER_ASSIGNEES and mode:
            ok, mode_error = _task_mode_allowed_for_assignee(assignee, mode)
            if not ok:
                return [], f"{key}: {mode_error}"
        if assignee == "project_manager" and mode:
            return [], f"{key}: project_manager tasks must not set mode"
        if assignee != "project_manager" and pm_phase:
            return [], f"{key}: only project_manager tasks may set pm_phase"
        if pm_phase and pm_phase not in _PM_TASK_PHASES:
            return [], f"{key}: pm_phase must be one of {sorted(_PM_TASK_PHASES)}, got {pm_phase!r}"
        if terminal_task is not None and not isinstance(terminal_task, bool):
            return [], f"{key}: terminal_task must be a boolean when provided"
        parents = raw.get("parents") or []
        if isinstance(parents, str):
            parents = [parents]
        if not isinstance(parents, list):
            return [], f"{key}: parents must be a list of prior task keys or existing task ids"
        parents = [str(p).strip() for p in parents if str(p).strip()]
        priority = raw.get("priority", 0)
        try:
            priority = int(priority)
        except Exception:
            return [], f"{key}: priority must be an integer"
        tasks.append({
            "key": key,
            "title": title,
            "body": body,
            "assignee": assignee,
            "mode": mode or None,
            "pm_phase": pm_phase or None,
            "terminal_task": terminal_task if isinstance(terminal_task, bool) else None,
            "parents": parents,
            "priority": priority,
        })
    task_keys = {t["key"] for t in tasks}
    created_so_far: set[str] = set()
    for t in tasks:
        for p in t["parents"]:
            if p in task_keys and p not in created_so_far:
                return [], f"{t['key']}: parent key {p!r} must refer to a task earlier in the tasks list"
        created_so_far.add(t["key"])
    if len(tasks) > 1 and require_pm_finalizer:
        finalizers = [t for t in tasks if t["assignee"] == "project_manager" and t["parents"]]
        if not finalizers:
            return [], "multi-task workflows must include a project_manager final/synthesis task with parents"
    return tasks, None


def _resolve_workspace(project_path: str | None) -> tuple[str | None, str | None, str | None]:
    # Profile config is authoritative. A model-supplied project_path is fallback
    # only, so the model cannot silently change repositories.
    configured_workspace = _load_terminal_cwd()
    workspace_path = configured_workspace or project_path
    ignored = None
    if configured_workspace and project_path and project_path != configured_workspace:
        ignored = project_path
    return workspace_path, ignored, configured_workspace


_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:\.?[A-Za-z0-9_.-]+/)+(?:[A-Za-z0-9_. -]+\.[A-Za-z0-9_.-]+)")
_ROOT_LAYER_DIRS = {"Domain", "Application", "Infrastructure", "Host", "WebApi", "Api"}
_ALLOWED_EXTRACTED_ROOTS = {"src", "tests", "docs", ".soul"}


def _normalize_contract_path(path: str) -> str:
    text = str(path or "").strip().strip("`'\",;:()[]{}")
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _extract_project_paths(*values: Any) -> list[str]:
    """Best-effort extraction of concrete project artifact paths from PM text."""
    chunks: list[str] = []
    for value in values:
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, list):
            chunks.extend(str(v) for v in value if isinstance(v, (str, int, float)))
        elif isinstance(value, dict):
            chunks.extend(str(v) for v in value.values() if isinstance(v, (str, int, float)))
    paths: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        for match in _PATH_RE.findall(chunk):
            rel = _normalize_contract_path(match)
            if not rel or rel.startswith("/") or rel.startswith("../"):
                continue
            if rel.startswith((".git/", "secrets/")) or rel == ".env":
                continue
            if rel not in seen:
                seen.add(rel)
                paths.append(rel)
    return paths


def _artifact_paths(plan_id: str) -> dict[str, str]:
    return {
        "design": f".soul/artifacts/design/{plan_id}_design.md",
        "implementation": f".soul/artifacts/implementation/{plan_id}_implementation.md",
        "verification": f".soul/artifacts/verification/{plan_id}_verification.md",
        "final": f".soul/artifacts/final/{plan_id}_final_report.md",
    }


def _normalize_contract_paths(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in (values or []):
        text = _normalize_contract_path(raw)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _canonicalize_extracted_project_path(project_path: str, path: str) -> str:
    """Normalize prose-extracted paths into project-relative contract paths.

    Design artifacts often quote absolute WSL paths or solution-relative .NET
    entries like ``Domain/Foo.csproj``.  Contract paths must be relative to the
    project root, so convert those forms and drop malformed prose fragments.
    """
    text = _normalize_contract_path(path).rstrip(".")
    if not text:
        return ""
    root_text = str(project_path or "").strip().replace("\\", "/").rstrip("/")
    if root_text:
        root_no_slash = root_text.lstrip("/")
        if text.startswith(root_text + "/"):
            text = text[len(root_text) + 1:]
        elif text.startswith(root_no_slash + "/"):
            text = text[len(root_no_slash) + 1:]
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return ""
    if parts[0] in _ROOT_LAYER_DIRS:
        parts.insert(0, "src")
    if parts[0] not in _ALLOWED_EXTRACTED_ROOTS:
        return ""
    return _normalize_contract_path("/".join(parts))


def _derive_reviewer_followup_scope_paths(plan: dict[str, Any], reviewer_meta: dict[str, Any]) -> list[str]:
    summary = str(reviewer_meta.get("summary") or "").strip()
    blocked_reason = str(reviewer_meta.get("blocked_reason") or "").strip()
    design_alignment = str(reviewer_meta.get("design_alignment_summary") or "").strip()
    escalation_reason = str(reviewer_meta.get("escalation_reason") or "").strip()
    unmet = [str(item).strip() for item in (reviewer_meta.get("unmet_requirements") or []) if str(item).strip()]
    acceptance_text: list[str] = []
    for item in (reviewer_meta.get("acceptance_checks") or []):
        if isinstance(item, dict):
            acceptance_text.extend(
                str(item.get(key) or "").strip()
                for key in ("criterion", "status", "notes")
                if str(item.get(key) or "").strip()
            )
        elif str(item).strip():
            acceptance_text.append(str(item).strip())
    extracted = _extract_project_paths(summary, blocked_reason, design_alignment, escalation_reason, unmet, acceptance_text)
    text_blob = "\n".join([summary, blocked_reason, design_alignment, escalation_reason, *unmet, *acceptance_text]).lower()
    if "solution wiring" in text_blob or ".sln" in text_blob or ".slnx" in text_blob:
        contract = _build_workflow_contract(plan)
        project_path = str(plan.get("project_path") or "").strip()
        design_artifact = str(contract.get("design_artifact") or "").strip()
        design_paths = _extract_design_artifact_concrete_paths(project_path, design_artifact)
        solution_paths = [path for path in design_paths if str(path).lower().endswith((".sln", ".slnx"))]
        if not solution_paths and project_path and design_artifact:
            artifact_text = _read_project_relative_text(project_path, design_artifact)
            root_solution_matches = re.findall(r"(?<![A-Za-z0-9_/.-])([A-Za-z0-9_. -]+\.(?:sln|slnx))", artifact_text, flags=re.IGNORECASE)
            solution_paths.extend(_normalize_contract_paths(root_solution_matches))
        extracted.extend(solution_paths)
    allowed: list[str] = []
    for path in _normalize_contract_paths(extracted):
        lowered = str(path).lower()
        if _is_concrete_implementation_path(path) or lowered.endswith((".sln", ".slnx")):
            allowed.append(path)
    return _normalize_contract_paths(allowed)


def _widen_plan_contract_for_reviewer_followup(plan: dict[str, Any], added_paths: list[str]) -> dict[str, Any]:
    normalized = _normalize_contract_paths(added_paths)
    if not normalized:
        return {"ok": True, "updated": False, "added_paths": []}
    explicit = plan.get("contract") if isinstance(plan.get("contract"), dict) else {}
    if not isinstance(plan.get("contract"), dict):
        plan["contract"] = explicit
    existing_impl = _normalize_contract_paths(explicit.get("implementation_paths") or [])
    existing_expected = _normalize_contract_paths(explicit.get("expected_deliverables") or [])
    merged_impl = _normalize_contract_paths([*existing_impl, *normalized])
    merged_expected = _normalize_contract_paths([*existing_expected, *normalized])
    explicit["implementation_paths"] = merged_impl
    explicit["expected_deliverables"] = merged_expected

    phase_allowed = explicit.get("phase_allowed_paths") if isinstance(explicit.get("phase_allowed_paths"), dict) else None
    if phase_allowed is not None:
        for phase in ("implementer", "reviewer", "final"):
            phase_allowed[phase] = _normalize_contract_paths([*(phase_allowed.get(phase) or []), *normalized])

    required_evidence = explicit.get("required_evidence_by_phase") if isinstance(explicit.get("required_evidence_by_phase"), dict) else None
    if required_evidence is not None:
        for phase in ("implementation", "reviewer", "final"):
            required_evidence[phase] = _normalize_contract_paths([*(required_evidence.get(phase) or []), *normalized])

    return {
        "ok": True,
        "updated": merged_impl != existing_impl or merged_expected != existing_expected,
        "added_paths": [path for path in normalized if path not in existing_impl or path not in existing_expected],
    }


def _is_docs_like_path(path: str) -> bool:
    text = str(path or "").strip().lower()
    if not text:
        return False
    return text.endswith((".md", ".html", ".txt", ".rst")) or text.startswith("docs/")


def _infer_review_mode(expected_deliverables: list[str]) -> str:
    deliverables = [str(p).strip().lower() for p in expected_deliverables if str(p or "").strip()]
    if deliverables and all(_is_docs_like_path(p) for p in deliverables):
        return "docs"
    return "code"


def _select_implementation_paths(explicit_contract: dict[str, Any], expected_deliverables: list[str]) -> list[str]:
    explicit_paths = _normalize_contract_paths(explicit_contract.get("implementation_paths") or [])
    if explicit_paths:
        return explicit_paths
    return [path for path in _normalize_contract_paths(expected_deliverables) if not path.startswith(".soul/")]


def _depends_on_any_task_key(task: dict[str, Any], task_by_key: dict[str, dict[str, Any]], target_keys: set[str]) -> bool:
    pending = [str(parent).strip() for parent in (task.get("parents") or []) if str(parent).strip()]
    seen: set[str] = set()
    while pending:
        parent = pending.pop()
        if parent in target_keys:
            return True
        if parent in seen:
            continue
        seen.add(parent)
        parent_task = task_by_key.get(parent)
        if parent_task:
            pending.extend(str(next_parent).strip() for next_parent in (parent_task.get("parents") or []) if str(next_parent).strip())
    return False


def _task_children_map(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    children: dict[str, set[str]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        key = str(task.get("key") or "").strip()
        if not key:
            continue
        children.setdefault(key, set())
        for parent in (task.get("parents") or []):
            parent_key = str(parent).strip()
            if parent_key:
                children.setdefault(parent_key, set()).add(key)
    return children


def _task_has_descendant_key(start_key: str, children_map: dict[str, set[str]], target_keys: set[str]) -> bool:
    pending = list(children_map.get(str(start_key or "").strip(), set()))
    seen: set[str] = set()
    while pending:
        child = pending.pop()
        if child in target_keys:
            return True
        if child in seen:
            continue
        seen.add(child)
        pending.extend(children_map.get(child, set()))
    return False


def _infer_pm_task_phase(task: dict[str, Any], task_by_key: dict[str, dict[str, Any]], children_map: dict[str, set[str]]) -> str:
    explicit = str(task.get("pm_phase") or "").strip()
    if explicit:
        return explicit
    key = str(task.get("key") or "").strip()
    implementer_keys = {k for k, t in task_by_key.items() if str(t.get("mode") or "").strip() == "implementer"}
    reviewer_keys = {k for k, t in task_by_key.items() if str(t.get("mode") or "").strip() == "reviewer"}
    if _task_has_descendant_key(key, children_map, implementer_keys | reviewer_keys):
        return "design_summary"
    if _depends_on_any_task_key(task, task_by_key, reviewer_keys):
        return "final_synthesis"
    if _depends_on_any_task_key(task, task_by_key, implementer_keys):
        return "final_synthesis"
    return "design_approval_gate"


def _is_terminal_pm_task(task: dict[str, Any], children_map: dict[str, set[str]]) -> bool:
    explicit = task.get("terminal_task")
    if isinstance(explicit, bool):
        return explicit
    key = str(task.get("key") or "").strip()
    return not children_map.get(key)


def _final_phase_task_keys(tasks: list[dict[str, Any]], expected_deliverables: list[str] | None = None) -> list[str]:
    normalized_tasks = [t for t in tasks if isinstance(t, dict)]
    task_by_key = {str(t.get("key") or "").strip(): t for t in normalized_tasks if str(t.get("key") or "").strip()}
    children_map = _task_children_map(normalized_tasks)
    pm_tasks = [t for t in normalized_tasks if str(t.get("assignee") or "").strip() == "project_manager" and (t.get("parents") or [])]
    finals = [
        t for t in pm_tasks
        if _infer_pm_task_phase(t, task_by_key, children_map) == "final_synthesis"
        and _is_terminal_pm_task(t, children_map)
    ]
    if finals:
        return [str(t.get("key") or "").strip() for t in finals if str(t.get("key") or "").strip()]
    reviewer_keys = {str(t.get("key") or "").strip() for t in normalized_tasks if str(t.get("mode") or "").strip() == "reviewer"}
    implementer_keys = {str(t.get("key") or "").strip() for t in normalized_tasks if str(t.get("mode") or "").strip() == "implementer"}
    if reviewer_keys:
        fallback = [t for t in pm_tasks if _depends_on_any_task_key(t, task_by_key, reviewer_keys) and _is_terminal_pm_task(t, children_map)]
        return [str(t.get("key") or "").strip() for t in fallback if str(t.get("key") or "").strip()]
    if expected_deliverables and implementer_keys:
        fallback = [t for t in pm_tasks if _depends_on_any_task_key(t, task_by_key, implementer_keys) and _is_terminal_pm_task(t, children_map)]
        return [str(t.get("key") or "").strip() for t in fallback if str(t.get("key") or "").strip()]
    return []


def _review_contract(
    expected_deliverables: list[str],
    *,
    review_target_paths: list[str] | None = None,
    design_artifact: str | None = None,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, Any]:
    normalized_targets = [str(item).strip() for item in (review_target_paths or expected_deliverables) if str(item).strip()]
    mode = _infer_review_mode(normalized_targets)
    common_checks = [
        "deliverable_exists",
        "scope_within_allowed_paths",
        "required_evidence_present",
        "design_spec_reviewed",
        "acceptance_criteria_checked",
    ]
    common_fields = [
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
    if mode == "docs":
        checks = [*common_checks, "content_matches_request", "line_count_limit"]
    else:
        checks = [*common_checks, "build_or_static_validation_run", "implementation_matches_design"]
    contract = {
        "mode": mode,
        "required_verdict_fields": common_fields,
        "checks": checks,
        "allowed_verdicts": ["pass", "concern", "blocked"],
        "allowed_escalation_targets": ["implementer", "architect", "project_manager", "none"],
    }
    design_artifact_text = str(design_artifact or "").strip()
    if design_artifact_text:
        contract["design_artifact"] = design_artifact_text
    normalized_acceptance = [str(item).strip() for item in (acceptance_criteria or []) if str(item).strip()]
    if normalized_acceptance:
        contract["acceptance_criteria"] = normalized_acceptance
    if normalized_targets:
        contract["expected_deliverables"] = normalized_targets
    return contract


def _final_report_contract(final_artifact: str | None = None) -> dict[str, Any]:
    contract = {
        "required_fields": ["report_summary", "user_report", "delivered_artifacts", "open_risks"],
        "allowed_statuses": ["completed", "needs_followup"],
        "required_sections": ["# Final Report", "## Summary", "## Delivered Artifacts", "## Open Risks"],
        "checks": [
            "reviewer_result_verdict_is_pass",
            "reviewer_scope_status_within_allowed_paths",
            "reviewer_evidence_present",
            "delivered_artifacts_within_scope",
            "user_report_non_empty",
            "final_report_sections_present",
        ],
    }
    final_artifact_text = str(final_artifact or "").strip()
    if final_artifact_text:
        contract["required_artifacts"] = [final_artifact_text]
    return contract


def _workflow_contract_path(project_path: str, plan_id: str) -> Path:
    return Path(project_path) / ".soul" / "workflows" / plan_id / "contract.json"


def _build_workflow_contract(plan: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(plan.get("plan_id") or "").strip()
    artifacts = _artifact_paths(plan_id)
    explicit = plan.get("contract") if isinstance(plan.get("contract"), dict) else {}
    plan_kind = _plan_kind(plan)
    explicit_design_artifact = _explicit_design_artifact(plan)
    design_artifact_candidates = _normalize_contract_paths([
        explicit_design_artifact,
        *_completed_design_task_artifact_paths(plan),
        artifacts.get("design") or "",
    ])
    design_artifact = design_artifact_candidates[0] if design_artifact_candidates else artifacts.get("design") or ""
    design_concrete_paths: list[str] = []
    for candidate in design_artifact_candidates:
        candidate_paths = _extract_design_artifact_concrete_paths(
            str(plan.get("project_path") or "").strip(),
            candidate,
        )
        if candidate_paths:
            design_artifact = candidate
            design_concrete_paths = candidate_paths
            break
    explicit_expected_deliverables = _normalize_contract_paths(explicit.get("expected_deliverables") or [])
    explicit_implementation_paths = _normalize_contract_paths(explicit.get("implementation_paths") or [])
    explicit_concrete_expected = [path for path in explicit_expected_deliverables if _is_concrete_implementation_path(path)]
    project_path = str(plan.get("project_path") or "").strip()
    implementation_paths = _reconcile_implementation_paths(
        project_path,
        explicit_implementation_paths,
        design_concrete_paths,
        explicit_concrete_expected,
    )
    if explicit_expected_deliverables and design_concrete_paths:
        expected_deliverables = _normalize_contract_paths([
            *[
                path for path in explicit_expected_deliverables
                if not _is_concrete_implementation_path(path)
                or path in implementation_paths
                or _path_has_existing_project_anchor(project_path, path)
            ],
            *implementation_paths,
        ])
    else:
        expected_deliverables = explicit_expected_deliverables or implementation_paths
    tasks = plan.get("tasks") or []
    final_task_keys = _final_phase_task_keys(tasks, expected_deliverables)
    phase_tasks = {
        "architect": [t.get("key") for t in tasks if isinstance(t, dict) and t.get("mode") == "architect"],
        "implementer": [t.get("key") for t in tasks if isinstance(t, dict) and t.get("mode") == "implementer"],
        "reviewer": [t.get("key") for t in tasks if isinstance(t, dict) and t.get("mode") == "reviewer"],
        "final": final_task_keys,
    }
    default_phase_allowed = {
        "architect": [design_artifact] if design_artifact else [artifacts["design"]],
        "implementer": [*implementation_paths, artifacts["implementation"]],
        "reviewer": [artifacts["verification"]],
        "final": [artifacts["final"]],
    }
    default_required_evidence = {
        "architect": [design_artifact] if design_artifact else [artifacts["design"]],
        "implementation": [design_artifact, *implementation_paths] if design_artifact else [artifacts["design"], *implementation_paths],
        "reviewer": [design_artifact, *implementation_paths, artifacts["verification"]] if design_artifact else [artifacts["design"], *implementation_paths, artifacts["verification"]],
        "final": [design_artifact, *implementation_paths, artifacts["verification"], artifacts["final"]] if design_artifact else [artifacts["design"], *implementation_paths, artifacts["verification"], artifacts["final"]],
    }
    return {
        "version": 3 if explicit else 2,
        "plan_id": plan_id,
        "plan_kind": plan_kind,
        "source_plan_id": str(explicit.get("source_plan_id") or "").strip() or None,
        "request": plan.get("request") or "",
        "summary": plan.get("summary") or "",
        "project_path": plan.get("project_path") or "",
        "design_artifact": design_artifact,
        "expected_deliverables": expected_deliverables,
        "implementation_paths": implementation_paths,
        "artifacts": artifacts,
        "review": _review_contract(
            expected_deliverables,
            review_target_paths=implementation_paths or expected_deliverables,
            design_artifact=design_artifact,
            acceptance_criteria=plan.get("acceptance_criteria") or [],
        ),
        "final_report": _final_report_contract(artifacts.get("final")),
        "claude_delegation": explicit.get("claude_delegation") or _claude_delegation_contract(),
        "required_tasks_by_phase": explicit.get("required_tasks_by_phase") or phase_tasks,
        "phase_allowed_paths": explicit.get("phase_allowed_paths") or default_phase_allowed,
        "required_evidence_by_phase": explicit.get("required_evidence_by_phase") or default_required_evidence,
        "approvals": {"plan_approved": bool(plan.get("approved_at")), "design_approved": bool(plan.get("design_approved_at"))},
        "approval_stages": {
            "pm_plan": {
                "status": "approved" if plan.get("approved_at") else "awaiting_pm_approval",
                "approved_at": plan.get("approved_at"),
                "approved_by": plan.get("approved_by"),
            },
            "design": {
                "status": "approved" if plan.get("design_approved_at") else ("awaiting_design_approval" if plan.get("design_ready_at") else "not_ready"),
                "approved_at": plan.get("design_approved_at"),
                "approved_by": plan.get("design_approved_by"),
            },
        },
        "done_when": explicit.get("done_when") or [
            "all required Kanban tasks are done",
            "expected deliverables exist",
            "required evidence exists",
            "reviewer has completed successfully",
            "final report is non-empty",
        ],
        "forbidden_paths": explicit.get("forbidden_paths") or [".git/", ".env", "secrets/"],
        "risk_level": explicit.get("risk_level") or "low",
    }


def _write_workflow_contract(plan: dict[str, Any]) -> dict[str, Any]:
    project_path = str(plan.get("project_path") or "").strip()
    plan_id = str(plan.get("plan_id") or "").strip()
    if not project_path or not plan_id:
        return {"ok": False, "error": "project_path and plan_id are required for workflow contract"}
    contract = _build_workflow_contract(plan)
    invariant_errors = _validate_workflow_contract_invariants(plan, contract)
    if invariant_errors:
        return {"ok": False, "error": "workflow contract invariants failed", "validation_errors": invariant_errors, "contract": contract}
    path = _workflow_contract_path(project_path, plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "path": str(path), "contract": contract}


def _export_approved_scope(
    plan: dict[str, Any],
    phase: str,
    task_id: str | None,
    approved_by: str | None = None,
    approved_at: int | None = None,
    reviewer_result: dict[str, Any] | None = None,
    parent_task_ids: list[str] | None = None,
) -> dict[str, Any]:
    project_path = str(plan.get("project_path") or "").strip()
    if not project_path:
        return {"ok": False, "error": "project_path is required for approved_scope export"}
    if phase not in _VALID_EXPORT_PHASES:
        return {"ok": False, "error": f"phase must be one of {sorted(_VALID_EXPORT_PHASES)}, got {phase!r}"}
    contract = _build_workflow_contract(plan)
    invariant_errors = _validate_workflow_contract_invariants(plan, contract)
    if invariant_errors:
        return {"ok": False, "error": "approved_scope export blocked by workflow contract invariants", "validation_errors": invariant_errors, "phase": phase}
    if phase in {"implementer", "reviewer", "final"} and not plan.get("design_approved_at"):
        return {"ok": False, "error": f"phase={phase} export requires design_approved_at on the plan", "phase": phase}
    artifacts = contract.get("artifacts", {})
    design_artifact = str(contract.get("design_artifact") or artifacts.get("design") or "").strip()
    expected = contract.get("expected_deliverables", [])
    implementation_paths = contract.get("implementation_paths", [])
    phase_allowed = contract.get("phase_allowed_paths", {})
    allowed = phase_allowed.get(phase) or [".soul/artifacts/"]
    if phase == "implementer" and not any(not str(path).startswith(".soul/") and not _is_docs_like_path(str(path)) for path in allowed):
        return {
            "ok": False,
            "error": "implementer approved_scope requires at least one concrete non-doc allowed path",
            "phase": phase,
        }
    # Review/final must be able to inspect deliverables without allowing code/config writes.
    if phase in {"reviewer", "final"}:
        allowed = sorted(set([*allowed, *expected, *implementation_paths, ".soul/artifacts/"]))
    required_by_phase = contract.get("required_evidence_by_phase", {})
    evidence_phase = "implementation" if phase == "implementer" else phase
    required = [str(p).strip() for p in (required_by_phase.get(evidence_phase) or [artifacts.get("design")]) if str(p).strip()]
    if phase in {"implementer", "reviewer", "final"} and not required:
        return {"ok": False, "error": f"phase={phase} export requires non-empty required evidence", "phase": phase}
    sync_now = int(time.time())
    design_approved_at = int(plan.get("design_approved_at") or approved_at or sync_now)
    approval_actor = approved_by or plan.get("design_approved_by") or plan.get("approved_by")
    scope = {
        "version": 1,
        "plan_id": plan.get("plan_id"),
        "task_id": task_id or "pending_task",
        "phase": phase,
        "approval": {
            "plan_approved": True,
            "design_approved": bool(plan.get("design_approved_at")) or phase in {"implementer", "reviewer", "final"},
            "approved_by": approval_actor,
            "approved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(design_approved_at)),
            "design_approved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(design_approved_at)),
        },
        "scope_sync": {
            "phase": phase,
            "task_id": task_id or "pending_task",
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(sync_now)),
        },
        "scope": {"allowed_paths": allowed, "disallowed_paths": [".git/", ".env", "secrets/"]},
        "risk": {"level": "low", "types": [], "impact": [], "rollback": []},
        "evidence": {"required": [p for p in required if p], "produced": []},
        "design_spec": {
            "artifact_path": design_artifact or None,
            "expected_deliverables": [str(p).strip() for p in expected if str(p).strip()],
            "implementation_paths": [str(p).strip() for p in implementation_paths if str(p).strip()],
            "acceptance_criteria": [str(item).strip() for item in (plan.get("acceptance_criteria") or []) if str(item).strip()],
        },
        "review": contract.get("review") or _review_contract(expected, review_target_paths=implementation_paths or expected, design_artifact=artifacts.get("design"), acceptance_criteria=plan.get("acceptance_criteria") or []),
        "final_report": contract.get("final_report") or _final_report_contract(artifacts.get("final")),
        "completion": {"parent_task_ids": [str(pid).strip() for pid in (parent_task_ids or []) if str(pid).strip()], "notes": f"Generated from PM workflow contract for {phase}"},
        "contract_path": str(_workflow_contract_path(project_path, str(plan.get("plan_id") or ""))),
    }
    if reviewer_result:
        scope["reviewer_result"] = reviewer_result
    path = Path(project_path) / ".soul" / "approved_scope.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(scope, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "path": str(path), "phase": phase, "scope": scope}


def _validate_staged_plan_graph(tasks: list[dict[str, Any]], expected_deliverables: list[str]) -> list[str]:
    errors: list[str] = []
    architect = [t for t in tasks if t.get("mode") == "architect"]
    implementer = [t for t in tasks if t.get("mode") == "implementer"]
    reviewer = [t for t in tasks if t.get("mode") == "reviewer"]
    task_by_key = {t["key"]: t for t in tasks}
    children_map = _task_children_map(tasks)
    finalizer_keys = set(_final_phase_task_keys(tasks, expected_deliverables))
    finalizers = [t for t in tasks if str(t.get("key") or "") in finalizer_keys]
    pm_with_parents = [t for t in tasks if t.get("assignee") == "project_manager" and t.get("parents")]
    if not architect:
        errors.append("staged workflow requires at least one backend-architect architect task")
    if not implementer:
        errors.append("staged workflow requires at least one backend-implementer implementer task after design approval")
    if not reviewer:
        errors.append("staged workflow requires at least one backend-reviewer reviewer task after implementation")
    if not finalizers:
        errors.append("staged workflow requires at least one terminal project_manager final/synthesis task after reviewer completion")
    reviewer_keys = {t["key"] for t in reviewer}
    implementer_keys = {t["key"] for t in implementer}
    if reviewer and pm_with_parents and not finalizers:
        errors.append("reviewer tasks require a separate project_manager final/synthesis task downstream of reviewer completion")
    architect_keys = {t["key"] for t in architect}
    for t in architect:
        for parent in t.get("parents") or []:
            ptask = task_by_key.get(parent)
            if ptask and ptask.get("key") not in architect_keys:
                errors.append(f"{t['key']}: architect phase task cannot depend on non-architect parent {parent!r}; phase 1 must be independently executable")
    for t in reviewer:
        if implementer_keys and not _depends_on_any_task_key(t, task_by_key, implementer_keys):
            errors.append(f"{t['key']}: reviewer task must depend on an implementer task")
    reviewer_keys = {t["key"] for t in reviewer}
    implementer_keys = {t["key"] for t in implementer}
    for final in finalizers:
        parents = set(final.get("parents") or [])
        if expected_deliverables and reviewer_keys and not (parents & reviewer_keys):
            errors.append(f"{final['key']}: final task must depend on reviewer task(s), not only architect/implementer")
        elif expected_deliverables and not reviewer_keys and implementer_keys and not (parents & implementer_keys):
            errors.append(f"{final['key']}: final task must depend on implementation output")
    for task in pm_with_parents:
        key = str(task.get("key") or "")
        inferred_phase = _infer_pm_task_phase(task, task_by_key, children_map)
        explicit_phase = str(task.get("pm_phase") or "").strip()
        if explicit_phase == "final_synthesis" and not _is_terminal_pm_task(task, children_map):
            errors.append(f"{key}: pm_phase=final_synthesis tasks must be terminal; downstream implementation/review work must be a separate PM gate phase")
        if inferred_phase == "design_summary" and explicit_phase == "final_synthesis":
            errors.append(f"{key}: PM task is positioned before downstream implementation/review work, so it cannot be final_synthesis; use pm_phase=design_summary or design_approval_gate")
    return errors


def _looks_like_relative_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text or text.startswith("/") or text.startswith("~"):
        return False
    if any(ch in text for ch in (":", "\n", "\r", "\t")):
        return False
    parts = [part for part in text.split("/") if part]
    if not parts:
        return False
    if any(part in {".", ".."} for part in parts):
        return False
    filename = parts[-1]
    return "." in filename and " " not in filename


def _is_concrete_implementation_path(path: str) -> bool:
    text = _normalize_contract_path(path)
    if not text or text.startswith(".soul/"):
        return False
    return not _is_docs_like_path(text)


def _path_has_existing_project_anchor(project_path: str, path: str) -> bool:
    text = _normalize_contract_path(path)
    if not text or text.startswith(".soul/"):
        return True
    root = Path(str(project_path or "").strip())
    if not root:
        return False
    candidate = root / text
    if candidate.exists() or candidate.parent.exists():
        return True
    parts = [part for part in text.split("/") if part]
    # Treat a path as anchored only when the exact file or immediate parent
    # directory exists.  A broad top-level `src/` match is too weak and lets
    # stale project/module paths such as `src/Books.Api/Program.cs` survive.
    return False


def _reconcile_implementation_paths(
    project_path: str,
    explicit_paths: list[str],
    design_paths: list[str],
    expected_paths: list[str],
) -> list[str]:
    explicit_paths = _normalize_contract_paths(explicit_paths)
    design_paths = _normalize_contract_paths(design_paths)
    expected_paths = _normalize_contract_paths(expected_paths)
    if not explicit_paths:
        return design_paths or expected_paths
    if not design_paths:
        return explicit_paths
    design_has_existing_anchor = any(_path_has_existing_project_anchor(project_path, path) for path in design_paths)
    reconciled: list[str] = []
    for path in explicit_paths:
        if not design_has_existing_anchor or _path_has_existing_project_anchor(project_path, path) or path in design_paths:
            reconciled.append(path)
    # Design artifact paths are canonical observed handoff evidence; always add
    # them so approved_scope follows the actual repository structure.
    reconciled.extend(design_paths)
    if not reconciled:
        reconciled = design_paths or explicit_paths or expected_paths
    return _normalize_contract_paths(reconciled)


_CANONICAL_TASK_PHASES = {"architect", "implementer", "reviewer", "final"}
_CANONICAL_PATH_PHASES = {"architect", "implementer", "reviewer", "final"}
_CANONICAL_EVIDENCE_PHASES = {"architect", "implementation", "reviewer", "final"}
_VALID_EXPORT_PHASES = {"architect", "implementer", "reviewer", "final"}


def _claude_delegation_contract() -> dict[str, dict[str, Any]]:
    required_fields = ["used", "command", "session_id", "output_artifact", "status"]
    return {
        "architect": {
            "required": True,
            "mode": "read_only",
            "required_fields": required_fields,
            "accepted_statuses": ["completed", "success"],
        },
        "implementer": {
            "required": True,
            "mode": "write_allowed",
            "required_fields": required_fields,
            "accepted_statuses": ["completed", "success"],
        },
        "reviewer": {"required": False, "mode": "independent_review"},
        "final": {"required": False, "mode": "pm_synthesis"},
    }


def _claude_delegation_policy_for_mode(mode: str) -> dict[str, Any]:
    return _claude_delegation_contract().get(str(mode or "").strip(), {"required": False})


def _claude_delegation_body_section(task: dict[str, Any]) -> list[str]:
    mode = str(task.get("mode") or "").strip()
    policy = _claude_delegation_policy_for_mode(mode)
    if not policy.get("required"):
        return []
    mode_label = "read-only" if mode == "architect" else "write-allowed"
    return [
        "",
        "[Python Claude runner 강제]",
        f"- 이 {mode} 카드는 직접 설계/구현하지 말고 Python Claude runner만 실행해 수행한다.",
        f"- runner 위임 모드: {mode_label}",
        "- 실행기는 tools.pm_claude_delegation 이며 Claude Code command, prompt, output format을 인자로 받아야 한다.",
        "- 아래 [Python Claude runner 실행 명령] 섹션의 명령을 먼저 실행해 prompt/artifact/manifest를 생성한다.",
        "- runner manifest가 없거나 현재 plan/task/mode와 일치하지 않으면 Kanban 완료가 차단된다.",
        "- reviewer/final 단계에는 이 Claude runner 강제가 적용되지 않으며 독립 검증/PM 종합을 유지한다.",
    ]


def _claude_runner_paths(plan_id: str, task_id: str, mode: str) -> dict[str, str]:
    artifact_kind = "implementation" if mode == "implementer" else "design"
    output_format = "implementation" if mode == "implementer" else "design"
    return {
        "prompt": f".soul/prompts/{plan_id}_{task_id}_{mode}.md",
        "context": f".soul/prompts/internal/{plan_id}_{task_id}_{mode}_context.md",
        "artifact": f".soul/artifacts/{artifact_kind}/{plan_id}_{artifact_kind}.md",
        "manifest": f".soul/artifacts/claude/{plan_id}_{task_id}_{mode}_manifest.json",
        "output_format": output_format,
    }


_COMMON_CLAUDE_PROMPT_SECTIONS = (
    "[작업 목표]",
    "[작업 내용]",
    "[파일 경로]",
    "[제약 사항]",
    "[제외 사항]",
    "[결과물 형식]",
    "[완료 조건]",
    "[검증 방법]",
    "[불확실성 처리]",
)

_IMPLEMENTER_ARTIFACT_REQUIRED_HEADINGS = (
    "## 구현 요약",
    "## 변경 파일",
    "## 설계/승인 범위 일치 여부",
    "## 검증 명령 및 결과",
    "## Reviewer Handoff",
    "## 남은 리스크",
)


def _task_text(task: dict[str, Any], key: str, fallback: str = "") -> str:
    return str(task.get(key) or fallback).strip()


def _write_internal_context_file(
    *,
    project_path: str,
    plan_id: str,
    task_id: str,
    mode: str,
    request: str,
    task: dict[str, Any],
    parent_ids: list[str],
    paths: dict[str, str],
) -> Path:
    context_path = Path(project_path) / paths["context"]
    context_path.parent.mkdir(parents=True, exist_ok=True)
    command = _claude_runner_command(plan_id, task_id, mode, project_path)
    context_text = "\n".join([
        f"# PM Claude runner internal context ({mode})",
        "",
        "이 파일은 PM/worker audit 및 debugging용이다. Claude runner stdin에는 이 파일을 전달하지 않는다.",
        "",
        "## Runtime metadata",
        f"- plan_id: {plan_id}",
        f"- task_id: {task_id}",
        f"- mode: {mode}",
        f"- project_path: {project_path}",
        f"- parent_task_ids: {', '.join(parent_ids) if parent_ids else '없음'}",
        f"- prompt_file: {paths['prompt']}",
        f"- artifact_path: {paths['artifact']}",
        f"- manifest_path: {paths['manifest']}",
        "",
        "## Runner command",
        "```bash",
        command,
        "```",
        "",
        "## Original user request",
        request.strip() or "(empty)",
        "",
        "## Raw task",
        f"- title: {_task_text(task, 'title')}",
        f"- key: {_task_text(task, 'key')}",
        "",
        _task_text(task, "body", "(no extra body)"),
    ])
    context_path.write_text(context_text, encoding="utf-8")
    return context_path


def _format_prompt_section(title: str, lines: list[str]) -> list[str]:
    return [title, *lines, ""]


def _claude_prompt_work_context(request: str, task: dict[str, Any]) -> str:
    request_context = str(request or "").strip()
    body = _task_text(task, "body", "")
    title = _task_text(task, "title", "")
    return "\n".join(part for part in [title, request_context, body] if part).strip()


def _infer_claude_prompt_features(*, mode: str, request: str, task: dict[str, Any]) -> set[str]:
    text = _claude_prompt_work_context(request, task)
    lowered = text.lower()
    features: set[str] = set()
    interface_markers = (
        "api", "endpoint", "dto", "contract", "interface", "request", "response",
        "get", "post", "put", "patch", "delete", "http", "route", "handler",
        "엔드포인트", "인터페이스", "계약", "요청", "응답", "입력", "출력", "함수", "이벤트",
    )
    tokens = set(re.findall(r"[a-z0-9_{}.-]+", lowered))
    if any(marker in tokens for marker in interface_markers if marker.isascii()) or any(marker in lowered for marker in interface_markers if not marker.isascii()) or re.search(r"\b(GET|POST|PUT|PATCH|DELETE)\b|/[A-Za-z0-9_/{}/.-]+", text):
        features.add("interface")
    edge_markers = (
        "empty", "null", "unknown", "error", "exception", "404", "400", "500", "validation", "invalid",
        "빈", "없", "오류", "예외", "검증", "경계", "엣지", "상태", "불가",
    )
    if any(marker in lowered for marker in edge_markers):
        features.add("edge_cases")
    architecture_markers = (
        "src", "domain", "application", "infrastructure", "controller", "handler", "repository", "service",
        "dependency", "di", "minimal api", "레이어", "아키텍처", "구조", "저장소", "서비스",
    )
    if any(marker in lowered for marker in architecture_markers):
        features.add("architecture_context")
    if mode == "architect":
        features.add("alternatives")
        features.add("user_confirmation")
    return features


def _conditional_claude_prompt_sections(*, mode: str, features: set[str]) -> list[str]:
    sections: list[str] = []
    if "interface" in features:
        sections += _format_prompt_section("[인터페이스 계약]", [
            "- API/함수/이벤트/입출력 경계가 있다면 method/path/input/output/error contract를 명시한다.",
            "- 응답 필드명, 타입, null 허용 여부, 빈 결과 동작을 구분한다.",
            "- 기존 호환성 또는 breaking change 여부를 확인한다.",
        ])
    if "edge_cases" in features:
        sections += _format_prompt_section("[엣지 케이스]", [
            "- 빈 결과, 존재하지 않는 대상, 잘못된 입력, null/unknown, 예외/오류 경로를 분리한다.",
            "- 이번 승인 범위에서 처리할 항목과 후속 범위로 남길 항목을 구분한다.",
        ])
    if "architecture_context" in features:
        sections += _format_prompt_section("[아키텍처 컨텍스트]", [
            "- 기존 레이어/폴더/DI/테스트 구조를 먼저 관찰하고 그 패턴을 우선한다.",
            "- 새 구조를 만들기보다 현재 경계 안에서 최소 변경을 우선한다.",
        ])
    if mode == "architect" and "alternatives" in features:
        sections += _format_prompt_section("[대안 비교]", [
            "- 의미 있는 설계 대안 2개 이상을 비교한다. 대안이 1개뿐이면 그 이유를 적는다.",
            "- 선택한 설계와 배제한 설계의 이유를 구현 영향/검증 난이도 기준으로 설명한다.",
        ])
    if mode == "architect" and "user_confirmation" in features:
        sections += _format_prompt_section("[사용자 확인 필요사항]", [
            "- 구현 전 사용자가 결정해야 하는 사항과 구현자가 기본값으로 진행 가능한 사항을 분리한다.",
            "- 확인 필요사항이 없으면 `없음`이라고 명시한다.",
        ])
    return sections


def _build_architect_delegation_prompt(
    *,
    project_path: str,
    request: str,
    task: dict[str, Any],
    paths: dict[str, str],
) -> str:
    """Build the PM-to-architect task envelope, not the Claude execution prompt.

    The PM owns routing, scope, approval boundaries, and artifact locations.
    The architect runner owns the actual Claude Code prompt composition so the
    architect remains responsible for analysis strategy and design judgment.
    """
    title = _task_text(task, "title", "설계 작업")
    body = _task_text(task, "body", "(no extra task body)")
    sections: list[str] = ["# Architect Task Envelope", ""]
    sections += _format_prompt_section("[역할 경계]", [
        "- 이 파일은 PM이 작성한 작업 지시 envelope이다. Claude Code 실행 prompt가 아니다.",
        "- PM은 무엇을/어떤 제약으로 맡길지만 지정한다.",
        "- architect runner가 이 envelope를 해석해 Claude Code 설계 prompt를 직접 구성한다.",
    ])
    sections += _format_prompt_section("[작업 지시]", [
        f"- 제목: {title}",
        "- 단계: architect design only",
        "- 목표: 설계/작업분해/검증계획 산출물을 작성해 PM의 사용자 승인 요청에 제공한다.",
    ])
    sections += _format_prompt_section("[사용자 원 요청]", [request.strip() or "(empty)"])
    sections += _format_prompt_section("[PM 전달 task body]", [body])
    sections += _format_prompt_section("[운영 제약]", [
        f"- 프로젝트 루트: `{project_path}`",
        "- production 코드 루트: `src` (사용자 요청 또는 task body가 다르게 지정하면 그 지시를 우선한다)",
        "- read-only architect 단계로 수행한다.",
        "- production 코드와 테스트 코드를 수정하지 않는다.",
        "- 사용자 승인 전 구현을 시작하지 않는다.",
    ])
    sections += _format_prompt_section("[산출물/게이트]", [
        f"- 설계 산출물 경로: `{paths['artifact']}`",
        "- 산출물은 PM/implementer/reviewer가 handoff로 사용할 수 있는 한국어 markdown 설계 문서여야 한다.",
        "- 필수 산출물 종류: 상태, 설계 요약, 목표/범위, 현황 분석, 선택한 설계, 영향 범위, 구현 작업분해, 검증 계획, 리스크, 사용자 확인 필요사항, 구현 승인 전제.",
        "- API/DTO/인터페이스 계약 등 세부 섹션 필요 여부와 구체 내용은 architect가 저장소와 요청을 보고 판단한다.",
    ])
    return "\n".join(sections).rstrip() + "\n"


def _build_implementer_delegation_prompt(
    *,
    project_path: str,
    request: str,
    task: dict[str, Any],
    paths: dict[str, str],
) -> str:
    title = _task_text(task, "title", "구현 작업")
    body = _task_text(task, "body", "승인된 scope와 task body를 기준으로 구현한다.")
    features = _infer_claude_prompt_features(mode="implementer", request=request, task=task)
    sections: list[str] = ["# Claude Code Implementer Delegation Prompt", ""]
    sections += _format_prompt_section("[작업 목표]", [
        f"- `{title}`를 승인된 범위 안에서 구현한다.",
        "- reviewer가 검증할 수 있도록 변경 파일과 검증 evidence를 남긴다.",
    ])
    sections += _format_prompt_section("[작업 내용]", [body])
    sections += _format_prompt_section("[파일 경로]", [
        f"- 프로젝트 루트: `{project_path}`",
        "- production 코드 루트: `src` (task body/approved scope가 다르게 지정하면 그 지시를 우선한다)",
        f"- 구현 산출물: `{paths['artifact']}`",
    ])
    sections += _conditional_claude_prompt_sections(mode="implementer", features=features)
    sections += _format_prompt_section("[제약 사항]", [
        "- 승인된 scope 안에서만 수정한다.",
        "- 범위 밖 변경이 필요하면 임의로 진행하지 말고 blocker/evidence로 보고한다.",
        "- 기존 스타일과 구조를 유지하고 불필요한 리팩터링을 하지 않는다.",
    ])
    sections += _format_prompt_section("[제외 사항]", [
        "- 승인되지 않은 기능 추가 제외",
        "- 승인되지 않은 파일/경로 수정 제외",
        "- reviewer 대기 자체를 self-block 사유로 처리하지 않기",
    ])
    sections += _format_prompt_section("[결과물 형식]", [
        "- Claude runner는 최종 stdout을 artifact 파일로 저장한다. 따라서 최종 응답 자체가 완전한 markdown 구현 handoff여야 한다.",
        "- `구현 완료` 같은 상태 보고만 출력하지 말고, 아래 heading을 포함한 전체 artifact 본문을 stdout에 직접 작성한다.",
        "- artifact에는 아래 heading을 포함한 한국어 markdown 구현 handoff만 작성한다.",
        "- 필수 heading은 정확히 `##` heading으로 작성하고, 체크리스트/요약표로 대체하지 않는다.",
        "- 필수 heading: `## 구현 요약`, `## 변경 파일`, `## 설계/승인 범위 일치 여부`, `## 검증 명령 및 결과`, `## Reviewer Handoff`, `## 남은 리스크`",
        "- `## 변경 파일`에는 파일별 변경 이유를 포함한다.",
        "- `## 검증 명령 및 결과`에는 실행한 명령, exit code 또는 pass/fail, 실패/미실행 사유를 포함한다.",
        "- `## Reviewer Handoff`에는 reviewer가 확인해야 할 설계 기준, 범위 경계, 주의 파일을 적는다.",
    ])
    sections += _format_prompt_section("[완료 조건]", [
        "- 승인된 deliverable을 구현했거나, 불가능한 사유를 명확한 blocker로 남긴다.",
        "- 변경 파일과 검증 명령/결과가 handoff에 포함된다.",
        "- reviewer가 이어서 검토할 수 있는 evidence가 있다.",
        "- reviewer/human 검증 대기만으로 blocked 처리하지 않는다.",
    ])
    sections += _format_prompt_section("[검증 방법]", [
        "- 가능한 build/test/static check를 실행한다.",
        "- 실행하지 못한 검증은 이유와 대체 확인 방법을 기록한다.",
        "- 실패한 검증은 수정 가능한 범위에서 먼저 보정하고, 남으면 blocker로 보고한다.",
    ])
    sections += _format_prompt_section("[불확실성 처리]", [
        "- 승인 범위가 불명확하면 가장 좁은 안전 범위로 진행한다.",
        "- 경로/계약이 실제 저장소와 충돌하면 추정 수정하지 말고 충돌 내용을 보고한다.",
        "- 새 의존성, 마이그레이션, 큰 구조 변경은 사전 승인 없이는 하지 않는다.",
    ])
    return "\n".join(sections).rstrip() + "\n"


def _build_claude_delegation_prompt(
    *,
    project_path: str,
    mode: str,
    request: str,
    task: dict[str, Any],
    paths: dict[str, str],
) -> str:
    if mode == "architect":
        return _build_architect_delegation_prompt(project_path=project_path, request=request, task=task, paths=paths)
    if mode == "implementer":
        return _build_implementer_delegation_prompt(project_path=project_path, request=request, task=task, paths=paths)
    return "\n".join([
        "# Claude Code Delegation Prompt",
        "",
        "[작업 목표]",
        f"- {_task_text(task, 'title', '작업을 수행한다.')}",
        "",
        "[작업 내용]",
        _task_text(task, "body", "(no extra body)"),
    ]) + "\n"


def _claude_runner_timeout_seconds() -> int:
    raw = os.environ.get("HERMES_PM_CLAUDE_RUNNER_TIMEOUT", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return 300


def _claude_runner_command(plan_id: str, task_id: str, mode: str, project_path: str) -> str:
    paths = _claude_runner_paths(plan_id, task_id, mode)
    parts = [
        "python3", "-m", "tools.pm_claude_delegation", "run",
        "--mode", mode,
        "--plan-id", plan_id,
        "--task-id", task_id,
        "--workdir", project_path,
        "--command", os.environ.get("HERMES_PM_CLAUDE_COMMAND", "claude -p --max-turns 8 --strict-mcp-config --mcp-config '{\"mcpServers\":{}}' --disable-slash-commands"),
        "--prompt-file", paths["prompt"],
        "--output-format", paths["output_format"],
        "--artifact-path", paths["artifact"],
        "--manifest-path", paths["manifest"],
        "--timeout", str(_claude_runner_timeout_seconds()),
    ]
    if mode == "architect":
        parts.append("--readonly")
    return " ".join(shlex.quote(str(part)) for part in parts)


def _format_claude_runner_invocation_section(plan_id: str, task_id: str, mode: str, project_path: str) -> str:
    if not _claude_delegation_policy_for_mode(mode).get("required"):
        return ""
    paths = _claude_runner_paths(plan_id, task_id, mode)
    command = _claude_runner_command(plan_id, task_id, mode, project_path)
    return "\n".join([
        "",
        "[Python Claude runner 실행 명령]",
        f"- prompt_file: {paths['prompt']}",
        f"- artifact_path: {paths['artifact']}",
        f"- manifest_path: {paths['manifest']}",
        "- 아래 명령을 작업 시작 시 먼저 실행할 것:",
        f"```bash\n{command}\n```",
    ])


def _write_claude_runner_prompt(
    *,
    project_path: str,
    plan_id: str,
    task_id: str,
    mode: str,
    request: str,
    task: dict[str, Any],
    parent_ids: list[str],
) -> dict[str, Any]:
    if not _claude_delegation_policy_for_mode(mode).get("required"):
        return {"ok": True, "skipped": True}
    paths = _claude_runner_paths(plan_id, task_id, mode)
    prompt_path = Path(project_path) / paths["prompt"]
    manifest_path = Path(project_path) / paths["manifest"]
    artifact_path = Path(project_path) / paths["artifact"]
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    context_path = _write_internal_context_file(
        project_path=project_path,
        plan_id=plan_id,
        task_id=task_id,
        mode=mode,
        request=request,
        task=task,
        parent_ids=parent_ids,
        paths=paths,
    )
    prompt_text = _build_claude_delegation_prompt(
        project_path=project_path,
        mode=mode,
        request=request,
        task=task,
        paths=paths,
    )
    prompt_path.write_text(prompt_text, encoding="utf-8")
    return {
        "ok": True,
        "prompt_path": str(prompt_path),
        "context_path": str(context_path),
        "manifest_path": str(manifest_path),
        "artifact_path": str(artifact_path),
    }


def _claude_manifest_path(project_path: str, plan_id: str, task_id: str, mode: str) -> Path:
    safe_plan = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(plan_id or "").strip())
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(task_id or "").strip())
    safe_mode = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(mode or "").strip())
    return Path(project_path) / ".soul" / "artifacts" / "claude" / f"{safe_plan}_{safe_task}_{safe_mode}_manifest.json"


def _validate_implementer_artifact_headings(artifact_path: Path) -> list[str]:
    try:
        text = artifact_path.read_text(encoding="utf-8")
    except Exception as exc:
        return [f"implementer: artifact could not be read for heading validation: {exc}"]
    missing = [heading for heading in _IMPLEMENTER_ARTIFACT_REQUIRED_HEADINGS if heading not in text]
    if not missing:
        return []
    return [
        "implementer: artifact is missing required handoff headings: " + ", ".join(missing)
        + "; edge-case-specific heading rules will be layered separately"
    ]


def _extract_markdown_section_bullets(text: str, heading: str) -> list[str]:
    lines = str(text or "").splitlines()
    in_section = False
    bullets: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_section:
                break
            in_section = stripped == heading
            continue
        if not in_section:
            continue
        match = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*\S)\s*$", stripped)
        if match:
            bullets.append(match.group(1).strip())
    seen: set[str] = set()
    result: list[str] = []
    for item in bullets:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _design_validation_checks_for_plan(plan: dict[str, Any]) -> list[str]:
    explicit = [str(item).strip() for item in (plan.get("acceptance_criteria") or []) if str(item).strip()]
    if explicit:
        return explicit
    project_path = str(plan.get("project_path") or "").strip()
    contract = _build_workflow_contract(plan)
    artifact = str(plan.get("design_artifact_path") or contract.get("design_artifact") or "").strip()
    if not project_path or not artifact:
        return []
    try:
        path = Path(artifact)
        if not path.is_absolute():
            path = Path(project_path) / path
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    return _extract_markdown_section_bullets(text, "## 검증 계획")


def _coverage_norm(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]+", "", str(text or "").lower())


def _reviewer_coverage_texts(reviewer_meta: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for item in reviewer_meta.get("acceptance_checks") or []:
        if isinstance(item, dict):
            texts.extend(str(item.get(key) or "").strip() for key in ("criterion", "status", "notes", "evidence") if str(item.get(key) or "").strip())
        elif str(item).strip():
            texts.append(str(item).strip())
    texts.extend(str(item).strip() for item in (reviewer_meta.get("unmet_requirements") or []) if str(item).strip())
    return texts


def _validate_design_validation_coverage(plan: dict[str, Any], reviewer_meta: dict[str, Any]) -> list[str]:
    review_status = _review_status_from_metadata(reviewer_meta)
    if review_status != "approved":
        return []
    checks = _design_validation_checks_for_plan(plan)
    if not checks:
        return []
    reviewer_texts = _reviewer_coverage_texts(reviewer_meta)
    normalized_reviewer_texts = [_coverage_norm(text) for text in reviewer_texts if _coverage_norm(text)]
    missing: list[str] = []
    for check in checks:
        norm_check = _coverage_norm(check)
        if not norm_check:
            continue
        if not any(norm_check in candidate or candidate in norm_check for candidate in normalized_reviewer_texts):
            missing.append(check)
    if not missing:
        return []
    return [
        "reviewer: design validation coverage missing for approved verdict: " + "; ".join(missing)
        + "; mark uncovered checks as concern/unmet or add matching acceptance_checks evidence"
    ]


def _validate_runner_manifest(project_path: str, plan_id: str, task_id: str, mode: str) -> list[str]:
    policy = _claude_delegation_policy_for_mode(mode)
    if not policy.get("required"):
        return []
    path = _claude_manifest_path(project_path, plan_id, task_id, mode)
    if not path.is_file():
        return [f"{mode}: Python Claude runner manifest is required at {path}"]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"{mode}: Python Claude runner manifest is not valid JSON: {exc}"]
    errors: list[str] = []
    if manifest.get("runner") != "pm_claude_delegation":
        errors.append(f"{mode}: manifest.runner must be pm_claude_delegation")
    if str(manifest.get("mode") or "") != mode:
        errors.append(f"{mode}: manifest.mode must match current task mode")
    if str(manifest.get("plan_id") or "") != str(plan_id):
        errors.append(f"{mode}: manifest.plan_id must match current plan")
    if str(manifest.get("task_id") or "") != str(task_id):
        errors.append(f"{mode}: manifest.task_id must match current task")
    if str(manifest.get("status") or "").lower() not in {"completed", "success"}:
        errors.append(f"{mode}: manifest.status must be completed/success")
    if int(manifest.get("exit_code") if isinstance(manifest.get("exit_code"), int) else -1) != 0:
        errors.append(f"{mode}: manifest.exit_code must be 0")
    artifact = str(manifest.get("artifact_path") or "").strip()
    if not artifact:
        errors.append(f"{mode}: manifest.artifact_path is required")
    else:
        artifact_path = Path(artifact)
        if not artifact_path.is_absolute():
            artifact_path = Path(project_path) / artifact_path
        if not artifact_path.is_file():
            errors.append(f"{mode}: manifest.artifact_path does not exist: {artifact}")
        elif mode == "implementer":
            errors.extend(_validate_implementer_artifact_headings(artifact_path))
    return errors


def _validate_claude_delegation_manifest(mode: str, metadata: Any) -> list[str]:
    policy = _claude_delegation_policy_for_mode(mode)
    if not policy.get("required"):
        return []
    if not isinstance(metadata, dict):
        return [f"{mode}: Claude Code delegation evidence is required in completion metadata.claude_delegation"]
    manifest = metadata.get("claude_delegation")
    if not isinstance(manifest, dict):
        return [f"{mode}: Claude Code delegation evidence is required in completion metadata.claude_delegation"]
    errors: list[str] = []
    if manifest.get("used") is not True:
        errors.append(f"{mode}: claude_delegation.used must be true")
    for field in policy.get("required_fields") or []:
        if field == "used":
            continue
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{mode}: claude_delegation.{field} is required")
    status = str(manifest.get("status") or "").strip().lower()
    accepted = {str(item).lower() for item in (policy.get("accepted_statuses") or [])}
    if status and accepted and status not in accepted:
        errors.append(f"{mode}: claude_delegation.status must be one of {sorted(accepted)}")
    return errors


def validate_task_completion_gate(conn: Any, task_id: str, metadata: Any | None = None, *, profile: str = "project_manager") -> dict[str, Any]:
    """Return PM-workflow completion blockers for task-level policy gates.

    This is called by kanban_db.complete_task before mutating task state. Only
    architect/implementer cards require Claude Code delegation evidence; reviewer
    and final cards remain independent review / PM synthesis phases.
    """
    data, plan_id, plan, phase = _find_plan_for_task(profile, task_id)
    if not data or not plan_id or not isinstance(plan, dict):
        return {"ok": True, "task_id": task_id, "reason": "task not tracked by PM workflow"}
    errors = _validate_runner_manifest(str(plan.get("project_path") or ""), plan_id, task_id, phase)
    if phase == "reviewer" and isinstance(metadata, dict):
        nested_reviewer_result = metadata.get("reviewer_result") if isinstance(metadata.get("reviewer_result"), dict) else {}
        reviewer_meta = {**metadata, **nested_reviewer_result} if nested_reviewer_result else metadata
        errors.extend(_validate_design_validation_coverage(plan, reviewer_meta))
    return {"ok": not errors, "task_id": task_id, "plan_id": plan_id, "phase": phase, "errors": errors}


def _path_extension_family(path: str) -> str:
    text = _normalize_contract_path(path)
    if not text:
        return ""
    suffix = Path(text).suffix.lower().lstrip(".")
    return suffix


def _read_project_relative_text(project_path: str, relative_path: str) -> str:
    root = Path(str(project_path or "").strip())
    rel = Path(str(relative_path or "").strip())
    if not root or not rel:
        return ""
    path = root / rel
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _extract_design_artifact_concrete_paths(project_path: str, design_artifact: str) -> list[str]:
    artifact_text = _read_project_relative_text(project_path, design_artifact)
    if not artifact_text:
        return []
    extracted = _extract_project_paths(artifact_text)
    canonical = [_canonicalize_extracted_project_path(project_path, path) for path in extracted]
    return [path for path in _normalize_contract_paths(canonical) if _is_concrete_implementation_path(path)]


def _validate_explicit_contract(contract: Any) -> list[str]:
    if contract in (None, {}):
        return []
    if not isinstance(contract, dict):
        return ["contract must be an object when provided"]
    errors: list[str] = []
    expected = contract.get("expected_deliverables")
    if expected is not None:
        if not isinstance(expected, list) or not expected:
            errors.append("contract.expected_deliverables must be a non-empty list of relative file paths")
        else:
            for item in expected:
                if not isinstance(item, str) or not _looks_like_relative_path(item):
                    errors.append(f"contract.expected_deliverables contains non-path value: {item!r}")
    implementation_paths = contract.get("implementation_paths")
    if implementation_paths is not None:
        if not isinstance(implementation_paths, list) or not implementation_paths:
            errors.append("contract.implementation_paths must be a non-empty list of relative file paths")
        else:
            for item in implementation_paths:
                if not isinstance(item, str) or not _looks_like_relative_path(item):
                    errors.append(f"contract.implementation_paths contains non-path value: {item!r}")
    design_artifact = contract.get("design_artifact")
    if design_artifact is not None:
        if not isinstance(design_artifact, str) or not _looks_like_relative_path(design_artifact):
            errors.append(f"contract.design_artifact must be a relative file path when provided: {design_artifact!r}")
    source_plan_id = contract.get("source_plan_id")
    if source_plan_id is not None and (not isinstance(source_plan_id, str) or not str(source_plan_id).strip()):
        errors.append("contract.source_plan_id must be a non-empty string when provided")
    required_tasks = contract.get("required_tasks_by_phase")
    if required_tasks is not None:
        if not isinstance(required_tasks, dict):
            errors.append("contract.required_tasks_by_phase must be an object keyed by architect/implementer/reviewer/final")
        else:
            bad = sorted(set(required_tasks.keys()) - _CANONICAL_TASK_PHASES)
            missing = sorted(_CANONICAL_TASK_PHASES - set(required_tasks.keys()))
            if bad or missing:
                errors.append(
                    "contract.required_tasks_by_phase must use exactly architect/implementer/reviewer/final keys"
                    + (f"; unexpected={bad}" if bad else "")
                    + (f"; missing={missing}" if missing else "")
                )
    phase_allowed = contract.get("phase_allowed_paths")
    if phase_allowed is not None:
        if not isinstance(phase_allowed, dict):
            errors.append("contract.phase_allowed_paths must be an object keyed by architect/implementer/reviewer/final")
        else:
            bad = sorted(set(phase_allowed.keys()) - _CANONICAL_PATH_PHASES)
            missing = sorted(_CANONICAL_PATH_PHASES - set(phase_allowed.keys()))
            if bad or missing:
                errors.append(
                    "contract.phase_allowed_paths must use exactly architect/implementer/reviewer/final keys"
                    + (f"; unexpected={bad}" if bad else "")
                    + (f"; missing={missing}" if missing else "")
                )
    evidence = contract.get("required_evidence_by_phase")
    if evidence is not None:
        if not isinstance(evidence, dict):
            errors.append("contract.required_evidence_by_phase must be an object keyed by architect/implementation/reviewer/final")
        else:
            bad = sorted(set(evidence.keys()) - _CANONICAL_EVIDENCE_PHASES)
            missing = sorted(_CANONICAL_EVIDENCE_PHASES - set(evidence.keys()))
            if bad or missing:
                errors.append(
                    "contract.required_evidence_by_phase must use exactly architect/implementation/reviewer/final keys"
                    + (f"; unexpected={bad}" if bad else "")
                    + (f"; missing={missing}" if missing else "")
                )
    return errors


def _validate_plan_kind_structure(plan: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    normalized_kind = _plan_kind(plan)
    tasks = [t for t in (plan.get("tasks") or []) if isinstance(t, dict)]
    explicit_contract = plan.get("contract") if isinstance(plan.get("contract"), dict) else {}
    architect_tasks = [t for t in tasks if str(t.get("mode") or "") == "architect"]
    implementer_tasks = [t for t in tasks if str(t.get("mode") or "") == "implementer"]
    reviewer_tasks = [t for t in tasks if str(t.get("mode") or "") == "reviewer"]
    debugger_tasks = [t for t in tasks if str(t.get("mode") or "") == "debugger"]
    pm_tasks = [t for t in tasks if str(t.get("assignee") or "") == "project_manager"]
    expected_deliverables = _normalize_contract_paths(contract.get("expected_deliverables") or [])
    implementation_paths = _normalize_contract_paths(contract.get("implementation_paths") or [])
    design_artifact = str(plan.get("design_artifact_path") or explicit_contract.get("design_artifact") or "").strip()

    if normalized_kind not in _PLAN_KINDS:
        return [f"plan_kind must be one of {sorted(_PLAN_KINDS)}, got {plan.get('plan_kind')!r}"]
    if normalized_kind == "design":
        if not architect_tasks:
            errors.append("design plan requires at least one architect task")
        if implementer_tasks or reviewer_tasks or debugger_tasks:
            errors.append("design plan cannot include implementer/reviewer/debugger tasks; create a separate execution plan after design approval")
        if pm_tasks:
            errors.append("design plan must stay worker-design-only; PM approval/reporting happens via workflow state, not project_manager tasks")
        if implementation_paths:
            errors.append("design plan cannot declare contract.implementation_paths; implementation scope belongs to a later execution plan")
        concrete_expected = [path for path in expected_deliverables if _is_concrete_implementation_path(path)]
        if concrete_expected:
            errors.append("design plan cannot declare concrete source/test expected_deliverables; approved implementation scope belongs to a later execution plan")
        return errors
    if normalized_kind == "mapping":
        source_plan_id = _source_plan_id(plan)
        if not architect_tasks:
            errors.append("mapping plan requires at least one architect task")
        if implementer_tasks or reviewer_tasks or debugger_tasks:
            errors.append("mapping plan cannot include implementer/reviewer/debugger tasks; mapping must stay architect-only until approved execution planning begins")
        if pm_tasks:
            errors.append("mapping plan must stay worker-design-only; PM approval/reporting happens via workflow state, not project_manager tasks")
        if not source_plan_id:
            errors.append("mapping plan requires contract.source_plan_id referencing an approved design/staged plan")
        return errors
    if normalized_kind == "execution":
        if architect_tasks:
            errors.append("execution plan cannot include architect tasks; approved design/mapping must come from an earlier design plan or artifact")
        if not implementer_tasks:
            errors.append("execution plan requires at least one implementer task")
        if not reviewer_tasks:
            errors.append("execution plan requires at least one reviewer task")
        if not any((t.get("parents") or []) for t in pm_tasks):
            errors.append("execution plan requires a project_manager final/synthesis task with parents")
        if not design_artifact:
            errors.append("execution plan requires contract.design_artifact referencing the approved design artifact")
        concrete_implementation_paths = [path for path in implementation_paths if _is_concrete_implementation_path(path)]
        if not concrete_implementation_paths:
            errors.append("execution plan requires contract.implementation_paths with concrete approved source/test paths")
        return errors
    return errors


def _validate_workflow_contract_invariants(plan: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    tasks = [t for t in (plan.get("tasks") or []) if isinstance(t, dict)]
    plan_kind = _plan_kind(plan)
    errors.extend(_validate_plan_kind_structure(plan, contract))
    has_architect = any(str(t.get("mode") or "") == "architect" for t in tasks)
    has_implementer = any(str(t.get("mode") or "") == "implementer" for t in tasks)
    has_reviewer = any(str(t.get("mode") or "") == "reviewer" for t in tasks)
    explicit_contract = plan.get("contract") if isinstance(plan.get("contract"), dict) else {}
    implementation_paths = [str(p).strip() for p in (contract.get("implementation_paths") or []) if str(p).strip()]
    concrete_implementation_paths = [path for path in implementation_paths if _is_concrete_implementation_path(path)]
    expected_deliverables = [str(p).strip() for p in (contract.get("expected_deliverables") or []) if str(p).strip()]
    implementer_allowed_paths = [str(p).strip() for p in ((contract.get("phase_allowed_paths") or {}).get("implementer") or []) if str(p).strip()]
    reviewer_mode = str((contract.get("review") or {}).get("mode") or "").strip()
    review_targets = [str(p).strip() for p in (((contract.get("review") or {}).get("expected_deliverables")) or []) if str(p).strip()]
    reviewer_result_fields = [str(p).strip() for p in (((contract.get("review") or {}).get("required_verdict_fields")) or []) if str(p).strip()]
    final_required = [str(p).strip() for p in (((contract.get("required_evidence_by_phase") or {}).get("final")) or []) if str(p).strip()]
    project_path = str(plan.get("project_path") or "").strip()
    design_artifact = str(contract.get("design_artifact") or "").strip()
    has_design_scope = bool(plan.get("design_artifact_path") or plan.get("design_ready_at") or plan.get("design_approved_at"))
    explicit_expected = _normalize_contract_paths(explicit_contract.get("expected_deliverables") or [])
    explicit_expected_all_artifacts = bool(explicit_expected) and all(path.startswith(".soul/") or _is_docs_like_path(path) for path in explicit_expected)
    path_scope_expected = bool(explicit_contract.get("implementation_paths")) or has_design_scope
    design_concrete_paths = _extract_design_artifact_concrete_paths(project_path, design_artifact)
    design_extension_families = {_path_extension_family(path) for path in design_concrete_paths if _path_extension_family(path)}
    implementation_extension_families = {_path_extension_family(path) for path in concrete_implementation_paths if _path_extension_family(path)}
    explicit_implementation_paths = _normalize_contract_paths(explicit_contract.get("implementation_paths") or [])
    explicit_implementation_extension_families = {_path_extension_family(path) for path in explicit_implementation_paths if _path_extension_family(path)}

    if has_implementer and path_scope_expected and not any(not path.startswith(".soul/") for path in implementer_allowed_paths):
        errors.append("implementer tasks require at least one non-.soul allowed path in contract.phase_allowed_paths.implementer")
    if has_architect and has_implementer and explicit_expected_all_artifacts and not explicit_contract.get("implementation_paths") and has_design_scope and reviewer_mode == "code":
        errors.append("plans that mix implementation mapping/design with execution must declare contract.implementation_paths as the single canonical source for implementer scope")
    if has_implementer and has_design_scope and reviewer_mode == "code" and not concrete_implementation_paths:
        errors.append("implementer execution requires architect-approved concrete implementation_paths or concrete paths discoverable from the design artifact")
    if has_implementer and explicit_contract.get("implementation_paths") and reviewer_mode == "code" and not concrete_implementation_paths:
        errors.append("contract.implementation_paths must include at least one concrete non-.soul source/test/config path for implementer execution")
    if has_reviewer and reviewer_mode == "code" and path_scope_expected and not concrete_implementation_paths:
        errors.append("reviewer code mode requires at least one concrete implementation_paths entry so code validation does not rely on prose or artifact-only scope")
    if has_reviewer and reviewer_mode == "code" and path_scope_expected and not review_targets:
        errors.append("reviewer code mode requires explicit review.expected_deliverables targets")
    if has_reviewer and reviewer_mode == "code" and path_scope_expected and not any(_is_concrete_implementation_path(path) for path in review_targets):
        errors.append("reviewer code mode must validate at least one concrete source/test path in review.expected_deliverables")
    if has_reviewer and reviewer_result_fields and "validated_artifacts" not in reviewer_result_fields:
        errors.append("reviewer artifacts must require validated_artifacts so final synthesis can verify reviewed scope")
    if has_reviewer and expected_deliverables and not final_required:
        errors.append("final phase requires required_evidence_by_phase.final entries that match reviewer evidence and contract scope")
    if has_implementer and design_extension_families and explicit_implementation_extension_families and design_extension_families.isdisjoint(explicit_implementation_extension_families):
        errors.append(
            "contract.implementation_paths extension family does not match the concrete code/test paths referenced by the approved design artifact"
            f" (design={sorted(design_extension_families)}, implementation={sorted(explicit_implementation_extension_families)})"
        )
    elif has_implementer and design_extension_families and implementation_extension_families and design_extension_families.isdisjoint(implementation_extension_families):
        errors.append(
            "contract.implementation_paths extension family does not match the concrete code/test paths referenced by the approved design artifact"
            f" (design={sorted(design_extension_families)}, implementation={sorted(implementation_extension_families)})"
        )
    return errors


def _validate_followup_task_roles(
    plan: dict[str, Any],
    resolved_tasks: list[dict[str, Any]],
    expected_deliverables: list[str] | None = None,
) -> list[str]:
    expected = [str(item).strip() for item in (expected_deliverables or []) if str(item).strip()]
    normalized_tasks = [t for t in resolved_tasks if isinstance(t, dict)]
    modes = {str(t.get("mode") or "") for t in normalized_tasks}
    plan_kind = _plan_kind(plan)
    final_task_keys = set(_final_phase_task_keys(normalized_tasks, expected))
    final_tasks = [t for t in normalized_tasks if str(t.get("key") or "") in final_task_keys]
    non_final = [t for t in normalized_tasks if str(t.get("key") or "") not in final_task_keys]
    errors: list[str] = []

    if plan_kind == "staged":
        if "implementer" not in modes:
            errors.append("staged plans require an implementer follow-up task after design approval")
        if "reviewer" not in modes:
            errors.append("staged plans require a reviewer follow-up task after design approval")
        if not final_tasks:
            errors.append("staged plans require a final PM follow-up task after design approval")
    if expected:
        if modes == set() or not non_final:
            errors.append("expected deliverables cannot be executed by a final-only follow-up graph")
        if "implementer" not in modes:
            errors.append("expected deliverables require an implementer follow-up task")
        if "reviewer" not in modes:
            errors.append("expected deliverables require a reviewer follow-up task")
        if not final_tasks:
            errors.append("expected deliverables require a final PM follow-up task")
    return errors


def _validate_followup_graph(plan: dict[str, Any], resolved_tasks: list[dict[str, Any]], created: list[dict[str, Any]]) -> list[str]:
    contract = _build_workflow_contract(plan)
    expected = contract.get("expected_deliverables") or []
    errors: list[str] = []
    if not created:
        return ["no follow-up tasks were created"]
    errors.extend(_validate_followup_task_roles(plan, resolved_tasks, expected))
    created_keys = {str(t.get("key") or "") for t in created if isinstance(t, dict)}
    expected_keys = {str(t.get("key") or "") for t in resolved_tasks if isinstance(t, dict)}
    missing = sorted(k for k in expected_keys if k and k not in created_keys)
    if missing:
        errors.append("missing created follow-up task keys: " + ", ".join(missing))
    final_task_keys = set(_final_phase_task_keys(resolved_tasks, expected))
    final_tasks = [t for t in resolved_tasks if str(t.get("key") or "") in final_task_keys]
    created_by_key = {str(t.get("key") or ""): t for t in created if isinstance(t, dict)}
    reviewer_created_ids = {str(created_by_key.get(t.get("key"), {}).get("task_id") or "") for t in resolved_tasks if t.get("mode") == "reviewer"}
    reviewer_created_ids.discard("")
    if expected and reviewer_created_ids:
        for final in final_tasks:
            created_final = created_by_key.get(str(final.get("key") or ""), {})
            parents = set(created_final.get("parents") or [])
            if not (parents & reviewer_created_ids):
                errors.append(f"{final.get('key')}: created final task must depend on created reviewer task id")
    return errors


def _mode_guardrail_text(task: dict[str, Any]) -> str:
    mode = str(task.get("mode") or "").strip()
    if mode == "architect":
        return "architect 모드: 설계 근거와 handoff만 작성한다. 최종 산출물 파일은 직접 구현하지 말고, 구현은 명시적 설계 승인 뒤에만 시작한다. 설계 산출물 + PM handoff + plan/scope/evidence 점검이 끝나면 이 카드는 완료 처리한다. 사용자 설계 승인 대기는 이 카드의 블로커가 아니라 다음 PM 게이트다."
    if mode == "implementer":
        return "implementer 모드: 설계 승인 후에만 승인된 산출물과 구현 근거를 작성할 수 있다. 승인된 산출물 + 구현 근거 + handoff 메모가 준비되면 이 카드는 완료 처리한다. reviewer/human review 대기는 구현 카드의 블로커가 아니라 다음 게이트다."
    if mode == "reviewer":
        return "reviewer 모드: 산출물, 범위, 근거, 설계 정합성을 검증하고 verification evidence를 작성한다. debugger로 명시되지 않았다면 제품 코드는 직접 수정하지 않는다."
    if mode == "debugger":
        return "debugger 모드: reviewer가 지적한 문제만 승인된 범위 안에서 수정하고, 수정 근거와 남은 리스크를 함께 보고한다."
    if str(task.get("assignee") or "") == "project_manager":
        pm_phase = str(task.get("pm_phase") or "").strip()
        if pm_phase in {"design_summary", "design_approval_gate"}:
            return "PM 설계 게이트: architect/reviewer 출력을 사용자 설계 승인을 위해 요약하는 단계다. 이 단계는 terminal final completion이 아니며, 최종 완료 메타데이터를 쓰면 안 된다. 다음 게이트는 사용자 설계 승인이고 그 뒤에 구현 작업이 열린다."
        return "PM final 단계: evidence를 다시 확인하고, 비어 있지 않은 최종 요약/보고를 작성한 뒤에만 완료 처리할 수 있다."
    return "PM workflow contract와 승인 범위를 벗어나지 말 것."


def _task_scope_paths(task: dict[str, Any], request: str) -> list[str]:
    paths = _extract_project_paths(task.get("body") or "", task.get("title") or "", request)
    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        norm = _normalize_contract_path(path)
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(norm)
    return deduped


def _completion_requirements_ko(task: dict[str, Any]) -> list[str]:
    mode = str(task.get("mode") or "").strip()
    pm_phase = str(task.get("pm_phase") or "").strip()
    if mode == "architect":
        return [
            "설계 산출물과 PM handoff 근거가 모두 작성되어 있어야 함",
            "최종 산출물 파일을 직접 구현하지 말 것",
            "사용자 설계 승인은 이 카드의 블로커가 아니라 다음 PM 게이트임",
        ]
    if mode == "implementer":
        return [
            "승인된 설계 범위 안에서만 변경할 것",
            "지정 산출물, 구현 근거, handoff 메모가 준비되면 완료 처리할 것",
            "리뷰어 대기는 구현 카드의 블로커가 아님",
        ]
    if mode == "reviewer":
        return [
            "변경 범위, 근거 파일, 설계 정합성, acceptance criteria를 함께 검토할 것",
            "pass/concern/blocked 중 하나의 verdict와 근거를 남길 것",
            "debugger로 명시되지 않았다면 제품 코드는 직접 수정하지 말 것",
        ]
    if mode == "debugger":
        return [
            "리뷰에서 지적된 범위만 수정할 것",
            "수정 근거와 남은 리스크를 함께 보고할 것",
        ]
    if str(task.get("assignee") or "") == "project_manager":
        if pm_phase in {"design_summary", "design_approval_gate"}:
            return [
                "설계 요약, 승인 판단 포인트, 남은 리스크를 사용자 판단 가능 수준으로 정리할 것",
                "이 단계에서 최종 완료 보고 형식으로 닫지 말 것",
                "다음 단계가 구현인지, 수정 설계인지 명확히 적을 것",
            ]
        return [
            "최종 산출물, 검증 결과, open risk, QA handoff를 모두 정리할 것",
            "사용자가 바로 승인/반려 판단할 수 있을 정도로 상세히 적을 것",
        ]
    return ["승인된 범위와 PM workflow contract를 벗어나지 말 것"]


def _report_requirements_ko(task: dict[str, Any]) -> list[str]:
    mode = str(task.get("mode") or "").strip()
    pm_phase = str(task.get("pm_phase") or "").strip()
    common = [
        "무엇을 왜 했는지 요약",
        "실제로 변경하거나 검토한 파일/산출물 경로",
        "검증 근거(명령, 리뷰 근거, evidence 파일)",
        "남은 리스크 또는 미해결 항목",
        "다음 단계에 넘길 handoff 메모",
    ]
    if mode == "architect":
        return common + ["설계 결정 이유와 대안 배제 이유", "구현 전에 사용자가 확인해야 할 판단 포인트"]
    if mode == "implementer":
        return common + ["핵심 로직 변경 내용", "테스트 반영 여부와 미실행 사유(있다면)"]
    if mode == "reviewer":
        return common + ["verdict(pass/concern/blocked)", "acceptance criteria별 통과/미통과 판단"]
    if mode == "debugger":
        return common + ["수정한 원인과 해결 방식", "재발 가능성 또는 추가 후속 필요 여부"]
    if str(task.get("assignee") or "") == "project_manager":
        if pm_phase in {"design_summary", "design_approval_gate"}:
            return [
                "설계 요약",
                "승인 판단에 필요한 핵심 변경점/영향 범위",
                "남은 리스크와 가정",
                "승인 시 다음 단계 / 보류 시 수정 필요 항목",
            ]
        return [
            "최종 작업 요약",
            "변경 파일/산출물 목록",
            "검증 결과와 reviewer verdict",
            "open risk와 QA handoff",
            "사용자가 최종 판단해야 할 포인트",
        ]
    return common


def _forbidden_scope_ko(task: dict[str, Any]) -> list[str]:
    mode = str(task.get("mode") or "").strip()
    pm_phase = str(task.get("pm_phase") or "").strip()
    if mode == "architect":
        return ["승인 전 구현 금지", "지정되지 않은 제품 파일 임의 수정 금지"]
    if mode == "implementer":
        return ["승인 범위를 벗어난 파일 수정 금지", "review-required를 이유로 스스로 blocked 처리 금지"]
    if mode == "reviewer":
        return ["근거 없는 pass/blocked 판정 금지", "debugger 지시 없이 제품 코드 수정 금지"]
    if str(task.get("assignee") or "") == "project_manager" and pm_phase in {"design_summary", "design_approval_gate"}:
        return ["최종 완료 보고처럼 작성 금지", "구현/테스트가 끝난 것처럼 표현 금지"]
    if str(task.get("assignee") or "") == "project_manager":
        return ["근거 없는 완료 선언 금지", "reviewer 결과와 충돌하는 요약 금지"]
    return ["승인 범위를 벗어난 작업 금지"]


def _format_task_body_ko(
    task: dict[str, Any],
    *,
    request: str,
    workspace_path: str,
    parent_ids: list[str],
    pm_phase: str | None,
    terminal_task: bool | None,
    plan_id: str | None = None,
    task_id: str | None = None,
) -> str:
    paths = _task_scope_paths(task, request)
    sections: list[str] = [str(task.get("body") or "").strip(), ""]
    sections.extend([
        "--- PM 작업 지시서 ---",
        "[작업 배경]",
        f"- 원본 요청: {request.strip()}",
        f"- 작업 제목: {str(task.get('title') or '').strip()}",
        "",
        "[작업 계약 정보]",
        f"- 작업 키: {str(task.get('key') or '').strip()}",
        f"- 담당자: {str(task.get('assignee') or '').strip()}",
        f"- 작업 모드: {str(task.get('mode') or '').strip() or '해당 없음'}",
        f"- PM 단계: {pm_phase or '해당 없음'}",
        f"- 최종 단계 여부: {terminal_task if terminal_task is not None else '해당 없음'}",
        f"- 선행 작업 ID: {', '.join(parent_ids) if parent_ids else '없음'}",
        f"- 프로젝트 경로: {workspace_path}",
        "",
        "[주요 대상 경로]",
    ])
    if paths:
        sections.extend([f"- {path}" for path in paths])
    else:
        sections.append("- 본문/요청에 명시된 승인 범위 중심으로만 작업")
    sections.extend([
        "",
        "[완료 조건]",
    ])
    sections.extend([f"- {item}" for item in _completion_requirements_ko({**task, 'pm_phase': pm_phase, 'terminal_task': terminal_task})])
    sections.extend(_claude_delegation_body_section(task))
    mode = str(task.get("mode") or "").strip()
    if plan_id and task_id and _claude_delegation_policy_for_mode(mode).get("required"):
        sections.append(_format_claude_runner_invocation_section(str(plan_id), str(task_id), mode, workspace_path))
    sections.extend([
        "",
        "[금지/주의 사항]",
    ])
    sections.extend([f"- {item}" for item in _forbidden_scope_ko({**task, 'pm_phase': pm_phase, 'terminal_task': terminal_task})])
    sections.extend([
        "",
        "[판단 기준/운영 가드레일]",
        f"- {_mode_guardrail_text({**task, 'pm_phase': pm_phase, 'terminal_task': terminal_task})}",
        "",
        "[완료 보고서 필수 항목]",
    ])
    sections.extend([f"- {item}" for item in _report_requirements_ko({**task, 'pm_phase': pm_phase, 'terminal_task': terminal_task})])
    return "\n".join(sections).strip()


def _plan_task_key_to_id(plan: dict[str, Any], field: str) -> dict[str, str]:
    mapping: dict[str, Any] = {}
    for row in plan.get(field) or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        task_id = str(row.get("task_id") or "").strip()
        if key and task_id:
            mapping[key] = task_id
    return mapping


def _reopen_design_tasks_for_revision(plan: dict[str, Any], revision_notes: str) -> dict[str, Any]:
    updates: list[dict[str, Any]] = []
    created_design_rows = {
        str(item.get("task_id") or "").strip(): item
        for item in (plan.get("created_design_tasks") or [])
        if isinstance(item, dict) and str(item.get("task_id") or "").strip()
    }
    task_ids = [
        str(item.get("task_id") or "").strip()
        for item in (plan.get("created_design_tasks") or [])
        if isinstance(item, dict)
    ]
    task_ids = [tid for tid in task_ids if tid]
    if not task_ids:
        return {"ok": True, "updates": updates}

    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
    except Exception as exc:
        return {"ok": False, "error": f"failed to connect to Kanban DB for revision recovery: {exc}", "updates": updates}

    try:
        for task_id in task_ids:
            row = kb.get_task(conn, task_id)
            if row is None:
                updates.append({"task_id": task_id, "ok": False, "error": "task not found"})
                continue
            comment = "\n".join([
                "PM revision request:",
                f"- {revision_notes}",
                "",
                "Architect instructions:",
                "- Revise the design artifact and any docs-only draft outputs to reflect the revision request.",
                "- Do NOT wait for user design approval to finish this architect task.",
                "- Your completion target is: updated design artifact + PM handoff so PM can call pm_mark_design_ready.",
            ]).strip()
            kb.add_comment(conn, task_id, "project_manager", comment)

            unblocked = False
            reopened = False
            reopened_status = None
            previous_status = getattr(row, "status", None)
            if previous_status == "blocked":
                unblocked = bool(kb.unblock_task(conn, task_id))
                if unblocked:
                    refreshed = kb.get_task(conn, task_id)
                    reopened_status = getattr(refreshed, "status", None)
            elif previous_status == "done":
                undone_parents = conn.execute(
                    "SELECT 1 FROM task_links l "
                    "JOIN tasks p ON p.id = l.parent_id "
                    "WHERE l.child_id = ? AND p.status != 'done' LIMIT 1",
                    (task_id,),
                ).fetchone()
                reopened_status = "todo" if undone_parents else "ready"
                cur = conn.execute(
                    "UPDATE tasks SET status = ? WHERE id = ? AND status = 'done'",
                    (reopened_status, task_id),
                )
                reopened = cur.rowcount == 1
            cached = created_design_rows.get(task_id)
            if cached is not None and (unblocked or reopened) and reopened_status:
                cached["status"] = reopened_status
            updates.append({
                "task_id": task_id,
                "previous_status": previous_status,
                "comment_added": True,
                "unblocked": unblocked,
                "reopened": reopened,
                "current_status": reopened_status,
            })
        return {"ok": True, "updates": updates}
    except Exception as exc:
        return {"ok": False, "error": f"revision recovery failed: {exc}", "updates": updates}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _complete_design_approval_gate_tasks(
    resolved_followup_tasks: list[dict[str, Any]],
    created_followup_tasks: list[dict[str, Any]],
    design_task_ids: set[str],
    approved_by: str,
) -> dict[str, Any]:
    updates: list[dict[str, Any]] = []
    if not resolved_followup_tasks or not created_followup_tasks or not design_task_ids:
        return {"ok": True, "updates": updates}

    gate_keys: set[str] = set()
    for task in resolved_followup_tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("assignee") or "").strip() != "project_manager":
            continue
        parents = {
            str(parent).strip()
            for parent in (task.get("parents") or [])
            if str(parent).strip()
        }
        if parents and parents.issubset(design_task_ids):
            key = str(task.get("key") or "").strip()
            if key:
                gate_keys.add(key)
    if not gate_keys:
        return {"ok": True, "updates": updates}

    gate_task_ids = [
        str(item.get("task_id") or "").strip()
        for item in created_followup_tasks
        if isinstance(item, dict) and str(item.get("key") or "").strip() in gate_keys and str(item.get("task_id") or "").strip()
    ]
    if not gate_task_ids:
        return {"ok": True, "updates": updates}

    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
    except Exception as exc:
        return {"ok": False, "error": f"failed to connect to Kanban DB for design gate completion: {exc}", "updates": updates}

    try:
        for task_id in gate_task_ids:
            row = kb.get_task(conn, task_id)
            if row is None:
                updates.append({"task_id": task_id, "ok": False, "error": "task not found"})
                continue
            if getattr(row, "status", None) == "done":
                updates.append({"task_id": task_id, "ok": True, "previous_status": "done", "completed": False})
                continue
            summary = f"Design approval recorded by {approved_by}; implementation may proceed."
            completed = bool(kb.complete_task(conn, task_id, summary=summary))
            refreshed = kb.get_task(conn, task_id)
            updates.append({
                "task_id": task_id,
                "ok": completed,
                "previous_status": getattr(row, "status", None),
                "current_status": getattr(refreshed, "status", None),
                "completed": completed,
            })
        return {"ok": all(bool(item.get("ok")) for item in updates), "updates": updates}
    except Exception as exc:
        return {"ok": False, "error": f"design gate completion failed: {exc}", "updates": updates}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _infer_execution_phase(plan: dict[str, Any]) -> str:
    explicit = str(plan.get("execution_phase") or "").strip()
    if explicit:
        return explicit
    status = str(plan.get("status") or "").strip()
    if status == "preflight_blocked":
        last_stage = str(plan.get("last_execution_stage") or "").strip()
        if last_stage.startswith("implementation_"):
            return "implementation"
        if any(plan.get(key) for key in ("implementation_preflight_result", "implementation_repair_result", "implementation_reverification_result")):
            return "implementation"
        if _plan_kind(plan) == "execution":
            return "implementation"
        if any(plan.get(key) for key in ("preflight_result", "repair_result", "reverification_result")):
            if str(plan.get("design_status") or "").strip() == "awaiting_approval" and plan.get("created_design_tasks"):
                return "implementation"
            return "design"
    if status in {"design_in_progress", "awaiting_design_approval", "design_revision_requested"}:
        return "design"
    if plan.get("created_followup_tasks") or str(plan.get("review_status") or "").strip() not in {"", "not_started"} or str(plan.get("final_status") or "").strip() not in {"", "not_started"}:
        return "implementation"
    return "plan"


def _ensure_plan_workflow_fields(plan: dict[str, Any]) -> dict[str, Any]:
    normalized_plan_kind = _normalize_plan_kind(plan.get("plan_kind"))
    plan["plan_kind"] = normalized_plan_kind if normalized_plan_kind in _PLAN_KINDS else "staged"
    if str(plan.get("status") or "").strip() == "awaiting_approval":
        plan["status"] = "awaiting_pm_approval"
    if not str(plan.get("design_status") or "").strip():
        plan["design_status"] = "not_started"
    plan.setdefault("design_summary", "")
    plan.setdefault("design_artifact_path", "")
    if not isinstance(plan.get("design_revision_notes"), list):
        plan["design_revision_notes"] = []
    plan.setdefault("design_requested_by", None)
    plan.setdefault("design_ready_at", None)
    plan.setdefault("design_approved_at", None)
    plan.setdefault("design_approved_by", None)
    if not isinstance(plan.get("created_design_tasks"), list):
        plan["created_design_tasks"] = []
    if not isinstance(plan.get("created_followup_tasks"), list):
        plan["created_followup_tasks"] = []
    if not str(plan.get("review_status") or "").strip():
        plan["review_status"] = "not_started"
    plan.setdefault("review_summary", "")
    if not isinstance(plan.get("review_metadata"), dict):
        plan["review_metadata"] = {}
    if not isinstance(plan.get("review_followup_tasks"), list):
        plan["review_followup_tasks"] = []
    if not str(plan.get("final_status") or "").strip():
        plan["final_status"] = "not_started"
    plan.setdefault("final_summary", "")
    if not isinstance(plan.get("final_report_metadata"), dict):
        plan["final_report_metadata"] = {}
    plan["execution_phase"] = _infer_execution_phase(plan)
    plan.setdefault("phase1_executed_at", None)
    plan.setdefault("phase2_executed_at", None)
    return plan


def _plan_mode_for_key(plan: dict[str, Any], key: str) -> str:
    tasks = [t for t in (plan.get("tasks") or []) if isinstance(t, dict)]
    task_by_key = {str(t.get("key") or "").strip(): t for t in tasks if str(t.get("key") or "").strip()}
    children_map = _task_children_map(tasks)
    final_task_keys = set(_final_phase_task_keys(tasks, _build_workflow_contract(plan).get("expected_deliverables") or []))
    for task in tasks:
        if str(task.get("key") or "") == str(key or ""):
            if task.get("mode"):
                return str(task.get("mode") or "")
            if str(task.get("key") or "") in final_task_keys:
                return "final"
            if str(task.get("assignee") or "") == "project_manager":
                return _infer_pm_task_phase(task, task_by_key, children_map)
    return ""


def _task_summary_from_conn(conn: Any, task_id: str) -> str:
    try:
        row = conn.execute(
            """
            SELECT summary
              FROM task_runs
             WHERE task_id = ? AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if row and row[0]:
            return str(row[0]).strip()
    except Exception:
        pass
    return ""


def _task_completion_metadata_from_conn(conn: Any, task_id: str) -> dict[str, Any]:
    try:
        row = conn.execute(
            """
            SELECT metadata
              FROM task_runs
             WHERE task_id = ? AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if row and row[0]:
            raw = row[0]
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str):
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
    except Exception:
        pass
    return {}


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def _reviewer_evidence_for_export(plan: dict[str, Any], reviewer_meta: dict[str, Any]) -> list[str]:
    direct = _string_list(reviewer_meta.get("evidence"))
    if direct:
        return direct
    contract = _build_workflow_contract(plan)
    artifacts = contract.get("artifacts") if isinstance(contract.get("artifacts"), dict) else {}
    expected = _string_list(contract.get("expected_deliverables"))
    changed_files = _string_list(reviewer_meta.get("changed_files"))
    candidates = [
        *changed_files,
        *[item for item in expected if item.startswith("review/") or item.startswith("reports/")],
        str(artifacts.get("verification") or "").strip(),
    ]
    seen: set[str] = set()
    evidence: list[str] = []
    for item in candidates:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        evidence.append(text)
    return evidence


def _review_status_from_metadata(reviewer_meta: dict[str, Any]) -> str:
    verdict = str(reviewer_meta.get("verdict") or "").strip().lower()
    scope_status = str(reviewer_meta.get("scope_status") or "").strip().lower()
    unmet = reviewer_meta.get("unmet_requirements") if isinstance(reviewer_meta.get("unmet_requirements"), list) else []
    acceptance_checks = reviewer_meta.get("acceptance_checks") if isinstance(reviewer_meta.get("acceptance_checks"), list) else []
    has_nonpass_acceptance = any(
        isinstance(item, dict) and str(item.get("status") or "").strip().lower() not in {"", "pass", "approved", "ok", "success", "done"}
        for item in acceptance_checks
    )
    if verdict == "blocked" or scope_status == "out_of_scope":
        return "blocked"
    if verdict == "concern" or unmet or has_nonpass_acceptance:
        return "concern"
    return "approved"


def _create_reviewer_followup_task(plan: dict[str, Any], reviewer_task_id: str, reviewer_meta: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(plan.get("plan_id") or "").strip()
    if not plan_id or not reviewer_task_id:
        return {"ok": False, "error": "plan_id and reviewer_task_id are required"}
    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
    except Exception as exc:
        return {"ok": False, "error": f"failed to connect to Kanban DB for reviewer follow-up creation: {exc}"}

    try:
        reviewer_task = kb.get_task(conn, reviewer_task_id)
        if reviewer_task is None:
            return {"ok": False, "error": "reviewer task not found", "task_id": reviewer_task_id}
        review_status = _review_status_from_metadata(reviewer_meta)
        verdict = str(reviewer_meta.get("verdict") or review_status).strip() or review_status
        scope_status = str(reviewer_meta.get("scope_status") or "").strip() or "within_allowed_paths"
        summary = str(reviewer_meta.get("summary") or plan.get("review_summary") or "").strip() or "Reviewer flagged follow-up work."
        unmet = [str(item).strip() for item in (reviewer_meta.get("unmet_requirements") or []) if str(item).strip()]
        acceptance_checks = reviewer_meta.get("acceptance_checks") if isinstance(reviewer_meta.get("acceptance_checks"), list) else []
        rendered_checks = []
        for item in acceptance_checks:
            if isinstance(item, dict):
                criterion = str(item.get("criterion") or "").strip()
                status = str(item.get("status") or "").strip()
                notes = str(item.get("notes") or "").strip()
                text = criterion or "acceptance check"
                if status:
                    text += f" [{status}]"
                if notes:
                    text += f" — {notes}"
                rendered_checks.append(text)
            elif str(item).strip():
                rendered_checks.append(str(item).strip())
        blocked_reason = str(reviewer_meta.get("blocked_reason") or "").strip()
        design_alignment = str(reviewer_meta.get("design_alignment_summary") or "").strip()
        changed_files = [str(item).strip() for item in (reviewer_meta.get("changed_files") or []) if str(item).strip()]
        evidence = [str(item).strip() for item in (reviewer_meta.get("evidence") or []) if str(item).strip()]
        lines = [
            f"PM auto-created this follow-up because reviewer status is {review_status}.",
            "",
            f"Plan ID: {plan_id}",
            f"Source reviewer task: {reviewer_task_id}",
            f"Reviewer verdict: {verdict}",
            f"Reviewer scope status: {scope_status}",
            f"Reviewer summary: {summary}",
        ]
        if blocked_reason:
            lines.append(f"Blocked reason: {blocked_reason}")
        if unmet:
            lines.append("")
            lines.append("Unmet requirements:")
            lines.extend(f"- {item}" for item in unmet)
        if rendered_checks:
            lines.append("")
            lines.append("Acceptance checks:")
            lines.extend(f"- {item}" for item in rendered_checks)
        if design_alignment:
            lines.append("")
            lines.append("Design alignment summary:")
            lines.append(f"- {design_alignment}")
        if changed_files:
            lines.append("")
            lines.append("Reviewer changed files:")
            lines.extend(f"- {item}" for item in changed_files)
        if evidence:
            lines.append("")
            lines.append("Reviewer evidence:")
            lines.extend(f"- {item}" for item in evidence)
        lines.extend([
            "",
            "Required outcome:",
            "- Address every unmet requirement or non-pass acceptance check.",
            "- Update the implementation/design evidence as needed.",
            "- Leave an explicit handoff summary for PM once the follow-up is done.",
        ])
        title_status = "blocked-fix" if review_status == "blocked" else "concern-followup"
        task_id = kb.create_task(
            conn,
            title=f"{title_status}: {plan_id}",
            body="\n".join(lines).strip(),
            assignee="backend-implementer",
            created_by="project_manager",
            workspace_kind=reviewer_task.workspace_kind,
            workspace_path=reviewer_task.workspace_path,
            tenant=reviewer_task.tenant,
            parents=[reviewer_task_id],
            idempotency_key=f"pm-review-followup:{plan_id}:{reviewer_task_id}:{review_status}",
        )
        followup_scope_paths = _derive_reviewer_followup_scope_paths(plan, reviewer_meta)
        contract_update = _widen_plan_contract_for_reviewer_followup(plan, followup_scope_paths)
        contract_write = _write_workflow_contract(plan)
        if contract_write.get("ok"):
            plan["workflow_contract"] = {"path": contract_write.get("path"), "ok": True}
        scope_export = _export_approved_scope(
            plan,
            "implementer",
            task_id,
            approved_by=str(plan.get("design_approved_by") or plan.get("approved_by") or "project_manager").strip() or "project_manager",
            approved_at=int(plan.get("design_approved_at") or plan.get("approved_at") or int(time.time())),
            parent_task_ids=[reviewer_task_id],
        )
        created_task = kb.get_task(conn, task_id)
        final_task_id = ""
        for item in plan.get("created_followup_tasks") or []:
            if isinstance(item, dict) and str(item.get("task_id") or "") and str(item.get("key") or ""):
                mode = _plan_mode_for_key(plan, str(item.get("key") or ""))
                if mode == "final":
                    final_task_id = str(item.get("task_id") or "")
                    break
        if final_task_id and review_status == "concern":
            kb.add_comment(
                conn,
                final_task_id,
                "project_manager",
                f"PM auto-created reviewer follow-up task {task_id} because reviewer reported concern. Reflect this in the final report as needs_followup unless the follow-up is resolved first.",
            )
        return {
            "ok": True,
            "task_id": task_id,
            "title": created_task.title if created_task else None,
            "status": created_task.status if created_task else None,
            "review_status": review_status,
            "final_task_notified": bool(final_task_id and review_status == "concern"),
            "followup_scope_paths": followup_scope_paths,
            "contract_update": contract_update,
            "scope_export": scope_export,
            "workflow_contract_written": bool(contract_write.get("ok")),
        }
    except Exception as exc:
        return {"ok": False, "error": f"reviewer follow-up creation failed: {exc}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _path_allowed_by_scope(path: str, allowed_paths: list[str]) -> bool:
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return False
    for allowed in allowed_paths:
        scope = str(allowed or "").strip().replace("\\", "/")
        if not scope:
            continue
        if scope.endswith("/"):
            if text.startswith(scope):
                return True
        elif text == scope:
            return True
    return False


def _update_cached_created_task_status(plan: dict[str, Any], task_id: str, status: str) -> None:
    for field in ("created_design_tasks", "created_followup_tasks", "created_tasks"):
        for row in plan.get(field) or []:
            if isinstance(row, dict) and str(row.get("task_id") or "") == str(task_id):
                row["status"] = status


def _find_plan_for_task(profile: str, task_id: str) -> tuple[dict[str, Any], str, dict[str, Any], str] | tuple[None, None, None, None]:
    data = _load_plans(profile)
    plans = data.get("plans") if isinstance(data.get("plans"), dict) else {}
    for plan_id, plan in plans.items():
        if not isinstance(plan, dict):
            continue
        for field in ("created_design_tasks", "created_followup_tasks"):
            for row in plan.get(field) or []:
                if isinstance(row, dict) and str(row.get("task_id") or "") == str(task_id):
                    phase = _plan_mode_for_key(plan, str(row.get("key") or ""))
                    return data, plan_id, plan, phase
    return None, None, None, None


def handle_completed_pm_workflow_task(conn: Any, task_id: str, *, profile: str = "project_manager") -> dict[str, Any]:
    data, plan_id, plan, phase = _find_plan_for_task(profile, task_id)
    if not data or not plan_id or not isinstance(plan, dict):
        return {"ok": False, "reason": "task not tracked by PM workflow", "task_id": task_id}
    plan = _ensure_plan_workflow_fields(plan)
    _update_cached_created_task_status(plan, task_id, "done")
    now = int(time.time())
    info: dict[str, Any] = {"ok": True, "plan_id": plan_id, "task_id": task_id, "phase": phase, "actions": []}
    if phase == "architect" and plan.get("status") in {"design_in_progress", "design_revision_requested"}:
        summary = _task_summary_from_conn(conn, task_id) or "Architect design evidence is ready; awaiting explicit user design approval."
        artifact = str(plan.get("design_artifact_path") or "").strip() or _build_workflow_contract(plan).get("artifacts", {}).get("design") or ""
        plan["status"] = "awaiting_design_approval"
        plan["design_status"] = "awaiting_approval"
        plan["design_summary"] = summary
        if artifact:
            plan["design_artifact_path"] = artifact
        plan["design_ready_at"] = now
        plan["design_requested_by"] = "pm_workflow_core"
        plan["updated_at"] = now
        contract_result = _write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract_result.get("path"), "ok": contract_result.get("ok", False)}
        info["actions"].append("marked_design_ready")
    elif phase == "implementer":
        followups = {str(item.get("key") or ""): item for item in (plan.get("created_followup_tasks") or []) if isinstance(item, dict)}
        reviewer_key = next((str(t.get("key") or "") for t in (plan.get("tasks") or []) if isinstance(t, dict) and t.get("mode") == "reviewer"), "")
        reviewer_row = followups.get(reviewer_key)
        reviewer_task_id = str(reviewer_row.get("task_id") or "") if isinstance(reviewer_row, dict) else ""
        if reviewer_task_id:
            export = _export_approved_scope(plan, "reviewer", reviewer_task_id, approved_by=plan.get("design_approved_by"), approved_at=plan.get("design_approved_at") or now)
            plan["approved_scope_export"] = export
            plan["updated_at"] = now
            info["actions"].append("exported_reviewer_scope")
    elif phase == "reviewer":
        reviewer_meta = _task_completion_metadata_from_conn(conn, task_id)
        nested_reviewer_result = reviewer_meta.get("reviewer_result") if isinstance(reviewer_meta.get("reviewer_result"), dict) else {}
        if nested_reviewer_result:
            reviewer_meta = {**reviewer_meta, **nested_reviewer_result}
        verdict = str(reviewer_meta.get("verdict") or "").strip()
        scope_status = str(reviewer_meta.get("scope_status") or "").strip()
        review_status = _review_status_from_metadata(reviewer_meta)
        plan["review_verdict"] = verdict or None
        plan["review_scope_status"] = scope_status or None
        plan["review_summary"] = str(reviewer_meta.get("summary") or _task_summary_from_conn(conn, task_id) or "").strip() or None
        plan["review_metadata"] = reviewer_meta or {}
        reviewer_followup = None
        if review_status in {"concern", "blocked"}:
            reviewer_followup = _create_reviewer_followup_task(plan, task_id, reviewer_meta)
            followup_task_id = str((reviewer_followup or {}).get("task_id") or "").strip()
            if followup_task_id and all(str(item.get("task_id") or "") != followup_task_id for item in (plan.get("review_followup_tasks") or []) if isinstance(item, dict)):
                plan.setdefault("review_followup_tasks", []).append({
                    "task_id": followup_task_id,
                    "review_status": review_status,
                    "source_task_id": task_id,
                })
        if review_status == "blocked":
            plan["review_status"] = "blocked"
            plan["updated_at"] = now
            info["actions"].append("review_blocked_final_handoff")
        else:
            plan["review_status"] = review_status
            followups = {str(item.get("key") or ""): item for item in (plan.get("created_followup_tasks") or []) if isinstance(item, dict)}
            final_keys = _final_phase_task_keys(plan.get("tasks") or [], _build_workflow_contract(plan).get("expected_deliverables") or [])
            final_key = str(final_keys[0] or "") if final_keys else ""
            final_row = followups.get(final_key)
            final_task_id = str(final_row.get("task_id") or "") if isinstance(final_row, dict) else ""
            if final_task_id:
                exported_reviewer_result = {
                    "verdict": verdict or ("concern" if review_status == "concern" else "pass"),
                    "scope_status": scope_status or "within_allowed_paths",
                    "summary": plan.get("review_summary") or "",
                    "evidence": _reviewer_evidence_for_export(plan, reviewer_meta),
                    "changed_files": reviewer_meta.get("changed_files") or [],
                    "validated_artifacts": reviewer_meta.get("validated_artifacts") or [],
                    "acceptance_checks": reviewer_meta.get("acceptance_checks") or [],
                    "unmet_requirements": reviewer_meta.get("unmet_requirements") or [],
                    "design_alignment_summary": reviewer_meta.get("design_alignment_summary") or "",
                }
                if reviewer_meta.get("blocked_reason"):
                    exported_reviewer_result["blocked_reason"] = reviewer_meta.get("blocked_reason")
                if reviewer_followup and reviewer_followup.get("task_id"):
                    exported_reviewer_result["followup_task_ids"] = [str(reviewer_followup.get("task_id"))]
                if review_status == "concern" and reviewer_followup and (reviewer_followup.get("scope_export") or {}).get("ok"):
                    plan["approved_scope_export"] = reviewer_followup.get("scope_export")
                    info["actions"].append("deferred_final_scope_until_followup")
                else:
                    export = _export_approved_scope(
                        plan,
                        "final",
                        final_task_id,
                        approved_by=plan.get("design_approved_by"),
                        approved_at=plan.get("design_approved_at") or now,
                        reviewer_result=exported_reviewer_result,
                        parent_task_ids=[task_id],
                    )
                    plan["approved_scope_export"] = export
                    info["actions"].append("exported_final_scope")

        if reviewer_followup and reviewer_followup.get("task_id"):
            info["review_followup_task_id"] = reviewer_followup.get("task_id")
            info["actions"].append("created_review_followup_task")
    elif phase == "final":
        final_meta = _task_completion_metadata_from_conn(conn, task_id)
        plan["final_report_metadata"] = final_meta or {}
        plan["final_summary"] = str(final_meta.get("report_summary") or _task_summary_from_conn(conn, task_id) or "").strip() or None
        plan["final_user_report"] = str(final_meta.get("user_report") or "").strip() or None
        plan["final_status"] = str(final_meta.get("completion_status") or "completed").strip() or "completed"
        plan["status"] = "completed" if plan["final_status"] == "completed" else "completed_with_followup"
        plan["completed_at"] = now
        plan["updated_at"] = now
        info["actions"].append("recorded_final_report")
    else:
        info["actions"].append("no_transition")
    data.setdefault("plans", {})[plan_id] = plan
    _save_plans(data, profile)
    return info


def handle_blocked_pm_workflow_task(conn: Any, task_id: str, *, profile: str = "project_manager") -> dict[str, Any]:
    data, plan_id, plan, phase = _find_plan_for_task(profile, task_id)
    if not data or not plan_id or not isinstance(plan, dict):
        return {"ok": False, "reason": "task not tracked by PM workflow", "task_id": task_id}
    plan = _ensure_plan_workflow_fields(plan)
    _update_cached_created_task_status(plan, task_id, "blocked")
    now = int(time.time())
    summary = _task_summary_from_conn(conn, task_id) or ""
    plan["last_blocked_task"] = {
        "task_id": task_id,
        "phase": phase,
        "summary": summary,
        "blocked_at": now,
    }
    plan["updated_at"] = now
    info: dict[str, Any] = {"ok": True, "plan_id": plan_id, "task_id": task_id, "phase": phase, "actions": []}
    if phase == "architect":
        plan["design_status"] = "blocked"
        if summary:
            plan["design_summary"] = summary
        info["actions"].append("recorded_design_block")
    elif phase == "reviewer":
        plan["review_status"] = "blocked"
        if summary:
            plan["review_summary"] = summary
        info["actions"].append("recorded_review_block")
    elif phase == "final":
        plan["final_status"] = "blocked"
        if summary:
            plan["final_summary"] = summary
        info["actions"].append("recorded_final_block")
    else:
        info["actions"].append("recorded_block")
    data.setdefault("plans", {})[plan_id] = plan
    _save_plans(data, profile)
    return info


def pm_create_kanban_workflow(
    request: str,
    tasks: list[dict[str, Any]],
    tenant: str | None = None,
    project_path: str | None = None,
    created_by: str = "project_manager",
    plan_id: str | None = None,
) -> str:
    """Internal creator: create verified PM Kanban workflow and return task IDs."""
    normalized, err = _normalize_tasks(tasks, require_pm_finalizer=_runtime_requires_pm_finalizer(tasks if isinstance(tasks, list) else []))
    if err:
        return _error(err)

    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
    except Exception as exc:
        return _error(f"failed to connect to Kanban DB: {exc}")

    try:
        try:
            board = kb.get_current_board()
        except Exception:
            board = os.environ.get("HERMES_KANBAN_BOARD") or "default"

        assignees = sorted(_WORKER_ASSIGNEES | {"project_manager"})
        workload_before = _collect_workload(kb, conn, assignees)
        workspace_path, project_path_override_ignored, configured_workspace = _resolve_workspace(project_path)
        if not workspace_path:
            return _error(
                "project_path is required: set terminal.cwd in the project_manager profile or pass project_path explicitly. Refusing to create scratch Kanban workspaces."
            )
        tenant = tenant or os.environ.get("HERMES_TENANT") or None

        created: list[dict[str, Any]] = []
        key_to_id: dict[str, str] = {}
        task_by_key = {str(t.get("key") or "").strip(): t for t in normalized}
        children_map = _task_children_map(normalized)
        try:
            for task in normalized:
                parent_ids = [key_to_id.get(p, p) for p in task["parents"]]
                pm_phase = _infer_pm_task_phase(task, task_by_key, children_map) if task.get("assignee") == "project_manager" else None
                terminal_task = _is_terminal_pm_task(task, children_map) if task.get("assignee") == "project_manager" else None
                body = _format_task_body_ko(
                    task,
                    request=request,
                    workspace_path=workspace_path,
                    parent_ids=parent_ids,
                    pm_phase=pm_phase,
                    terminal_task=terminal_task,
                )
                tid = kb.create_task(
                    conn,
                    title=task["title"],
                    body=body,
                    assignee=task["assignee"],
                    created_by=created_by,
                    workspace_kind="dir",
                    workspace_path=workspace_path,
                    tenant=tenant,
                    priority=task["priority"],
                    parents=parent_ids,
                )
                task_plan_id = str(plan_id or "").strip()
                task_mode = str(task.get("mode") or "").strip()
                runner_prompt = None
                if task_plan_id and _claude_delegation_policy_for_mode(task_mode).get("required"):
                    body = _format_task_body_ko(
                        task,
                        request=request,
                        workspace_path=workspace_path,
                        parent_ids=parent_ids,
                        pm_phase=pm_phase,
                        terminal_task=terminal_task,
                        plan_id=task_plan_id,
                        task_id=tid,
                    )
                    runner_prompt = _write_claude_runner_prompt(
                        project_path=workspace_path,
                        plan_id=task_plan_id,
                        task_id=tid,
                        mode=task_mode,
                        request=request,
                        task=task,
                        parent_ids=parent_ids,
                    )
                    with kb.write_txn(conn):
                        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, tid))
                key_to_id[task["key"]] = tid
                row = kb.get_task(conn, tid)
                if row is None:
                    return _error("created task could not be verified", task_id=tid, created=created)
                row_workspace_kind = getattr(row, "workspace_kind", None)
                row_workspace_path = getattr(row, "workspace_path", None)
                if row_workspace_kind != "dir" or row_workspace_path != workspace_path:
                    return _error(
                        "created task workspace verification failed",
                        task_id=tid,
                        expected_workspace_kind="dir",
                        expected_workspace_path=workspace_path,
                        actual_workspace_kind=row_workspace_kind,
                        actual_workspace_path=row_workspace_path,
                        created=created,
                    )
                created.append({
                    "key": task["key"],
                    "task_id": tid,
                    "title": row.title,
                    "assignee": row.assignee,
                    "status": row.status,
                    "mode": task.get("mode"),
                    "pm_phase": pm_phase,
                    "terminal_task": terminal_task,
                    "parents": parent_ids,
                    "workspace_kind": row_workspace_kind,
                    "workspace_path": row_workspace_path,
                    "runner_prompt": runner_prompt,
                })
        except Exception as exc:
            return _error(f"workflow creation failed: {exc}", created=created, workload_before=workload_before)

        if len(created) != len(normalized) or any(not c.get("task_id") for c in created):
            return _error("workflow verification failed: not all tasks have real IDs", created=created)

        workload_after = _collect_workload(kb, conn, assignees)
        return _json({
            "ok": True,
            "board": board,
            "project_path": workspace_path,
            "configured_workspace": configured_workspace,
            "ignored_project_path_override": project_path_override_ignored,
            "tenant": tenant,
            "preflight_workload": workload_before,
            "created_tasks": created,
            "workload_after": workload_after,
            "required_user_report": "사용자에게는 반드시 실제 task_id를 기준으로 보고하라. T1/T2 같은 계획용 키를 등록 증거로 쓰지 말고, 변경 범위·검증 근거·남은 리스크를 한국어로 상세히 설명해야 한다.",
        })
    finally:
        try:
            conn.close()
        except Exception:
            pass


def pm_create_plan(
    request: str,
    summary: str,
    tasks: list[dict[str, Any]],
    acceptance_criteria: list[str] | None = None,
    validation_plan: list[str] | None = None,
    risks: list[str] | None = None,
    contract: dict[str, Any] | None = None,
    tenant: str | None = None,
    project_path: str | None = None,
    created_by: str = "project_manager",
    plan_kind: str = "staged",
) -> str:
    """Store a PM plan awaiting explicit user plan approval without creating Kanban cards."""
    request = str(request or "").strip()
    summary = str(summary or "").strip()
    if not request:
        return _error("request is required")
    if not summary:
        return _error("summary is required")
    normalized_plan_kind = _normalize_plan_kind(plan_kind)
    if normalized_plan_kind not in _PLAN_KINDS:
        return _error("plan_kind must be one of canonical PM workflow kinds", validation_errors=[f"plan_kind must be one of {sorted(_PLAN_KINDS)}, got {plan_kind!r}"])
    normalized, err = _normalize_tasks(tasks, require_pm_finalizer=normalized_plan_kind not in {"design", "mapping"})
    if err:
        return _error(err)
    contract = dict(contract or {})
    source_enriched_contract, source_errors = _inherit_contract_from_source_plan({
        "plan_kind": normalized_plan_kind,
        "contract": contract,
    })
    contract = source_enriched_contract
    if source_errors:
        return _error("plan kind violates canonical PM workflow structure", validation_errors=source_errors)
    contract_errors = _validate_explicit_contract(contract)
    if contract_errors:
        return _error("explicit contract violates canonical PM workflow schema", validation_errors=contract_errors)
    workspace_path, ignored, configured_workspace = _resolve_workspace(project_path)
    if not workspace_path:
        return _error("configured project workspace is required before creating a PM plan")

    preview_plan = {
        "plan_id": "preview",
        "plan_kind": normalized_plan_kind,
        "request": request,
        "summary": summary,
        "tasks": normalized,
        "acceptance_criteria": acceptance_criteria or [],
        "validation_plan": validation_plan or [],
        "project_path": workspace_path,
        "contract": contract or {},
    }
    preview_contract = _build_workflow_contract(preview_plan)
    graph_errors = []
    if normalized_plan_kind == "staged":
        graph_errors = _validate_staged_plan_graph(normalized, preview_contract.get("expected_deliverables") or [])
    invariant_errors = _validate_workflow_contract_invariants(preview_plan, preview_contract)
    validation_errors = [*graph_errors, *invariant_errors]
    if validation_errors:
        error_message = "plan graph violates staged workflow contract"
        if normalized_plan_kind != "staged":
            error_message = "plan kind violates canonical PM workflow structure"
        return _error(
            error_message,
            validation_errors=validation_errors,
            expected_deliverables=preview_contract.get("expected_deliverables") or [],
            implementation_paths=preview_contract.get("implementation_paths") or [],
        )

    data = _load_plans()
    plan_id = _new_plan_id()
    now = int(time.time())
    plan = {
        "plan_id": plan_id,
        "plan_kind": normalized_plan_kind,
        "status": "awaiting_pm_approval",
        "request": request,
        "summary": summary,
        "tasks": normalized,
        "acceptance_criteria": acceptance_criteria or [],
        "validation_plan": validation_plan or [],
        "risks": risks or [],
        "contract": contract or {},
        "tenant": tenant or os.environ.get("HERMES_TENANT") or None,
        "project_path": workspace_path,
        "configured_workspace": configured_workspace,
        "ignored_project_path_override": ignored,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "approved_at": None,
        "approved_by": None,
        "executed_at": None,
        "created_tasks": [],
        "design_status": "not_started",
        "design_summary": "",
        "design_artifact_path": "",
        "design_revision_notes": [],
        "design_requested_by": None,
        "design_ready_at": None,
        "design_approved_at": None,
        "design_approved_by": None,
        "created_design_tasks": [],
        "created_followup_tasks": [],
        "execution_phase": "plan",
        "phase1_executed_at": None,
        "phase2_executed_at": None,
    }
    data.setdefault("plans", {})[plan_id] = plan
    contract_result = _write_workflow_contract(plan)
    plan["workflow_contract"] = {
        "path": contract_result.get("path"),
        "ok": contract_result.get("ok"),
    }
    _save_plans(data)
    return _json({
        "ok": True,
        "mode": "plan",
        "plan_id": plan_id,
        "status": "awaiting_pm_approval",
        "plan_kind": normalized_plan_kind,
        "project_path": workspace_path,
        "ignored_project_path_override": ignored,
        "workflow_contract": plan.get("workflow_contract"),
        "expected_deliverables": contract_result.get("contract", {}).get("expected_deliverables", []),
        "tasks": normalized,
        "acceptance_criteria": acceptance_criteria or [],
        "validation_plan": validation_plan or [],
        "risks": risks or [],
        "required_user_report": "Report this plan_id, the PM brief/acceptance criteria, and ask the user to approve the plan before architect work starts. Do not claim Kanban cards were created.",
        "approval_phrase": f"승인. {plan_id} 진행해",
    })


def pm_get_plan_status(plan_id: str | None = None) -> str:
    data = _load_plans()
    plans = data.get("plans", {})
    if plan_id:
        plan = plans.get(str(plan_id).strip())
        if not plan:
            return _error("plan_id not found", plan_id=plan_id)
        plan = _ensure_plan_workflow_fields(plan)
        return _json({"ok": True, "plan": plan})
    recent = sorted((_ensure_plan_workflow_fields(dict(p)) for p in plans.values()), key=lambda p: p.get("updated_at", 0), reverse=True)[:10]
    return _json({"ok": True, "recent_plans": recent})


def pm_reject_plan(plan_id: str, rejected_by: str = "user", reason: str = "") -> str:
    """Reject / deny a stored PM-plan-approval or design-approval plan, persisting the rejection."""
    plan_id = str(plan_id or "").strip()
    if not plan_id:
        return _error("plan_id is required")
    data = _load_plans()
    plan = data.get("plans", {}).get(plan_id)
    if not plan:
        return _error("plan_id not found", plan_id=plan_id)
    if plan.get("status") not in {"awaiting_pm_approval", "awaiting_approval", "approved", "design_in_progress", "awaiting_design_approval", "design_revision_requested"}:
        return _error(
            "plan is not in a rejectable state",
            plan_id=plan_id,
            status=plan.get("status"),
        )
    now = int(time.time())
    plan["status"] = "rejected"
    plan["rejected_by"] = rejected_by
    plan["rejected_reason"] = reason
    plan["updated_at"] = now
    _save_plans(data)
    return _json({
        "ok": True,
        "plan_id": plan_id,
        "status": "rejected",
        "rejected_by": rejected_by,
        "reason": reason,
    })


def pm_execute_plan(plan_id: str, approved_by: str = "user") -> str:
    """Execute phase 1 of an approved PM plan by creating architect/design Kanban cards only."""
    plan_id = str(plan_id or "").strip()
    if not plan_id:
        return _error("plan_id is required. Create a plan first and ask the user to approve that plan_id.")
    data = _load_plans()
    plan = data.get("plans", {}).get(plan_id)
    if not plan:
        return _error("plan_id not found. Create a plan first and ask the user to approve that plan_id.", plan_id=plan_id)
    plan = _ensure_plan_workflow_fields(plan)
    status = str(plan.get("status") or "")
    if status == "executed":
        return _error("plan already fully executed", plan_id=plan_id, created_tasks=plan.get("created_tasks", []))
    if status == "rejected":
        return _error("plan was rejected and cannot be executed. Create a new plan if needed.", plan_id=plan_id)
    if status == "design_revision_requested":
        return _error(
            "design revision is already in progress; reuse the existing architect tasks and wait for pm_mark_design_ready instead of re-executing the design phase",
            plan_id=plan_id,
            status=status,
            created_design_tasks=plan.get("created_design_tasks", []),
        )
    if status not in {"awaiting_pm_approval", "awaiting_approval", "approved", "preflight_blocked"}:
        return _error("plan is not executable for design phase", plan_id=plan_id, status=status)

    execution_phase = str(plan.get("execution_phase") or "")
    plan_kind = _plan_kind(plan)
    retryable_design_preflight_block = status == "preflight_blocked" and execution_phase == "design"
    retryable_execution_preflight_block = status == "preflight_blocked" and plan_kind == "execution" and execution_phase == "implementation"
    if status == "preflight_blocked" and not (retryable_design_preflight_block or retryable_execution_preflight_block):
        return _error(
            "plan is blocked in implementation preflight; retry with pm_approve_design_and_execute",
            plan_id=plan_id,
            status=status,
            execution_phase=execution_phase or None,
        )

    if plan_kind == "execution":
        assignees = sorted({str(t.get("assignee") or "").strip() for t in (plan.get("tasks") or []) if t.get("assignee")})
        preflight_result = _run_worker_preflight(assignees)
        plan["last_execution_stage"] = "implementation_preflight"
        plan["implementation_preflight_result"] = preflight_result
        repair_result = None
        if not preflight_result.get("ok"):
            repair_result = _attempt_preflight_repair(preflight_result)
            plan["last_execution_stage"] = "implementation_repair"
            plan["implementation_repair_result"] = repair_result
        effective_preflight = repair_result or preflight_result
        plan["implementation_reverification_result"] = effective_preflight if repair_result is not None else None
        if not effective_preflight.get("ok"):
            now = int(time.time())
            plan["status"] = "preflight_blocked"
            plan["updated_at"] = now
            plan["execution_phase"] = "implementation"
            plan["created_followup_tasks"] = []
            plan["created_tasks"] = []
            _save_plans(data)
            return _json({
                "ok": False,
                "plan_id": plan_id,
                "error": "worker preflight failed after repair attempt",
                "status": "preflight_blocked",
                "phase": "implementation",
                "preflight_result": preflight_result,
                "repair_result": repair_result,
                "reverification_result": effective_preflight if repair_result is not None else None,
                "hint": "Retry with pm_execute_plan after resolving worker issues.",
            })
        result_text = pm_create_kanban_workflow(
            request=plan.get("request") or "",
            tasks=plan.get("tasks") or [],
            tenant=plan.get("tenant"),
            project_path=plan.get("project_path"),
            created_by="project_manager",
            plan_id=plan_id,
        )
        try:
            result = json.loads(result_text)
        except Exception:
            return _error("workflow returned non-json result", raw=result_text)
        if not result.get("ok"):
            plan["status"] = "execution_failed"
            plan["execution_error"] = result
            plan["updated_at"] = int(time.time())
            _save_plans(data)
            return _json({"ok": False, "plan_id": plan_id, "error": "execution plan failed", "workflow_result": result})
        followup_errors = _validate_followup_graph(plan, plan.get("tasks") or [], result.get("created_tasks", []))
        if followup_errors:
            plan["status"] = "execution_failed"
            plan["execution_error"] = {"ok": False, "error": "created execution graph violates workflow contract", "validation_errors": followup_errors, "workflow_result": result}
            plan["updated_at"] = int(time.time())
            _save_plans(data)
            return _json({
                "ok": False,
                "plan_id": plan_id,
                "error": "created execution graph violates workflow contract",
                "validation_errors": followup_errors,
                "workflow_result": result,
            })
        now = int(time.time())
        plan["status"] = "executed"
        plan["design_status"] = "approved"
        plan["design_approved_at"] = now
        plan["design_approved_by"] = approved_by
        if not plan.get("design_artifact_path"):
            plan["design_artifact_path"] = _explicit_design_artifact(plan)
        plan["executed_at"] = now
        plan["updated_at"] = now
        plan["execution_phase"] = "implementation"
        plan["phase2_executed_at"] = now
        plan["created_design_tasks"] = []
        plan["created_followup_tasks"] = result.get("created_tasks", [])
        plan["created_tasks"] = list(plan.get("created_followup_tasks") or [])
        contract_result = _write_workflow_contract(plan)
        impl_task_id = ""
        for item in plan.get("created_followup_tasks") or []:
            mode = _plan_mode_for_key(plan, str(item.get("key") or ""))
            if mode == "implementer":
                impl_task_id = str(item.get("task_id") or "")
                break
        scope_export = _export_approved_scope(plan, "implementer", impl_task_id, approved_by=approved_by, approved_at=now)
        plan["workflow_contract"] = {"path": contract_result.get("path"), "ok": contract_result.get("ok")}
        plan["approved_scope_export"] = {"path": scope_export.get("path"), "ok": scope_export.get("ok"), "phase": scope_export.get("phase")}
        workflow_result = plan.get("workflow_result") or {}
        if not isinstance(workflow_result, dict):
            workflow_result = {}
        workflow_result["implementation_phase"] = result
        plan["workflow_result"] = workflow_result
        _save_plans(data)
        result.update({
            "ok": True,
            "mode": "execute_plan",
            "phase": "implementation",
            "plan_id": plan_id,
            "plan_status": "executed",
            "approved_by": approved_by,
            "preflight_result": preflight_result,
            "repair_result": repair_result,
            "reverification_result": effective_preflight if repair_result is not None else None,
            "created_followup_tasks": result.get("created_tasks", []),
            "created_design_tasks": [],
            "required_user_report": "Report these real implementation/review/final task ids to the user and note this execution plan started from an already approved design artifact.",
        })
        return _json(result)

    design_tasks, followup_tasks = _split_design_tasks(plan.get("tasks") or [])
    if not design_tasks:
        return _error("plan must include at least one backend-architect architect task before execution", plan_id=plan_id)
    if plan_kind in {"design", "mapping"}:
        followup_tasks = []
    elif not followup_tasks:
        return _error("plan must include follow-up implementation/review/final tasks after design", plan_id=plan_id)

    assignees = sorted({str(t.get("assignee") or "").strip() for t in design_tasks if t.get("assignee")})
    preflight_result = _run_worker_preflight(assignees)
    plan["last_execution_stage"] = "design_preflight"
    plan["preflight_result"] = preflight_result

    repair_result = None
    if not preflight_result.get("ok"):
        repair_result = _attempt_preflight_repair(preflight_result)
        plan["last_execution_stage"] = "design_repair"
        plan["repair_result"] = repair_result

    effective_preflight = repair_result or preflight_result
    plan["reverification_result"] = effective_preflight if repair_result is not None else None

    if not effective_preflight.get("ok"):
        now = int(time.time())
        plan["status"] = "preflight_blocked"
        plan["updated_at"] = now
        plan["execution_phase"] = "design"
        plan["created_design_tasks"] = []
        _save_plans(data)
        return _json({
            "ok": False,
            "plan_id": plan_id,
            "error": "worker preflight failed after repair attempt",
            "status": "preflight_blocked",
            "phase": "design",
            "preflight_result": preflight_result,
            "repair_result": repair_result,
            "reverification_result": effective_preflight if repair_result is not None else None,
            "hint": "Retry with pm_execute_plan after resolving worker issues.",
        })

    plan["last_execution_stage"] = "create_design_cards"
    result_text = pm_create_kanban_workflow(
        request=plan.get("request") or "",
        tasks=design_tasks,
        tenant=plan.get("tenant"),
        project_path=plan.get("project_path"),
        created_by="project_manager",
        plan_id=plan_id,
    )
    try:
        result = json.loads(result_text)
    except Exception:
        return _error("workflow returned non-json result", raw=result_text)
    if not result.get("ok"):
        plan["status"] = "execution_failed"
        plan["execution_error"] = result
        plan["updated_at"] = int(time.time())
        _save_plans(data)
        return _json({"ok": False, "plan_id": plan_id, "error": "design execution failed", "workflow_result": result})

    now = int(time.time())
    if not plan.get("approved_at"):
        plan["approved_at"] = now
    if not plan.get("approved_by"):
        plan["approved_by"] = approved_by
    plan["status"] = "design_in_progress"
    plan["updated_at"] = now
    plan["execution_phase"] = "design"
    plan["design_status"] = "in_progress"
    plan["phase1_executed_at"] = now
    plan["created_design_tasks"] = result.get("created_tasks", [])
    plan["created_tasks"] = list(plan.get("created_design_tasks") or [])
    contract_result = _write_workflow_contract(plan)
    first_design_task_id = ""
    if plan.get("created_design_tasks"):
        first_design_task_id = str((plan.get("created_design_tasks") or [{}])[0].get("task_id") or "")
    scope_export = _export_approved_scope(plan, "architect", first_design_task_id, approved_by=approved_by, approved_at=now)
    plan["workflow_contract"] = {"path": contract_result.get("path"), "ok": contract_result.get("ok")}
    plan["approved_scope_export"] = {"path": scope_export.get("path"), "ok": scope_export.get("ok"), "phase": scope_export.get("phase")}
    plan["workflow_result"] = {"design_phase": result}
    _save_plans(data)
    result.update({
        "mode": "execute_design_phase",
        "phase": "design",
        "plan_id": plan_id,
        "plan_status": "design_in_progress",
        "approved_by": approved_by,
        "preflight_result": preflight_result,
        "repair_result": repair_result,
        "reverification_result": effective_preflight if repair_result is not None else None,
        "created_design_tasks": result.get("created_tasks", []),
        "required_user_report": "Report these architect task IDs to the user as the design phase. After design is complete, call pm_mark_design_ready and ask for design approval before any implementation starts.",
    })
    return _json(result)


def pm_mark_design_ready(
    plan_id: str,
    design_summary: str = "",
    design_artifact_path: str = "",
    updated_by: str = "project_manager",
) -> str:
    """Persist design evidence and mark the plan ready for user design approval."""
    plan_id = str(plan_id or "").strip()
    if not plan_id:
        return _error("plan_id is required")
    data = _load_plans()
    plan = data.get("plans", {}).get(plan_id)
    if not plan:
        return _error("plan_id not found", plan_id=plan_id)
    plan = _ensure_plan_workflow_fields(plan)
    if _plan_kind(plan) == "execution":
        return _error("execution plan does not have a local design phase; execute it directly or create a separate design plan", plan_id=plan_id, status=plan.get("status"))
    if plan.get("status") not in {"design_in_progress", "design_revision_requested", "awaiting_design_approval"}:
        return _error("plan is not in a design-ready state", plan_id=plan_id, status=plan.get("status"))
    now = int(time.time())
    plan["status"] = "awaiting_design_approval"
    plan["design_status"] = "awaiting_approval"
    plan["design_summary"] = str(design_summary or "").strip()
    if str(design_artifact_path or "").strip():
        plan["design_artifact_path"] = str(design_artifact_path).strip()
    plan["design_ready_at"] = now
    plan["design_requested_by"] = updated_by
    plan["updated_at"] = now
    _save_plans(data)
    return _json({
        "ok": True,
        "plan_id": plan_id,
        "status": "awaiting_design_approval",
        "design_status": "awaiting_approval",
        "design_summary": plan.get("design_summary") or "",
        "design_artifact_path": plan.get("design_artifact_path") or "",
        "required_user_report": "Show the design summary/artifact to the user and ask for explicit design approval before implementation starts.",
        "approval_phrase": f"승인. {plan_id} 설계 승인, 구현 진행해",
    })


def pm_request_design_revision(plan_id: str, revision_notes: str, requested_by: str = "user") -> str:
    """Persist a design revision request and send the workflow back to architect mode."""
    plan_id = str(plan_id or "").strip()
    revision_notes = str(revision_notes or "").strip()
    if not plan_id:
        return _error("plan_id is required")
    if not revision_notes:
        return _error("revision_notes is required")
    data = _load_plans()
    plan = data.get("plans", {}).get(plan_id)
    if not plan:
        return _error("plan_id not found", plan_id=plan_id)
    plan = _ensure_plan_workflow_fields(plan)
    if plan.get("status") not in {"design_in_progress", "awaiting_design_approval", "design_revision_requested"}:
        return _error("plan is not in a revisable design state", plan_id=plan_id, status=plan.get("status"))
    now = int(time.time())
    notes = plan.get("design_revision_notes") or []
    if not isinstance(notes, list):
        notes = []
    notes.append({"requested_by": requested_by, "notes": revision_notes, "at": now})
    recovery = _reopen_design_tasks_for_revision(plan, revision_notes)
    plan["design_revision_notes"] = notes
    plan["status"] = "design_revision_requested"
    plan["design_status"] = "revision_requested"
    plan["design_ready_at"] = None
    plan["design_requested_by"] = None
    plan["updated_at"] = now
    _save_plans(data)
    return _json({
        "ok": True,
        "plan_id": plan_id,
        "status": "design_revision_requested",
        "design_status": "revision_requested",
        "revision_notes": notes,
        "design_task_recovery": recovery,
        "required_user_report": "Tell the architect to revise the design. Existing architect tasks were annotated with the revision request and blocked design tasks were reopened when possible. Call pm_mark_design_ready only after updated design artifacts exist.",
    })


def pm_supersede_plan(plan_id: str, reason: str, superseded_by: str = "") -> str:
    """Mark a stale/contaminated plan as superseded so notifiers and operators can ignore it."""
    plan_id = str(plan_id or "").strip()
    reason = str(reason or "").strip()
    superseded_by = str(superseded_by or "").strip()
    if not plan_id:
        return _error("plan_id is required")
    if not reason:
        return _error("reason is required")
    data = _load_plans()
    plan = data.get("plans", {}).get(plan_id)
    save_profile: str | None = None
    if not plan:
        data = _load_plans("project_manager")
        plan = data.get("plans", {}).get(plan_id)
        save_profile = "project_manager"
    if not plan:
        return _error("plan_id not found", plan_id=plan_id)
    plan = _ensure_plan_workflow_fields(plan)
    plan["status"] = "superseded"
    plan["design_status"] = "superseded"
    plan["superseded_reason"] = reason
    if superseded_by:
        plan["superseded_by"] = superseded_by
    plan["updated_at"] = int(time.time())
    data.setdefault("plans", {})[plan_id] = plan
    _save_plans(data, save_profile)
    return _json({
        "ok": True,
        "plan_id": plan_id,
        "status": "superseded",
        "design_status": "superseded",
        "superseded_reason": reason,
        "superseded_by": superseded_by or None,
    })


def pm_approve_design_and_execute(
    plan_id: str,
    approved_by: str = "user",
    design_artifact_path: str = "",
) -> str:
    """Execute phase 2 of a plan after user design approval by creating implementation/follow-up Kanban cards."""
    plan_id = str(plan_id or "").strip()
    if not plan_id:
        return _error("plan_id is required")
    data = _load_plans()
    plan = data.get("plans", {}).get(plan_id)
    if not plan:
        return _error("plan_id not found", plan_id=plan_id)
    plan = _ensure_plan_workflow_fields(plan)
    plan_kind = _plan_kind(plan)
    if plan_kind == "execution":
        return _error("execution plan does not use design approval handoff; run pm_execute_plan after plan approval", plan_id=plan_id, status=plan.get("status"))
    if plan.get("status") == "executed":
        return _error("plan already fully executed", plan_id=plan_id, created_tasks=plan.get("created_tasks", []))
    if plan.get("status") == "rejected":
        return _error("plan was rejected and cannot be executed", plan_id=plan_id)
    if plan_kind in {"design", "mapping"}:
        if plan.get("status") not in {"awaiting_design_approval"}:
            return _error(f"{plan_kind}-only plan is not ready for approval", plan_id=plan_id, status=plan.get("status"))
        now = int(time.time())
        plan["status"] = "approved"
        plan["design_status"] = "approved"
        plan["design_approved_at"] = now
        plan["design_approved_by"] = approved_by
        if str(design_artifact_path or "").strip():
            plan["design_artifact_path"] = str(design_artifact_path).strip()
        contract_result = _write_workflow_contract(plan)
        plan["workflow_contract"] = {"path": contract_result.get("path"), "ok": contract_result.get("ok")}
        plan["updated_at"] = now
        plan["execution_phase"] = "design_approved"
        _save_plans(data)
        return _json({
            "ok": True,
            "mode": "approve_design_and_execute",
            "phase": "design_approved",
            "plan_id": plan_id,
            "plan_kind": plan_kind,
            "plan_status": "approved",
            "approved_by": approved_by,
            "execution_started": False,
            "created_design_tasks": plan.get("created_design_tasks", []),
            "created_followup_tasks": [],
            "required_user_report": "Report that the design/mapping plan is approved and create a separate execution plan that references this approved plan as source_plan_id.",
        })
    status = str(plan.get("status") or "")
    design_status = str(plan.get("design_status") or "")
    execution_phase = str(plan.get("execution_phase") or "")
    retryable_preflight_block = (
        status == "preflight_blocked"
        and execution_phase == "implementation"
        and design_status == "awaiting_approval"
    )
    if status != "awaiting_design_approval" and not retryable_preflight_block:
        return _error("plan is not ready for implementation phase", plan_id=plan_id, status=plan.get("status"))

    design_key_to_id = _plan_task_key_to_id(plan, "created_design_tasks")
    if not design_key_to_id:
        return _error("design phase has no created architect task ids", plan_id=plan_id)

    design_tasks, followup_tasks = _split_design_tasks(plan.get("tasks") or [])
    if not design_tasks:
        return _error("plan has no architect tasks to anchor design approval", plan_id=plan_id)
    if not followup_tasks:
        return _error("plan has no follow-up tasks to execute after design approval", plan_id=plan_id)

    resolved_followup_tasks, err = _resolve_followup_task_parents(followup_tasks, design_key_to_id)
    if err:
        return _error(err, plan_id=plan_id)

    followup_role_errors = _validate_followup_task_roles(plan, resolved_followup_tasks)
    if followup_role_errors:
        plan["status"] = "execution_failed"
        plan["execution_error"] = {
            "ok": False,
            "error": "resolved follow-up graph violates workflow contract",
            "validation_errors": followup_role_errors,
        }
        plan["updated_at"] = int(time.time())
        _save_plans(data)
        return _json({
            "ok": False,
            "plan_id": plan_id,
            "error": "resolved follow-up graph violates workflow contract",
            "validation_errors": followup_role_errors,
        })

    contract = _build_workflow_contract(plan)
    contract_errors = _validate_workflow_contract_invariants(plan, contract)
    if contract_errors:
        now = int(time.time())
        plan["status"] = "awaiting_design_approval"
        plan["design_status"] = "awaiting_approval"
        plan["last_execution_stage"] = "contract_validation"
        plan["execution_error"] = {
            "ok": False,
            "error": "workflow contract is not executable after design approval",
            "validation_errors": contract_errors,
        }
        plan["updated_at"] = now
        _save_plans(data)
        return _json({
            "ok": False,
            "plan_id": plan_id,
            "error": "workflow contract is not executable after design approval",
            "validation_errors": contract_errors,
            "hint": "Update contract.implementation_paths / expected_deliverables from the approved design before creating implementation cards.",
        })

    assignees = sorted({str(t.get("assignee") or "").strip() for t in resolved_followup_tasks if t.get("assignee")})
    preflight_result = _run_worker_preflight(assignees)
    plan["last_execution_stage"] = "implementation_preflight"
    plan["implementation_preflight_result"] = preflight_result

    repair_result = None
    if not preflight_result.get("ok"):
        repair_result = _attempt_preflight_repair(preflight_result)
        plan["last_execution_stage"] = "implementation_repair"
        plan["implementation_repair_result"] = repair_result

    effective_preflight = repair_result or preflight_result
    plan["implementation_reverification_result"] = effective_preflight if repair_result is not None else None

    if not effective_preflight.get("ok"):
        now = int(time.time())
        plan["status"] = "preflight_blocked"
        plan["updated_at"] = now
        plan["execution_phase"] = "implementation"
        plan["created_followup_tasks"] = []
        plan["created_tasks"] = []
        _save_plans(data)
        return _json({
            "ok": False,
            "plan_id": plan_id,
            "error": "worker preflight failed after repair attempt",
            "status": "preflight_blocked",
            "phase": "implementation",
            "preflight_result": preflight_result,
            "repair_result": repair_result,
            "reverification_result": effective_preflight if repair_result is not None else None,
            "hint": "Retry with pm_approve_design_and_execute after resolving worker issues.",
        })

    plan["last_execution_stage"] = "create_followup_cards"
    result_text = pm_create_kanban_workflow(
        request=plan.get("request") or "",
        tasks=resolved_followup_tasks,
        tenant=plan.get("tenant"),
        project_path=plan.get("project_path"),
        created_by="project_manager",
        plan_id=plan_id,
    )
    try:
        result = json.loads(result_text)
    except Exception:
        return _error("workflow returned non-json result", raw=result_text)
    if not result.get("ok"):
        plan["status"] = "execution_failed"
        plan["execution_error"] = result
        plan["updated_at"] = int(time.time())
        _save_plans(data)
        return _json({"ok": False, "plan_id": plan_id, "error": "follow-up execution failed", "workflow_result": result})

    followup_errors = _validate_followup_graph(plan, resolved_followup_tasks, result.get("created_tasks", []))
    if followup_errors:
        plan["status"] = "execution_failed"
        plan["execution_error"] = {"ok": False, "error": "created follow-up graph violates workflow contract", "validation_errors": followup_errors, "workflow_result": result}
        plan["updated_at"] = int(time.time())
        _save_plans(data)
        return _json({
            "ok": False,
            "plan_id": plan_id,
            "error": "created follow-up graph violates workflow contract",
            "validation_errors": followup_errors,
            "workflow_result": result,
        })

    now = int(time.time())
    design_task_ids = {tid for tid in design_key_to_id.values() if str(tid).strip()}
    gate_completion = _complete_design_approval_gate_tasks(
        resolved_followup_tasks,
        result.get("created_tasks", []),
        design_task_ids,
        approved_by,
    )
    if not gate_completion.get("ok"):
        plan["status"] = "execution_failed"
        plan["execution_error"] = {
            "ok": False,
            "error": "failed to reconcile design-approval gate tasks after approval",
            "gate_completion": gate_completion,
            "workflow_result": result,
        }
        plan["updated_at"] = now
        _save_plans(data)
        return _json({
            "ok": False,
            "plan_id": plan_id,
            "error": "failed to reconcile design-approval gate tasks after approval",
            "gate_completion": gate_completion,
            "workflow_result": result,
        })
    plan["status"] = "executed"
    plan["design_status"] = "approved"
    plan["design_approved_at"] = now
    plan["design_approved_by"] = approved_by
    if str(design_artifact_path or "").strip():
        plan["design_artifact_path"] = str(design_artifact_path).strip()
    plan["executed_at"] = now
    plan["updated_at"] = now
    plan["execution_phase"] = "implementation"
    plan["phase2_executed_at"] = now
    plan["created_followup_tasks"] = result.get("created_tasks", [])
    gate_updates_by_id = {
        str(item.get("task_id") or ""): item
        for item in (gate_completion.get("updates") or [])
        if isinstance(item, dict) and str(item.get("task_id") or "")
    }
    for created in plan["created_followup_tasks"]:
        if not isinstance(created, dict):
            continue
        update = gate_updates_by_id.get(str(created.get("task_id") or ""))
        if update and str(update.get("current_status") or "").strip():
            created["status"] = str(update.get("current_status") or "").strip()
    plan["created_tasks"] = list(plan.get("created_design_tasks") or []) + list(plan.get("created_followup_tasks") or [])
    contract_result = _write_workflow_contract(plan)
    impl_task_id = ""
    for item in plan.get("created_followup_tasks") or []:
        key = str(item.get("key") or "").lower()
        title = str(item.get("title") or "").lower()
        if "impl" in key or "implement" in title or "create" in title:
            impl_task_id = str(item.get("task_id") or "")
            break
    scope_export = _export_approved_scope(plan, "implementer", impl_task_id, approved_by=approved_by, approved_at=now)
    plan["workflow_contract"] = {"path": contract_result.get("path"), "ok": contract_result.get("ok")}
    plan["approved_scope_export"] = {"path": scope_export.get("path"), "ok": scope_export.get("ok"), "phase": scope_export.get("phase")}
    workflow_result = plan.get("workflow_result") or {}
    if not isinstance(workflow_result, dict):
        workflow_result = {}
    workflow_result["implementation_phase"] = result
    plan["workflow_result"] = workflow_result
    _save_plans(data)
    result.update({
        "mode": "approve_design_and_execute",
        "phase": "implementation",
        "plan_id": plan_id,
        "plan_status": "executed",
        "approved_by": approved_by,
        "preflight_result": preflight_result,
        "repair_result": repair_result,
        "reverification_result": effective_preflight if repair_result is not None else None,
        "created_followup_tasks": result.get("created_tasks", []),
        "created_design_tasks": plan.get("created_design_tasks", []),
        "design_gate_completion": gate_completion,
        "required_user_report": "Report these real implementation/review/final task ids to the user and mention that implementation started only after design approval.",
    })
    return _json(result)


def _create_plan_handler(args: dict[str, Any], **kwargs: Any) -> str:
    return pm_create_plan(
        request=str(args.get("request") or ""),
        summary=str(args.get("summary") or ""),
        tasks=args.get("tasks") or [],
        acceptance_criteria=args.get("acceptance_criteria") or [],
        validation_plan=args.get("validation_plan") or [],
        risks=args.get("risks") or [],
        contract=args.get("contract") or {},
        tenant=args.get("tenant"),
        project_path=args.get("project_path"),
        created_by=str(args.get("created_by") or "project_manager"),
        plan_kind=str(args.get("plan_kind") or "staged"),
    )


def _execute_plan_handler(args: dict[str, Any], **kwargs: Any) -> str:
    return pm_execute_plan(
        plan_id=str(args.get("plan_id") or ""),
        approved_by=str(args.get("approved_by") or "user"),
    )


def _mark_design_ready_handler(args: dict[str, Any], **kwargs: Any) -> str:
    return pm_mark_design_ready(
        plan_id=str(args.get("plan_id") or ""),
        design_summary=str(args.get("design_summary") or ""),
        design_artifact_path=str(args.get("design_artifact_path") or ""),
        updated_by=str(args.get("updated_by") or "project_manager"),
    )


def _request_design_revision_handler(args: dict[str, Any], **kwargs: Any) -> str:
    return pm_request_design_revision(
        plan_id=str(args.get("plan_id") or ""),
        revision_notes=str(args.get("revision_notes") or ""),
        requested_by=str(args.get("requested_by") or "user"),
    )


def _supersede_plan_handler(args: dict[str, Any], **kwargs: Any) -> str:
    return pm_supersede_plan(
        plan_id=str(args.get("plan_id") or ""),
        reason=str(args.get("reason") or ""),
        superseded_by=str(args.get("superseded_by") or ""),
    )


def _approve_design_and_execute_handler(args: dict[str, Any], **kwargs: Any) -> str:
    return pm_approve_design_and_execute(
        plan_id=str(args.get("plan_id") or ""),
        approved_by=str(args.get("approved_by") or "user"),
        design_artifact_path=str(args.get("design_artifact_path") or ""),
    )


def _reject_plan_handler(args: dict[str, Any], **kwargs: Any) -> str:
    return pm_reject_plan(
        plan_id=str(args.get("plan_id") or ""),
        rejected_by=str(args.get("rejected_by") or "user"),
        reason=str(args.get("reason") or ""),
    )


def _status_handler(args: dict[str, Any], **kwargs: Any) -> str:
    return pm_get_plan_status(plan_id=args.get("plan_id"))


def _internal_create_handler(args: dict[str, Any], **kwargs: Any) -> str:
    return pm_create_kanban_workflow(
        request=str(args.get("request") or ""),
        tasks=args.get("tasks") or [],
        tenant=args.get("tenant"),
        project_path=args.get("project_path"),
        created_by=str(args.get("created_by") or "project_manager"),
    )


_TASK_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "Stable local key such as T1, T2, summary."},
        "title": {"type": "string"},
        "body": {"type": "string"},
        "assignee": {"type": "string", "enum": ["backend-architect", "backend-implementer", "backend-reviewer", "backend-specialist", "project_manager"]},
        "mode": {"type": "string", "enum": ["architect", "implementer", "debugger", "reviewer"], "description": "Required for backend worker tasks; omit for project_manager tasks. Dedicated worker mapping: backend-architect=architect, backend-implementer=implementer/debugger, backend-reviewer=reviewer. Legacy backend-specialist is multi-mode and may use architect/implementer/debugger/reviewer when explicitly requested."},
        "parents": {"type": "array", "items": {"type": "string"}},
        "priority": {"type": "integer"},
    },
    "required": ["key", "title", "body", "assignee"],
}

registry.register(
    name="pm_create_plan",
    toolset=_PUBLIC_TOOLSET,
    schema={
        "name": "pm_create_plan",
        "description": "Create and store a PM brief/plan awaiting explicit user plan approval without creating Kanban cards. Use for new feature/multi-step requests before architect work starts.",
        "parameters": {
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "Original user request."},
                "summary": {"type": "string", "description": "Short plan summary to show the user."},
                "project_path": {"type": "string", "description": "Optional fallback only; profile terminal.cwd is authoritative."},
                "tenant": {"type": "string"},
                "created_by": {"type": "string"},
                "plan_kind": {"type": "string", "enum": ["staged", "design", "mapping", "execution"], "description": "staged=legacy two-stage plan, design=architect-only design plan, mapping=approved-design to concrete-scope mapping plan, execution=approved-scope execution plan."},
                "tasks": {"type": "array", "items": _TASK_ITEM_SCHEMA},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "validation_plan": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
                "contract": {
                    "type": "object",
                    "description": "Optional explicit workflow contract fields. Prefer this over path inference for productized staged workflows.",
                    "properties": {
                        "expected_deliverables": {"type": "array", "items": {"type": "string"}},
                        "implementation_paths": {"type": "array", "items": {"type": "string"}},
                        "design_artifact": {"type": "string"},
                        "source_plan_id": {"type": "string"},
                        "required_tasks_by_phase": {"type": "object"},
                        "phase_allowed_paths": {"type": "object"},
                        "required_evidence_by_phase": {"type": "object"},
                        "done_when": {"type": "array", "items": {"type": "string"}},
                        "forbidden_paths": {"type": "array", "items": {"type": "string"}},
                        "risk_level": {"type": "string"}
                    }
                },
            },
            "required": ["request", "summary", "tasks"],
        },
    },
    handler=_create_plan_handler,
    description="Create a PM plan awaiting explicit plan approval",
    emoji="📝",
)

registry.register(
    name="pm_execute_plan",
    toolset=_PUBLIC_TOOLSET,
    schema={
        "name": "pm_execute_plan",
        "description": "Execute phase 1 of a PM-plan-approved workflow by creating architect/design Kanban cards only. Do not use this to start implementation directly.",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "Plan ID returned by pm_create_plan, e.g. plan_ab12cd34."},
                "approved_by": {"type": "string", "description": "Who approved plan execution, default user."},
            },
            "required": ["plan_id"],
        },
    },
    handler=_execute_plan_handler,
    description="Execute the design phase of an approved PM plan",
    emoji="🚦",
)

registry.register(
    name="pm_mark_design_ready",
    toolset=_PUBLIC_TOOLSET,
    schema={
        "name": "pm_mark_design_ready",
        "description": "Mark a plan's design phase as ready for user approval after architect output exists.",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "Plan ID awaiting design approval."},
                "design_summary": {"type": "string", "description": "User-facing design summary."},
                "design_artifact_path": {"type": "string", "description": "Optional path to the design artifact file."},
                "updated_by": {"type": "string", "description": "Who is marking the design ready; default project_manager."},
            },
            "required": ["plan_id"],
        },
    },
    handler=_mark_design_ready_handler,
    description="Mark design ready for approval",
    emoji="🧭",
)

registry.register(
    name="pm_request_design_revision",
    toolset=_PUBLIC_TOOLSET,
    schema={
        "name": "pm_request_design_revision",
        "description": "Persist a user-requested design revision and send the workflow back to architect mode.",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "Plan ID whose design needs revision."},
                "revision_notes": {"type": "string", "description": "Concrete user-requested design changes."},
                "requested_by": {"type": "string", "description": "Who requested the revision, default user."},
            },
            "required": ["plan_id", "revision_notes"],
        },
    },
    handler=_request_design_revision_handler,
    description="Request design revision",
    emoji="✏️",
)

registry.register(
    name="pm_supersede_plan",
    toolset=_PUBLIC_TOOLSET,
    schema={
        "name": "pm_supersede_plan",
        "description": "Mark a stale/contaminated PM plan as superseded so notifiers and operators ignore it.",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "Plan ID to supersede."},
                "reason": {"type": "string", "description": "Why the plan is being superseded/abandoned."},
                "superseded_by": {"type": "string", "description": "Optional replacement plan id."}
            },
            "required": ["plan_id", "reason"],
        },
    },
    handler=_supersede_plan_handler,
    description="Supersede stale PM plan",
    emoji="🗃️",
)

registry.register(
    name="pm_approve_design_and_execute",
    toolset=_PUBLIC_TOOLSET,
    schema={
        "name": "pm_approve_design_and_execute",
        "description": "After the user approves the design, create implementation/review/final Kanban cards from the approved plan.",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "Plan ID whose design has been approved."},
                "approved_by": {"type": "string", "description": "Who approved the design, default user."},
                "design_artifact_path": {"type": "string", "description": "Optional final design artifact path to persist on the plan."},
            },
            "required": ["plan_id"],
        },
    },
    handler=_approve_design_and_execute_handler,
    description="Approve design and start implementation phase",
    emoji="✅",
)

registry.register(
    name="pm_reject_plan",
    toolset=_PUBLIC_TOOLSET,
    schema={
        "name": "pm_reject_plan",
        "description": "Reject/deny a stored awaiting-approval plan. Use when the user refuses a plan (거부, 승인 거부, 실행하지 마, reject, deny).",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "Plan ID to reject, e.g. plan_ab12cd34."},
                "rejected_by": {"type": "string", "description": "Who rejected, default user."},
                "reason": {"type": "string", "description": "Optional reason for rejection."},
            },
            "required": ["plan_id"],
        },
    },
    handler=_reject_plan_handler,
    description="Reject a PM plan",
    emoji="🚫",
)

registry.register(
    name="pm_get_plan_status",
    toolset=_PUBLIC_TOOLSET,
    schema={
        "name": "pm_get_plan_status",
        "description": "Get one PM plan by plan_id or list recent plans.",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "Optional plan ID."},
            },
        },
    },
    handler=_status_handler,
    description="Get PM plan status",
    emoji="📌",
)

# Internal-only registration. This keeps compatibility for local verification but
# removes the raw creator from the public pm_workflow toolset exposed to Slack PM.
registry.register(
    name="pm_create_kanban_workflow",
    toolset=_INTERNAL_TOOLSET,
    schema={
        "name": "pm_create_kanban_workflow",
        "description": "Internal raw Kanban workflow creator. Do not expose to Slack PM; use pm_execute_plan instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "project_path": {"type": "string"},
                "tenant": {"type": "string"},
                "created_by": {"type": "string"},
                "tasks": {"type": "array", "items": _TASK_ITEM_SCHEMA},
            },
            "required": ["request", "tasks"],
        },
    },
    handler=_internal_create_handler,
    description="Internal verified Kanban workflow creator",
    emoji="📋",
)
