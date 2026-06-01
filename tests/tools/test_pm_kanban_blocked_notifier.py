from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'pm-workflow' / 'pm_kanban_blocked_notifier.py'


@pytest.fixture
def notifier_module(tmp_path, monkeypatch):
    hermes_home = tmp_path / '.hermes'
    plans_path = hermes_home / 'profiles' / 'project_manager' / 'state' / 'pm_plans.json'
    state_path = hermes_home / 'state' / 'pm_kanban_blocked_notifier.json'
    board_path = hermes_home / 'kanban' / 'current'
    board_path.parent.mkdir(parents=True, exist_ok=True)
    board_path.write_text('testboard', encoding='utf-8')
    db_path = hermes_home / 'kanban' / 'boards' / 'testboard' / 'kanban.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute('create table tasks (id text primary key, title text, assignee text, status text, workspace_kind text, workspace_path text)')
    conn.execute('create table task_events (id integer primary key autoincrement, task_id text, run_id integer, kind text, payload text, created_at integer)')
    conn.execute('create table task_runs (id integer primary key autoincrement, task_id text, outcome text, summary text, ended_at integer)')
    conn.commit()
    conn.close()

    spec = importlib.util.spec_from_file_location('pm_kanban_blocked_notifier_test', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, 'HERMES_HOME', hermes_home)
    monkeypatch.setattr(module, 'PM_PLANS_PATH', plans_path)
    monkeypatch.setattr(module, 'CURRENT_BOARD', board_path)
    monkeypatch.setattr(module, 'STATE_PATH', state_path)
    return module, plans_path, state_path, db_path


def _write_plans(plans_path: Path, plans: dict) -> None:
    plans_path.parent.mkdir(parents=True, exist_ok=True)
    plans_path.write_text(json.dumps({'plans': plans}, ensure_ascii=False, indent=2), encoding='utf-8')


def _insert_task(db_path: Path, *, task_id: str, title: str, assignee: str, status: str = 'blocked') -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        'insert into tasks(id,title,assignee,status,workspace_kind,workspace_path) values(?,?,?,?,?,?)',
        (task_id, title, assignee, status, 'dir', '/tmp/workspace'),
    )
    conn.commit()
    conn.close()


def _insert_block_event(db_path: Path, *, task_id: str, reason: str, created_at: int = 111) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        'insert into task_events(task_id,run_id,kind,payload,created_at) values(?,?,?,?,?)',
        (task_id, 1, 'blocked', json.dumps({'reason': reason}, ensure_ascii=False), created_at),
    )
    conn.execute(
        'insert into task_runs(task_id,outcome,summary,ended_at) values(?,?,?,?)',
        (task_id, 'blocked', reason, created_at),
    )
    conn.commit()
    conn.close()


def test_main_emits_blocked_message_for_pm_plan_task(notifier_module, capsys):
    module, plans_path, state_path, db_path = notifier_module
    _insert_task(db_path, task_id='t_blocked', title='Review docs-only compliance', assignee='backend-specialist')
    _insert_block_event(db_path, task_id='t_blocked', reason='scope mismatch')
    _write_plans(plans_path, {
        'plan_blocked': {
            'plan_id': 'plan_blocked',
            'status': 'executed',
            'project_path': '/tmp/workspace',
            'tasks': [
                {'key': 'T3', 'mode': 'reviewer', 'title': 'Review docs-only compliance'},
            ],
            'created_followup_tasks': [
                {'key': 'T3', 'task_id': 't_blocked', 'title': 'Review docs-only compliance', 'assignee': 'backend-specialist'},
            ],
        }
    })

    rc = module.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert 'PM 보고: 작업이 블로킹되었습니다.' in out
    assert '- Plan ID: plan_blocked' in out
    assert '- Task ID: t_blocked' in out
    assert '- Phase: reviewer' in out
    assert '- Reason: scope mismatch' in out
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['boards']['testboard']['last_event_id'] == 1


def test_main_dedupes_already_reported_blocked_event(notifier_module, capsys):
    module, plans_path, state_path, db_path = notifier_module
    _insert_task(db_path, task_id='t_blocked', title='Review docs-only compliance', assignee='backend-specialist')
    _insert_block_event(db_path, task_id='t_blocked', reason='scope mismatch')
    _write_plans(plans_path, {
        'plan_blocked': {
            'plan_id': 'plan_blocked',
            'status': 'executed',
            'project_path': '/tmp/workspace',
            'tasks': [{'key': 'T3', 'mode': 'reviewer', 'title': 'Review docs-only compliance'}],
            'created_followup_tasks': [{'key': 'T3', 'task_id': 't_blocked', 'title': 'Review docs-only compliance', 'assignee': 'backend-specialist'}],
        }
    })
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({'initialized_at': 1, 'boards': {'testboard': {'last_event_id': 1}}}, ensure_ascii=False, indent=2), encoding='utf-8')

    rc = module.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert out == ''


