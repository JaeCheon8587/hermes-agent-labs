#!/usr/bin/env python3
"""Notify when staged PM design tasks are complete and ready for design approval.

Designed for Hermes cron with no_agent=True:
- Detects PM plans in design_in_progress whose created architect/design tasks are all done.
- Marks the plan awaiting_design_approval in PM plan state.
- Prints a Slack-ready design approval request once per plan.
- Prints nothing when there is nothing new.
"""
from __future__ import annotations

import os
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
STATE_PATH = HERMES_HOME / 'state' / 'pm_design_ready_notifier.json'


def _now() -> int:
    return int(time.time())


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return '-'
    return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M:%S')


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except Exception:
        return ''


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
    data.setdefault('reported', [])
    reported_versions = data.get('reported_versions')
    if not isinstance(reported_versions, dict):
        data['reported_versions'] = {}
    reported_events = data.get('reported_events')
    if not isinstance(reported_events, dict):
        data['reported_events'] = {}
    return data


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _design_ready_event_key(plan_id: str, plan: dict[str, Any]) -> str:
    artifact = _design_artifact(plan)
    artifact_text = _read_text(_artifact_abspath(plan, artifact)) if artifact else ''
    artifact_digest = _sha256_text(artifact_text)[:16] if artifact_text else 'no-artifact'
    version = _design_ready_version(plan)
    if version is None:
        version = int(plan.get('updated_at') or plan.get('created_at') or 0)
    return f"design_ready:{plan_id}:{version}:{artifact_digest}"


def _design_ready_version(plan: dict[str, Any]) -> int | None:
    raw = plan.get('design_ready_at')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _was_reported(state: dict[str, Any], plan_id: str, plan: dict[str, Any]) -> bool:
    reported_events = state.get('reported_events') if isinstance(state.get('reported_events'), dict) else {}
    event_key = _design_ready_event_key(plan_id, plan)
    if event_key in reported_events:
        return True
    version = _design_ready_version(plan)
    reported_versions = state.get('reported_versions') if isinstance(state.get('reported_versions'), dict) else {}
    if version is not None and reported_versions.get(plan_id) == version:
        return True
    legacy_reported = state.get('reported') if isinstance(state.get('reported'), list) else []
    return version is None and plan_id in legacy_reported


def _task_statuses(conn: sqlite3.Connection, task_ids: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tid in task_ids:
        row = conn.execute('select id,title,assignee,status,completed_at from tasks where id=?', (tid,)).fetchone()
        if row is None:
            rows.append({'id': tid, 'status': 'missing'})
        else:
            rows.append(dict(row))
    return rows


def _latest_design_comment(conn: sqlite3.Connection, task_ids: list[str]) -> str:
    if not task_ids:
        return ''
    qmarks = ','.join('?' for _ in task_ids)
    row = conn.execute(
        f'select body from task_comments where task_id in ({qmarks}) order by created_at desc, id desc limit 1',
        task_ids,
    ).fetchone()
    return str(row['body']).strip() if row and row['body'] else ''


def _design_artifact(plan: dict[str, Any]) -> str:
    explicit = str(plan.get('design_artifact_path') or '').strip()
    if explicit:
        return explicit
    meta = plan.get('workflow_contract') if isinstance(plan.get('workflow_contract'), dict) else {}
    path = meta.get('path')
    if path:
        contract = _read_json(Path(path))
        artifacts = contract.get('artifacts') if isinstance(contract.get('artifacts'), dict) else {}
        if artifacts.get('design'):
            return str(artifacts['design'])
    return f".soul/artifacts/design/{plan.get('plan_id')}_design.md"


def _artifact_abspath(plan: dict[str, Any], artifact: str) -> Path:
    candidate = Path(artifact)
    if candidate.is_absolute():
        return candidate
    project_path = Path(str(plan.get('project_path') or '').strip() or '.')
    return project_path / candidate


def _split_markdown_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {'_root': []}
    current = '_root'
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith('## '):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if line.startswith('# '):
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _clean_section_items(lines: list[str], limit: int = 4) -> list[str]:
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith('### '):
            line = line[4:].strip()
        elif line.startswith('- '):
            line = line[2:].strip()
        elif line[:2].isdigit() and '. ' in line[:4]:
            line = line.split('. ', 1)[1].strip()
        if not line:
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def _artifact_report_sections(plan: dict[str, Any], artifact: str) -> dict[str, list[str]]:
    text = _read_text(_artifact_abspath(plan, artifact))
    if not text:
        return {}
    sections = _split_markdown_sections(text)
    wanted = [
        '목적',
        '권장 설계 방향',
        '변경 대상 후보 파일',
        '영향 범위',
        '구현 범위 / 비범위',
        '승인 판단 포인트',
        'QA handoff 항목',
    ]
    return {name: _clean_section_items(sections.get(name, [])) for name in wanted if sections.get(name)}


def _design_summary_text(plan: dict[str, Any], artifact: str, summary: str) -> str:
    sections = _artifact_report_sections(plan, artifact)
    lines: list[str] = []
    ordered = [
        ('설계 목적', '목적'),
        ('핵심 설계 방향', '권장 설계 방향'),
        ('주요 변경 대상', '변경 대상 후보 파일'),
        ('영향 범위', '영향 범위'),
        ('구현 범위 / 비범위', '구현 범위 / 비범위'),
        ('승인 판단 포인트', '승인 판단 포인트'),
        ('QA handoff 예정 항목', 'QA handoff 항목'),
    ]
    for label, key in ordered:
        items = sections.get(key) or []
        if not items:
            continue
        lines.append(label)
        lines.extend(f'- {item}' for item in items)
        lines.append('')
    if not lines:
        return summary or '설계 산출물은 준비되었지만 상세 요약을 추출하지 못했습니다. 설계 문서를 직접 확인해 주세요.'
    return '\n'.join(lines).strip()


def _mark_design_ready(plans_data: dict[str, Any], plan_id: str, plan: dict[str, Any], summary: str, artifact: str) -> None:
    now = _now()
    plan['status'] = 'awaiting_design_approval'
    plan['design_status'] = 'awaiting_approval'
    plan['design_summary'] = summary
    plan['design_artifact_path'] = artifact
    plan['design_ready_at'] = now
    plan['design_requested_by'] = 'pm_design_ready_notifier'
    plan['updated_at'] = now
    plans_data.setdefault('plans', {})[plan_id] = plan
    _atomic_write_json(PM_PLANS_PATH, plans_data)


def _candidate_plans(conn: sqlite3.Connection, plans: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, dict[str, Any], list[dict[str, Any]], bool]]:
    out = []
    for plan_id, plan in plans.items():
        if not isinstance(plan, dict) or _was_reported(state, plan_id, plan):
            continue
        if str(plan.get('status') or '') in {'superseded', 'archived'}:
            continue
        design = [r for r in (plan.get('created_design_tasks') or []) if isinstance(r, dict) and r.get('task_id')]
        if not design:
            continue
        task_ids = [str(r['task_id']) for r in design]
        statuses = _task_statuses(conn, task_ids)
        if not statuses or not all(row.get('status') == 'done' for row in statuses):
            continue
        legacy_transition_needed = False
        status = str(plan.get('status') or '')
        design_status = str(plan.get('design_status') or '')
        if status == 'design_in_progress' and design_status == 'in_progress':
            legacy_transition_needed = True
        elif not (status == 'awaiting_design_approval' and design_status == 'awaiting_approval'):
            continue
        out.append((plan_id, plan, statuses, legacy_transition_needed))
    # Newest first: old abandoned smoke-test plans may still be design_in_progress.
    return sorted(out, key=lambda item: int(item[1].get('created_at') or item[1].get('updated_at') or 0), reverse=True)


