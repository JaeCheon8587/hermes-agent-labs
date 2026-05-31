from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path('/home/jjc/.hermes/scripts/pm_design_ready_notifier.py')


@pytest.fixture
def notifier_module(tmp_path, monkeypatch):
    hermes_home = tmp_path / '.hermes'
    plans_path = hermes_home / 'profiles' / 'project_manager' / 'state' / 'pm_plans.json'
    state_path = hermes_home / 'state' / 'pm_design_ready_notifier.json'
    board_path = hermes_home / 'kanban' / 'current'
    board_path.parent.mkdir(parents=True, exist_ok=True)
    board_path.write_text('testboard', encoding='utf-8')
    db_path = hermes_home / 'kanban' / 'boards' / 'testboard' / 'kanban.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute('create table tasks (id text primary key, title text, assignee text, status text, completed_at integer)')
    conn.execute('create table task_comments (id integer primary key autoincrement, task_id text, body text, created_at integer)')
    conn.commit()
    conn.close()

    spec = importlib.util.spec_from_file_location('pm_design_ready_notifier_test', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, 'HERMES_HOME', hermes_home)
    monkeypatch.setattr(module, 'PM_PLANS_PATH', plans_path)
    monkeypatch.setattr(module, 'CURRENT_BOARD', board_path)
    monkeypatch.setattr(module, 'STATE_PATH', state_path)
    return module, plans_path, state_path, db_path


def _write_plan(plans_path: Path, plan: dict) -> None:
    plans_path.parent.mkdir(parents=True, exist_ok=True)
    plans_path.write_text(json.dumps({'plans': {plan['plan_id']: plan}}, ensure_ascii=False, indent=2), encoding='utf-8')


def _write_artifact(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def _read_plan(plans_path: Path, plan_id: str) -> dict:
    return json.loads(plans_path.read_text(encoding='utf-8'))['plans'][plan_id]


def _insert_task(db_path: Path, task_id: str, *, title: str = 'architect', assignee: str = 'backend-specialist', status: str = 'done') -> None:
    conn = sqlite3.connect(db_path)
    completed_at = 123 if status == 'done' else None
    conn.execute('insert into tasks(id,title,assignee,status,completed_at) values(?,?,?,?,?)', (task_id, title, assignee, status, completed_at))
    conn.commit()
    conn.close()


def test_main_emits_for_awaiting_design_approval_plan(notifier_module, capsys):
    module, plans_path, state_path, db_path = notifier_module
    _insert_task(db_path, 't1')
    artifact_path = Path('/tmp/workspace/.soul/artifacts/design/plan_notify_design.md')
    _write_artifact(artifact_path, """# 로그아웃 설계안\n\n## 목적\n로그아웃 기능을 기존 구조 안에 추가한다.\n\n## 권장 설계 방향\n- LoginService 중심 구조를 유지한다.\n- refresh token revoke를 로그아웃 경계로 둔다.\n\n## 변경 대상 후보 파일\n- src/Auth/LoginService.cs\n- tests/Auth.Tests/LoginServiceTests.cs\n\n## 승인 판단 포인트\n- refresh token revoke를 기준으로 볼지 결정한다.\n""")
    plan = {
        'plan_id': 'plan_notify',
        'status': 'awaiting_design_approval',
        'design_status': 'awaiting_approval',
        'project_path': '/tmp/workspace',
        'design_ready_at': 123456,
        'design_summary': 'design ready',
        'design_artifact_path': '.soul/artifacts/design/plan_notify_design.md',
        'design_requested_by': 'pm_workflow_core',
        'created_design_tasks': [{'key': 'T1', 'task_id': 't1'}],
        'workflow_contract': {'path': '/tmp/contract.json'},
    }
    _write_plan(plans_path, plan)

    rc = module.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert 'PM 보고: 설계 검토가 끝났습니다.' in out
    assert '설계 승인 보고' in out
    assert '핵심 설계 방향' in out
    assert 'LoginService 중심 구조를 유지한다.' in out
    assert '승인 판단 포인트' in out
    saved_state = json.loads(state_path.read_text(encoding='utf-8'))
    assert 'plan_notify' in saved_state['reported']
    assert any(key.startswith('design_ready:plan_notify:123456:') for key in saved_state['reported_events'])

    second_rc = module.main()
    second_out = capsys.readouterr().out

    assert second_rc == 0
    assert second_out == ''


def test_main_does_not_mutate_ready_plan_fields(notifier_module, capsys):
    module, plans_path, _state_path, db_path = notifier_module
    _insert_task(db_path, 't1')
    plan = {
        'plan_id': 'plan_no_mutate',
        'status': 'awaiting_design_approval',
        'design_status': 'awaiting_approval',
        'project_path': '/tmp/workspace',
        'design_ready_at': 123456,
        'design_summary': 'keep me',
        'design_artifact_path': '.soul/artifacts/design/plan_no_mutate_design.md',
        'design_requested_by': 'pm_workflow_core',
        'updated_at': 777,
        'created_design_tasks': [{'key': 'T1', 'task_id': 't1'}],
        'workflow_contract': {'path': '/tmp/contract.json'},
    }
    _write_plan(plans_path, plan)

    rc = module.main()
    _ = capsys.readouterr()
    saved = _read_plan(plans_path, 'plan_no_mutate')

    assert rc == 0
    assert saved['status'] == 'awaiting_design_approval'
    assert saved['design_status'] == 'awaiting_approval'
    assert saved['design_requested_by'] == 'pm_workflow_core'
    assert saved['updated_at'] == 777
    assert saved['design_summary'] == 'keep me'


def test_main_re_notifies_after_design_revision_cycle(notifier_module, capsys):
    module, plans_path, state_path, db_path = notifier_module
    _insert_task(db_path, 't1')
    first_plan = {
        'plan_id': 'plan_revision_cycle',
        'status': 'awaiting_design_approval',
        'design_status': 'awaiting_approval',
        'project_path': '/tmp/workspace',
        'design_ready_at': 111,
        'design_summary': 'first design ready',
        'design_artifact_path': '.soul/artifacts/design/plan_revision_cycle_design.md',
        'design_requested_by': 'pm_design_ready_notifier',
        'created_design_tasks': [{'key': 'T1', 'task_id': 't1'}],
        'workflow_contract': {'path': '/tmp/contract.json'},
    }
    _write_plan(plans_path, first_plan)

    first_rc = module.main()
    first_out = capsys.readouterr().out

    assert first_rc == 0
    assert 'PM 보고: 설계 검토가 끝났습니다.' in first_out

    revised_plan = {
        **first_plan,
        'status': 'design_revision_requested',
        'design_status': 'revision_requested',
        'design_summary': 'first design needs revision',
    }
    _write_plan(plans_path, revised_plan)

    revision_rc = module.main()
    revision_out = capsys.readouterr().out

    assert revision_rc == 0
    assert revision_out == ''

    second_ready_plan = {
        **first_plan,
        'design_ready_at': 222,
        'design_summary': 'second design ready',
    }
    _write_plan(plans_path, second_ready_plan)

    second_rc = module.main()
    second_out = capsys.readouterr().out

    assert second_rc == 0
    assert 'PM 보고: 설계 검토가 끝났습니다.' in second_out
    saved_state = json.loads(state_path.read_text(encoding='utf-8'))
    assert saved_state['reported_versions']['plan_revision_cycle'] == 222