def test_main_reports_design_gate_pm_phase_instead_of_final(notifier_module, capsys):
    module, plans_path, state_path, db_path = notifier_module
    _insert_task(db_path, task_id='t_gate_blocked', title='설계 승인 게이트 확인 및 구현 작업 패키지화', assignee='project_manager')
    _insert_block_event(db_path, task_id='t_gate_blocked', reason='waiting for revised design summary')
    _write_plans(plans_path, {
        'plan_gate_blocked': {
            'plan_id': 'plan_gate_blocked',
            'status': 'executed',
            'project_path': '/tmp/workspace',
            'tasks': [
                {'key': 'T1', 'mode': 'architect', 'title': 'architect'},
                {'key': 'T2', 'title': '설계 승인 게이트 확인 및 구현 작업 패키지화', 'assignee': 'project_manager', 'pm_phase': 'design_approval_gate', 'parents': ['T1']},
                {'key': 'T3', 'mode': 'implementer', 'title': 'implementer', 'parents': ['T2']},
            ],
            'created_followup_tasks': [
                {'key': 'T2', 'task_id': 't_gate_blocked', 'title': '설계 승인 게이트 확인 및 구현 작업 패키지화', 'assignee': 'project_manager', 'pm_phase': 'design_approval_gate'},
            ],
        }
    })

    rc = module.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert '- Phase: design_approval_gate' in out
    assert '- Phase: final' not in out
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['boards']['testboard']['last_event_id'] == 1


def test_legacy_global_last_event_id_does_not_suppress_current_board(notifier_module, capsys):
    module, plans_path, state_path, db_path = notifier_module
    _insert_task(db_path, task_id='t_board_local', title='Board-local blocked event', assignee='backend-architect')
    _insert_block_event(db_path, task_id='t_board_local', reason='board local event id is lower than legacy global id')
    _write_plans(plans_path, {
        'plan_board_local': {
            'plan_id': 'plan_board_local',
            'status': 'executed',
            'project_path': '/tmp/workspace',
            'tasks': [{'key': 'T1', 'mode': 'architect', 'title': 'Board-local blocked event'}],
            'created_design_tasks': [{'key': 'T1', 'task_id': 't_board_local', 'title': 'Board-local blocked event', 'assignee': 'backend-architect'}],
            'created_tasks': [{'key': 'T1', 'task_id': 't_board_local', 'title': 'Board-local blocked event', 'assignee': 'backend-architect'}],
        }
    })
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({'initialized_at': 1, 'last_event_id': 1900}, ensure_ascii=False, indent=2), encoding='utf-8')

    rc = module.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert 'PM 보고: 작업이 블로킹되었습니다.' in out
    assert '- Plan ID: plan_board_local' in out
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['last_event_id'] == 1900
    assert state['boards']['testboard']['last_event_id'] == 1


def test_main_escalates_when_pm_task_blocks_three_times(notifier_module, capsys):
    module, plans_path, state_path, db_path = notifier_module
    _insert_task(db_path, task_id='t_repeat_blocked', title='도서 등록 API 설계', assignee='backend-architect')
    _insert_block_event(db_path, task_id='t_repeat_blocked', reason='작업 지시서 보완 필요', created_at=111)
    _insert_block_event(db_path, task_id='t_repeat_blocked', reason='작업 지시서 보완 필요', created_at=222)
    _insert_block_event(db_path, task_id='t_repeat_blocked', reason='작업 지시서 보완 필요', created_at=333)
    _write_plans(plans_path, {
        'plan_repeat_blocked': {
            'plan_id': 'plan_repeat_blocked',
            'status': 'executed',
            'project_path': '/tmp/workspace',
            'tasks': [{'key': 'T1', 'mode': 'architect', 'title': '도서 등록 API 설계'}],
            'created_design_tasks': [{'key': 'T1', 'task_id': 't_repeat_blocked', 'title': '도서 등록 API 설계', 'assignee': 'backend-architect'}],
            'created_tasks': [{'key': 'T1', 'task_id': 't_repeat_blocked', 'title': '도서 등록 API 설계', 'assignee': 'backend-architect'}],
        }
    })
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({'initialized_at': 1, 'boards': {'testboard': {'last_event_id': 2}}}, ensure_ascii=False, indent=2), encoding='utf-8')

    rc = module.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert 'PM 긴급 보고: 작업이 3회 이상 반복 블로킹되었습니다.' in out
    assert '- Block count: 3' in out
    assert '반복 블로킹 경고' in out
    assert '사용자 확인 또는 PM 지시서 재작성 검토가 필요합니다' in out
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['boards']['testboard']['last_event_id'] == 3
