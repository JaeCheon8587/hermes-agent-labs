# PM Workflow Product Hardening Plan

> For Hermes: implement in small verified slices. Treat the current cron scripts as temporary scaffolding to be absorbed into core PM workflow logic.

Goal: turn the now-validated staged PM workflow into a durable product feature without ad-hoc cron glue or contaminated-board confusion.

Architecture: keep PM plan state + Kanban + workflow contract as the source of truth. Treat `.soul/approved_scope.json` as a generated phase snapshot, not independently managed state. Move design-ready, phase-scope-sync, and completion validation from cron scripts into core lifecycle transitions.

Tech stack: `tools/pm_workflow_tool.py`, Kanban DB/task lifecycle, notifier scripts, PM/backend-specialist profiles, pytest.

---

## Scope validated already

The v7 live run proved these behaviors work end-to-end:
- plan-only creation
- contract generation
- architect-only phase after plan approval
- automatic design-ready notification
- design approval → implementer/reviewer/final follow-up creation
- reviewer/final scope synchronization
- final report generation
- normal completion notification

This plan is for product hardening, not concept discovery.

---

## Workstream A — absorb cron glue into core workflow

### Objective
Eliminate behavior that currently depends on polling scripts:
- `pm_design_ready_notifier.py`
- `pm_scope_phase_sync.py`
- parts of `pm_kanban_completion_notifier.py`

### Deliverables
- direct core transition for architect done → `awaiting_design_approval`
- direct core transition for implementer done → reviewer scope export
- direct core transition for reviewer done → final scope export
- direct final validation before completion reporting

### Files
- Modify: `tools/pm_workflow_tool.py`
- Inspect/integrate with: Kanban lifecycle hooks / worker completion path
- Later shrink/remove: `~/ .hermes/scripts/pm_design_ready_notifier.py`
- Later shrink/remove: `~/ .hermes/scripts/pm_scope_phase_sync.py`

### Tasks
1. Identify the canonical completion hook that can observe `architect`, `implementer`, `reviewer`, and `project_manager final` task completion.
2. Add a resolver from Kanban `task_id` → PM `plan_id` + plan task mode.
3. On architect completion, call the equivalent of `pm_mark_design_ready` inside core workflow logic.
4. On implementer completion, export reviewer-phase approved scope immediately.
5. On reviewer completion, export final-phase approved scope immediately.
6. On PM final completion, run completion validation before allowing success reporting.
7. Downgrade cron scripts to safety nets or remove them after core path passes tests.

### Success criteria
- new live run works with cron jobs disabled
- no manual unblock/scope rewrite needed
- `approved_scope.json` always matches current actionable phase

---

## Workstream B — make workflow contract explicit, not regex-derived

### Objective
Replace best-effort deliverable extraction with explicit plan schema fields.

### Problem
Current contract generation still infers some structure from natural-language request/task text. That is fragile.

### Deliverables
Plan schema explicitly stores:
- `expected_deliverables`
- `phase_allowed_paths`
- `required_evidence_by_phase`
- `required_tasks_by_phase`
- `risk_level`
- optional `notification_style`

### Files
- Modify: `tools/pm_workflow_tool.py`
- Update docs/reference: staged PM workflow documentation/skill references

### Tasks
1. Define the minimal explicit contract schema in Python.
2. Require PM-created plans to either provide explicit deliverables or fail validation for staged artifact-producing work.
3. Keep regex/path extraction only as transitional fallback with warnings.
4. Persist schema version in contract.
5. Add validation errors that explain exactly what is missing.

### Success criteria
- staged doc/code plans do not rely on path regex to infer deliverables
- invalid/missing deliverable plans fail early and loudly

---

## Workstream C — unify stale/contaminated plan handling

### Objective
Stop old blocked/superseded runs from polluting UI, notifiers, and operator judgment.

### Deliverables
- formal stale states: `superseded`, `archived`, optional `ignored_by_notifier`
- one cleanup policy for abandoned live-test plans/tasks
- notifiers skip obsolete plans deterministically

### Files
- Modify: `tools/pm_workflow_tool.py`
- Modify: notifier logic
- Possibly add helper for PM cleanup/admin actions

### Tasks
1. Define plan-level stale states and meanings.
2. Make notifiers filter out stale states by default.
3. Add a PM/admin helper to supersede/archive a plan and annotate reason.
4. Decide whether child Kanban tasks should also be archived or just ignored by reporting.
5. Document operator cleanup flow after failed experiments.

### Success criteria
- old v2/v3/v4 contamination does not reappear in user-facing notifications
- no notifier scans stale plans as active candidates

---

## Workstream D — improve PM-facing notification UX

### Objective
Make messages feel like PM workflow output, not raw cron plumbing.

### Problems seen
- `Cronjob Response:` framing leaks internals
- approval prompts feel mechanical
- notifier names are more visible than PM intent

### Deliverables
- message templates for:
  - plan proposal
  - design approval request
  - scope mismatch warning
  - final completion report
- optional PM-branded prefix instead of cron branding

### Files
- notifier/reporting scripts or core messaging integration path
- PM profile messaging conventions if needed

### Tasks
1. Define target message copy for each stage.
2. Minimize raw scheduler framing where platform allows.
3. Ensure approvals always include one canonical reply phrase.
4. Ensure warnings are actionable and name the exact plan/task/phase.

### Success criteria
- user can follow workflow without understanding cron internals
- every notification clearly states whether action is required

---

## Workstream E — add regression tests

### Objective
Lock in behavior so future edits do not reopen the bugs we just found.

### Minimum test matrix
1. plan-only create does not create Kanban cards
2. invalid graph rejects final-only follow-up
3. architect completion transitions plan to `awaiting_design_approval`
4. design approval creates implementer/reviewer/final tasks
5. implementer completion exports reviewer scope
6. reviewer completion exports final scope
7. reviewer scope includes verification artifact path
8. final validation rejects missing deliverable/evidence/summary
9. completion notifier accepts a good finalized staged run
10. stale/superseded plans are ignored by notifiers
11. localized reviewer titles (`검토`, `검증`) count as reviewer ancestry
12. explicit contract fields override fallback extraction

### Files
- Add/modify pytest files under `tests/`
- Add fixtures for PM plans, Kanban DB rows, and contract snapshots

### Success criteria
- all historical bug classes from v2–v7 are covered by tests
- a future edit cannot silently regress notifier or phase sync behavior

---

## Recommended implementation order

1. Workstream A — absorb cron glue into core workflow
2. Workstream E — add tests around those core transitions immediately
3. Workstream C — stale/contaminated plan handling
4. Workstream B — explicit contract schema hardening
5. Workstream D — PM-facing notification UX polish

Reasoning:
- A is the biggest operational risk reducer.
- E prevents reintroducing the bugs while refactoring A.
- C removes board/report pollution that confuses operators.
- B hardens plan creation semantics after the transition model is stable.
- D is valuable, but mostly polish once the mechanics are trustworthy.

---

## Immediate next slice I recommend

Implement just this first vertical slice:
- move architect done → `pm_mark_design_ready`
- move implementer done → reviewer scope export
- move reviewer done → final scope export
- add tests for those three transitions

That gets us from "validated with cron assistance" to "core workflow owns the critical phase transitions."

---

## Definition of done for product hardening

We can call this productized when all are true:
- a fresh vN run succeeds with cron helpers disabled
- no manual state edits/unblocks are needed
- no stale-plan noise reaches the user
- contract fields are explicit for staged artifact-producing work
- regression tests cover the v2–v7 bug classes