def _format_message(board: str, plan_id: str, plan: dict[str, Any], statuses: list[dict[str, Any]], artifact: str, summary: str) -> str:
    detail = _design_summary_text(plan, artifact, summary)
    lines = [
        'PM 보고: 설계 검토가 끝났습니다.',
        '',
        f'- Plan ID: {plan_id}',
        f"- 작업 경로: {plan.get('project_path') or '-'}",
        f'- 준비 시각: {_fmt_ts(int(plan.get("design_ready_at") or _now()))}',
        f'- 보드: {board}',
        '',
        '설계 승인 보고',
        detail,
        '',
        f'설계 산출물: {artifact}',
        '',
        '완료된 설계 작업',
    ]
    for row in statuses:
        lines.append(f"- {row.get('title')} ({row.get('id')}, {row.get('status')})")
    lines.extend([
        '',
        '승인 후 다음 단계',
        '- 구현 카드 생성',
        '- 리뷰/검증 카드 생성',
        '- 최종 PM 보고 카드 생성',
        '',
        '승인하려면 아래 문구로 답변하세요.',
        f'승인. {plan_id} 설계 승인, 구현 진행해',
        '보류하거나 수정이 필요하면 변경 요청을 함께 적어 주세요.',
    ])
    return '\n'.join(lines).strip()


def main() -> int:
    board = _read_board()
    conn = _connect(board)
    state = _load_state()
    plans_data = _read_json(PM_PLANS_PATH)
    plans = plans_data.get('plans') if isinstance(plans_data.get('plans'), dict) else {}
    reported_versions = state.get('reported_versions') if isinstance(state.get('reported_versions'), dict) else {}
    legacy_reported = state.get('reported') if isinstance(state.get('reported'), list) else []
    for plan_id in legacy_reported:
        plan = plans.get(plan_id)
        if not isinstance(plan, dict) or plan_id in reported_versions:
            continue
        version = _design_ready_version(plan)
        if version is not None:
            reported_versions[plan_id] = version
    state['reported_versions'] = reported_versions
    candidates = _candidate_plans(conn, plans, state)
    if not candidates:
        current_state = _read_json(STATE_PATH)
        if not STATE_PATH.exists() or not isinstance(current_state.get('reported_events'), dict):
            _atomic_write_json(STATE_PATH, state)
        return 0

    plan_id, plan, statuses, legacy_transition_needed = candidates[0]
    task_ids = [str(row.get('id')) for row in statuses if row.get('id')]
    artifact = _design_artifact(plan)
    latest_comment = _latest_design_comment(conn, task_ids)
    summary = latest_comment or str(plan.get('design_summary') or '').strip() or 'Architect design evidence is ready; awaiting explicit user design approval.'
    if legacy_transition_needed:
        _mark_design_ready(plans_data, plan_id, plan, summary, artifact)
        # Reload changed fields for display.
        plan = (_read_json(PM_PLANS_PATH).get('plans') or {}).get(plan_id, plan)
    version = _design_ready_version(plan)
    if version is not None:
        reported_versions[plan_id] = version
        state['reported_versions'] = reported_versions
    reported_events = state.get('reported_events') if isinstance(state.get('reported_events'), dict) else {}
    event_key = _design_ready_event_key(plan_id, plan)
    reported_events[event_key] = {
        'plan_id': plan_id,
        'design_ready_at': version,
        'artifact': artifact,
        'reported_at': _now(),
    }
    state['reported_events'] = reported_events
    reported = {pid for pid in legacy_reported if isinstance(pid, str)}
    reported.add(plan_id)
    state['reported'] = sorted(reported)
    state['last_reported_at'] = _now()
    _atomic_write_json(STATE_PATH, state)
    print(_format_message(board, plan_id, plan, statuses, artifact, summary))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
