#!/usr/bin/env python3
"""Notify when PM workflow tasks transition into blocked state.

Designed for Hermes cron with no_agent=True:
- Prints a Slack-ready message only when there is a new blocked event for a PM workflow task.
- Prints nothing when there is nothing new, so cron stays silent.
- Tracks the last seen blocked task_event id to avoid duplicate notifications.
"""
from __future__ import annotations

import os
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HERMES_HOME = Path(os.environ.get('HERMES_ROOT', str(Path.home() / '.hermes'))).expanduser()
PM_PLANS_PATH = HERMES_HOME / 'profiles' / 'project_manager' / 'state' / 'pm_plans.json'
CURRENT_BOARD = HERMES_HOME / 'kanban' / 'current'
STATE_PATH = HERMES_HOME / 'state' / 'pm_kanban_blocked_notifier.json'
REPEATED_BLOCK_THRESHOLD = 3


def _now() -> int:
    return int(time.time())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


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


def _connect(board: str) -> sqlite3.Connection:
    path = _db_path(board)
    if not path.exists():
        raise SystemExit(f'Kanban DB not found: {path}')
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_state() -> dict[str, Any]:
    data = _read_json(STATE_PATH)
    data.setdefault('initialized_at', _now())
    boards = data.get('boards')
    if not isinstance(boards, dict):
        data['boards'] = {}
    return data


def _board_state(state: dict[str, Any], board: str) -> dict[str, Any]:
    boards = state.setdefault('boards', {})
    if not isinstance(boards, dict):
        boards = {}
        state['boards'] = boards
    board_data = boards.setdefault(board, {})
    if not isinstance(board_data, dict):
        board_data = {}
        boards[board] = board_data
    board_data.setdefault('last_event_id', 0)
    return board_data


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return '-'
    return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M:%S')


def _load_pm_plans() -> dict[str, Any]:
    data = _read_json(PM_PLANS_PATH)
    plans = data.get('plans') if isinstance(data.get('plans'), dict) else {}
    return plans if isinstance(plans, dict) else {}


def _mode_for_row(plan: dict[str, Any], row: dict[str, Any]) -> str:
    key = str(row.get('key') or '').strip()
    row_pm_phase = str(row.get('pm_phase') or '').strip()
    if row_pm_phase:
        return row_pm_phase
    for task in plan.get('tasks') or []:
        if isinstance(task, dict) and str(task.get('key') or '').strip() == key:
            mode = str(task.get('mode') or '').strip()
            if mode:
                return mode
            pm_phase = str(task.get('pm_phase') or '').strip()
            if pm_phase:
                return pm_phase
            if task.get('assignee') == 'project_manager' and task.get('parents'):
                return 'final'
    title = str(row.get('title') or '').lower()
    if 'architect' in title or '설계' in title:
        return 'architect'
    if 'implement' in title or '구현' in title or '작성' in title:
        return 'implementer'
    if 'review' in title or 'verify' in title or '검토' in title or '검증' in title:
        return 'reviewer'
    if row.get('assignee') == 'project_manager':
        return 'final'
    return ''


def _find_plan_for_task(task_id: str) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None, str]:
    for plan_id, plan in _load_pm_plans().items():
        if not isinstance(plan, dict) or str(plan.get('status') or '') in {'superseded', 'archived'}:
            continue
        for field in ('created_design_tasks', 'created_followup_tasks', 'created_tasks'):
            for row in plan.get(field) or []:
                if isinstance(row, dict) and str(row.get('task_id') or '') == task_id:
                    return plan_id, plan, row, _mode_for_row(plan, row)
    return None, None, None, ''


def _candidate_events(conn: sqlite3.Connection, last_event_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        '''
        select e.id, e.task_id, e.run_id, e.kind, e.payload, e.created_at,
               t.title, t.assignee, t.status, t.workspace_kind, t.workspace_path
          from task_events e
          join tasks t on t.id = e.task_id
         where e.kind = 'blocked'
           and e.id > ?
         order by e.id asc
        ''',
        (int(last_event_id),),
    ).fetchall()


