#!/usr/bin/env python3
"""Notify when PM final-summary Kanban tasks complete.

Designed for Hermes cron with no_agent=True:
- Prints a Slack-ready message only when there is a new completed PM final task.
- Prints nothing when there is nothing new, so cron stays silent.
- Records reported task ids to avoid duplicate notifications.
"""
from __future__ import annotations

import os
import argparse
import hashlib
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HERMES_HOME = Path(os.environ.get('HERMES_ROOT', str(Path.home() / '.hermes'))).expanduser()
PM_PLANS_PATH = HERMES_HOME / 'profiles' / 'project_manager' / 'state' / 'pm_plans.json'
CURRENT_BOARD = HERMES_HOME / 'kanban' / 'current'
STATE_PATH = HERMES_HOME / 'state' / 'pm_kanban_completion_notifier.json'
DEFAULT_PROJECT_PATH = '/mnt/c/Users/cross/OneDrive/Desktop/HermesTest'


def _now() -> int:
    return int(time.time())


def _read_board() -> str:
    try:
        val = CURRENT_BOARD.read_text(encoding='utf-8').strip()
        return val or 'default'
    except FileNotFoundError:
        return 'default'


def _db_path(board: str) -> Path:
    if board == 'default':
        return HERMES_HOME / 'kanban.db'
    return HERMES_HOME / 'kanban' / 'boards' / board / 'kanban.db'


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {'initialized_at': _now(), 'reported': [], 'reported_events': {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('state is not dict')
        data.setdefault('initialized_at', _now())
        data.setdefault('reported', [])
        if not isinstance(data.get('reported_events'), dict):
            data['reported_events'] = {}
        return data
    except Exception:
        return {'initialized_at': _now(), 'reported': [], 'reported_events': {}}


def _event_digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(STATE_PATH)


def _connect(board: str) -> sqlite3.Connection:
    path = _db_path(board)
    if not path.exists():
        raise SystemExit(f'Kanban DB not found: {path}')
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _summary(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute(
        'select summary from task_runs where task_id=? and outcome=? order by ended_at desc, id desc limit 1',
        (task_id, 'completed'),
    ).fetchone()
    if row and row['summary']:
        return str(row['summary']).strip()
    task = conn.execute('select result from tasks where id=?', (task_id,)).fetchone()
    if task and task['result']:
        return str(task['result']).strip()
    return '(요약 없음)'


def _parents(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        '''
        select t.*
        from task_links l
        join tasks t on t.id = l.parent_id
        where l.child_id = ?
        order by t.created_at asc
        ''',
        (task_id,),
    ).fetchall()


def _load_pm_plans() -> dict[str, Any]:
    try:
        data = json.loads(PM_PLANS_PATH.read_text(encoding='utf-8'))
        plans = data.get('plans') if isinstance(data, dict) else {}
        return plans if isinstance(plans, dict) else {}
    except Exception:
        return {}


def _final_task_keys(plan: dict[str, Any], expected: list[Any]) -> set[str]:
    tasks = [t for t in (plan.get('tasks') or []) if isinstance(t, dict)]
    reviewer_keys = {str(t.get('key') or '') for t in tasks if t.get('mode') == 'reviewer'}
    final_keys: set[str] = set()
    for task in tasks:
        if str(task.get('assignee') or '') != 'project_manager':
            continue
        key = str(task.get('key') or '')
        title = str(task.get('title') or '').lower()
        parents = {str(p) for p in (task.get('parents') or [])}
        if reviewer_keys and parents & reviewer_keys:
            final_keys.add(key)
            continue
        if key.lower() == 'final' or 'final' in title or 'synthesis' in title:
            # Do not classify architect/downstream design-approval gates as final.
            if not reviewer_keys or parents & reviewer_keys:
                final_keys.add(key)
    return final_keys


def _find_plan_for_final(task_id: str) -> tuple[str | None, dict[str, Any] | None]:
    for plan_id, plan in _load_pm_plans().items():
        if not isinstance(plan, dict) or str(plan.get('status') or '') in {'superseded', 'archived'}:
            continue
        contract = _extract_contract(plan)
        expected = contract.get('expected_deliverables') if isinstance(contract.get('expected_deliverables'), list) else []
        final_keys = _final_task_keys(plan, expected)
        task_rows = (plan.get('created_followup_tasks') or []) + (plan.get('created_tasks') or [])
        for row in task_rows:
            if isinstance(row, dict) and row.get('task_id') == task_id and str(row.get('key') or '') in final_keys:
                return plan_id, plan
    return None, None


def _extract_contract(plan: dict[str, Any] | None) -> dict[str, Any]:
    if not plan:
        return {}
    contract_meta = plan.get('workflow_contract') if isinstance(plan.get('workflow_contract'), dict) else {}
    path = contract_meta.get('path')
    if path:
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _mode_for_row(plan: dict[str, Any] | None, row: dict[str, Any]) -> str:
    if not plan:
        return ''
    key = str(row.get('key') or '')
    for task in plan.get('tasks') or []:
        if isinstance(task, dict) and str(task.get('key') or '') == key:
            return str(task.get('mode') or '')
    title = str(row.get('title') or '').lower()
    if '구현' in title or '작성' in title or 'implementation' in title or 'implement' in title:
        return 'implementer'
    if '검토' in title or '검증' in title or '리뷰' in title or 'review' in title or 'verify' in title:
        return 'reviewer'
    return ''


def _task_rows_for_plan(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not plan:
        return []
    rows: list[dict[str, Any]] = []
    for field in ('created_design_tasks', 'created_followup_tasks', 'created_tasks'):
        for row in plan.get(field) or []:
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _parent_is_reviewerish(parent: sqlite3.Row, plan: dict[str, Any] | None) -> bool:
    parent_id = str(parent['id'])
    for row in _task_rows_for_plan(plan):
        if str(row.get('task_id') or '') == parent_id and _mode_for_row(plan, row) == 'reviewer':
            return True
    title = str(parent['title'] or '').lower()
    return any(token in title for token in ('review', 'verify', 'verification', '검토', '검증', '리뷰'))


def _validate_completion(conn: sqlite3.Connection, task: sqlite3.Row, parents: list[sqlite3.Row]) -> dict[str, Any]:
    summary = _summary(conn, task['id'])
    issues: list[str] = []
    plan_id, plan = _find_plan_for_final(task['id'])
    if not plan:
        return {'ok': False, 'skip': True, 'issues': ['not a PM final/synthesis task'], 'plan_id': plan_id, 'expected_deliverables': [], 'summary': summary}
    if not summary or summary == '(요약 없음)':
        issues.append('PM final summary is empty')
    if not parents:
        issues.append('final task has no parent tasks')
    if parents and not any(p['assignee'] != 'project_manager' for p in parents):
        issues.append('final task has no non-PM parent')
    if parents and not any(_parent_is_reviewerish(p, plan) for p in parents):
        issues.append('final task does not directly depend on a reviewer/verification task')
    contract = _extract_contract(plan)
    expected = contract.get('expected_deliverables') if isinstance(contract.get('expected_deliverables'), list) else []
    project_path = (contract.get('project_path') if contract else None) or (plan or {}).get('project_path') or DEFAULT_PROJECT_PATH
    for rel in expected:
        if not (Path(project_path) / str(rel)).exists():
            issues.append(f'expected deliverable missing: {rel}')
    followups = (plan or {}).get('created_followup_tasks') or []
    if expected:
        # Dedicated worker pools may use backend-implementer/backend-reviewer instead
        # of the legacy single backend-specialist assignee. The PM plan task mode is
        # the source of truth here; assignee names are deployment-specific.
        if not any(isinstance(r, dict) and (_mode_for_row(plan, r) == 'implementer') for r in followups):
            issues.append('PM plan has expected deliverables but no implementer follow-up task')
        if not any(isinstance(r, dict) and (_mode_for_row(plan, r) == 'reviewer') for r in followups):
            issues.append('PM plan has expected deliverables but no reviewer follow-up task')
    return {'ok': not issues, 'issues': issues, 'plan_id': plan_id, 'expected_deliverables': expected, 'summary': summary}


def _candidate_final_tasks(conn: sqlite3.Connection, since: int) -> list[sqlite3.Row]:
    return conn.execute(
        '''
        select t.*
        from tasks t
        where t.status = 'done'
          and t.assignee = 'project_manager'
          and t.completed_at is not null
          and t.completed_at >= ?
          and exists (select 1 from task_links l where l.child_id = t.id)
        order by t.completed_at asc
        ''',
        (since,),
    ).fetchall()


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return '-'
    return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M:%S')


def _completion_event_key(task: sqlite3.Row, msg: str) -> str:
    completed_at = str(task['completed_at'] or '')
    return f"completion:{task['id']}:{completed_at}:{_event_digest(msg)}"


def _format_message(board: str, task: sqlite3.Row, parents: list[sqlite3.Row], conn: sqlite3.Connection) -> str:
    workspace = f"{task['workspace_kind']} @ {task['workspace_path'] or ''}".strip()
    validation = _validate_completion(conn, task, parents)
    if validation.get('skip'):
        return ''
    if not validation.get('ok'):
        lines = [
            'PM 보고 보류: 완료 증빙을 다시 확인해야 합니다.',
            '',
            f"- 보드: {board}",
            f"- 최종 task: {task['id']} — {task['title']}",
            f"- 감지 시각: {_fmt_ts(task['completed_at'])}",
            f"- 작업 경로: {workspace}",
            f"- plan_id: {validation.get('plan_id') or '-'}",
            '',
            '보류 이유',
        ]
        lines.extend(f"- {issue}" for issue in validation.get('issues') or [])
        lines.extend([
            '',
            '다음 조치',
            '- PM이 산출물/검증/최종 요약을 보완한 뒤 다시 완료 보고하도록 정정해 주세요.',
        ])
        return '\n'.join(lines).strip()
    lines = [
        'PM 최종 보고: 작업이 완료되었습니다.',
        '',
        f"- 보드: {board}",
        f"- 최종 task: {task['id']} — {task['title']}",
        f"- 완료 시각: {_fmt_ts(task['completed_at'])}",
        f"- 작업 경로: {workspace}",
        '',
        '최종 요약',
        _summary(conn, task['id']),
        '',
        '완료된 선행 작업',
    ]
    for p in parents:
        lines.extend([
            f"- {p['id']} [{p['assignee']}] {p['title']}",
            f"  상태: {p['status']}",
            f"  요약: {_summary(conn, p['id'])}",
        ])
    lines.extend([
        '',
        '안내',
        f"- 기대 작업 경로: {DEFAULT_PROJECT_PATH}",
        '- 수정이나 추가 요청이 있으면 이 채널에서 @PM으로 답변해 주세요.',
    ])
    return '\n'.join(lines).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='Print latest candidate without marking reported')
    ap.add_argument('--include-existing', action='store_true', help='Include completed tasks before initialized_at')
    args = ap.parse_args()

    board = _read_board()
    state = _load_state()
    reported = set(state.get('reported') or [])
    reported_events = state.get('reported_events') if isinstance(state.get('reported_events'), dict) else {}
    since = 0 if args.include_existing or args.dry_run else int(state.get('initialized_at') or _now())

    conn = _connect(board)
    candidates = _candidate_final_tasks(conn, since)
    pending = [t for t in candidates if t['id'] not in reported]

    if not pending:
        current_state = {}
        try:
            current_state = json.loads(STATE_PATH.read_text(encoding='utf-8')) if STATE_PATH.exists() else {}
        except Exception:
            current_state = {}
        if not STATE_PATH.exists() and not args.dry_run or (not args.dry_run and not isinstance(current_state.get('reported_events'), dict)):
            _save_state(state)
        return 0

    # Send oldest eligible final task first; skip PM gate tasks that are not real finals.
    task = None
    msg = ''
    skipped: list[str] = []
    for candidate in pending:
        candidate_msg = _format_message(board, candidate, _parents(conn, candidate['id']), conn)
        if candidate_msg:
            event_key = _completion_event_key(candidate, candidate_msg)
            if event_key in reported_events:
                skipped.append(candidate['id'])
                continue
            task = candidate
            msg = candidate_msg
            break
        skipped.append(candidate['id'])
    if not msg or task is None:
        if not args.dry_run:
            reported.update(skipped)
            state['reported'] = sorted(reported)
            _save_state(state)
        return 0
    if not args.dry_run:
        event_key = _completion_event_key(task, msg)
        reported.update(skipped)
        reported.add(task['id'])
        reported_events[event_key] = {
            'task_id': task['id'],
            'completed_at': task['completed_at'],
            'reported_at': _now(),
        }
        state['reported'] = sorted(reported)
        state['reported_events'] = reported_events
        state['last_reported_at'] = _now()
        _save_state(state)
    print(msg)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