def _reason_for_event(conn: sqlite3.Connection, event: sqlite3.Row) -> str:
    try:
        payload = json.loads(event['payload']) if event['payload'] else {}
        if isinstance(payload, dict) and payload.get('reason'):
            return str(payload['reason']).strip()
    except Exception:
        pass
    if event['run_id']:
        row = conn.execute(
            'select summary from task_runs where id = ? limit 1',
            (int(event['run_id']),),
        ).fetchone()
        if row and row['summary']:
            return str(row['summary']).strip()
    row = conn.execute(
        "select summary from task_runs where task_id = ? and outcome = 'blocked' order by ended_at desc, id desc limit 1",
        (event['task_id'],),
    ).fetchone()
    if row and row['summary']:
        return str(row['summary']).strip()
    return '(reason unavailable)'


def _blocked_count_for_task(conn: sqlite3.Connection, event: sqlite3.Row) -> int:
    row = conn.execute(
        """
        select count(*) as cnt
          from task_events
         where task_id = ?
           and kind = 'blocked'
           and id <= ?
        """,
        (event['task_id'], int(event['id'])),
    ).fetchone()
    if not row:
        return 0
    return int(row['cnt'] or 0)


def _latest_architect_feedback(conn: sqlite3.Connection, task_id: str) -> str:
    try:
        row = conn.execute(
            """
            select body
              from task_comments
             where task_id = ?
               and author = 'backend-architect'
             order by created_at desc, id desc
             limit 1
            """,
            (task_id,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return ""
    if not row or not row['body']:
        return ""
    return str(row['body']).strip()


def _format_message(
    board: str,
    event: sqlite3.Row,
    plan_id: str,
    phase: str,
    reason: str,
    blocked_count: int,
    architect_feedback: str = "",
) -> str:
    workspace = f"{event['workspace_kind']} @ {event['workspace_path'] or ''}".strip()
    repeated = blocked_count >= REPEATED_BLOCK_THRESHOLD
    title = 'PM 긴급 보고: 작업이 3회 이상 반복 블로킹되었습니다.' if repeated else 'PM 보고: 작업이 블로킹되었습니다.'
    lines = [
        title,
        '',
        f'- Plan ID: {plan_id}',
        f"- Task ID: {event['task_id']}",
        f"- Task: {event['title']}",
        f"- Phase: {phase or '-'}",
        f"- Assignee: {event['assignee']}",
        f"- Block count: {blocked_count}",
        f"- Reason: {reason}",
        f"- 감지 시각: {_fmt_ts(event['created_at'])}",
        f"- 보드: {board}",
        f"- 작업 경로: {workspace}",
    ]
    if repeated:
        lines.extend([
            '',
            '반복 블로킹 경고',
            f'- 이 task가 blocked 상태로 이동한 횟수가 {blocked_count}회입니다.',
            '- 같은 보완/언블록 루프가 해결되지 않는 것으로 보고, 사용자 확인 또는 PM 지시서 재작성 검토가 필요합니다.',
        ])
    if architect_feedback:
        lines.extend([
            '',
            'Architect 피드백',
            architect_feedback[:2000],
        ])
    lines.extend([
        '',
        '다음 액션',
        '필요하면 이 채널에서 @PM으로 수정/재지시/언블록 요청을 보내세요.',
    ])
    return '\n'.join(lines).strip()


def main() -> int:
    board = _read_board()
    conn = _connect(board)
    state = _load_state()
    board_state = _board_state(state, board)
    last_event_id = int(board_state.get('last_event_id') or 0)
    try:
        events = _candidate_events(conn, last_event_id)
    except sqlite3.DatabaseError as exc:
        board_state['last_db_error'] = str(exc)
        board_state['last_db_error_at'] = _now()
        _atomic_write_json(STATE_PATH, state)
        return 0
    if not events:
        if not STATE_PATH.exists():
            _atomic_write_json(STATE_PATH, state)
        return 0

    max_event_id = last_event_id
    for event in events:
        max_event_id = max(max_event_id, int(event['id']))
        plan_id, _plan, _row, phase = _find_plan_for_task(str(event['task_id']))
        if not plan_id:
            continue
        reason = _reason_for_event(conn, event)
        blocked_count = _blocked_count_for_task(conn, event)
        architect_feedback = _latest_architect_feedback(conn, str(event['task_id']))
        print(_format_message(board, event, plan_id, phase, reason, blocked_count, architect_feedback))
        board_state['last_event_id'] = max_event_id
        board_state['last_reported_at'] = _now()
        _atomic_write_json(STATE_PATH, state)
        return 0

    board_state['last_event_id'] = max_event_id
    board_state['last_reported_at'] = _now()
    _atomic_write_json(STATE_PATH, state)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
