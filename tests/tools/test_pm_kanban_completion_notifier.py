from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path('/home/jjc/.hermes/scripts/pm_kanban_completion_notifier.py')


@pytest.fixture
def notifier_module(tmp_path, monkeypatch):
    hermes_home = tmp_path / '.hermes'
    plans_path = hermes_home / 'profiles' / 'project_manager' / 'state' / 'pm_plans.json'
    state_path = hermes_home / 'state' / 'pm_kanban_completion_notifier.json'
    board_path = hermes_home / 'kanban' / 'current'
    board_path.parent.mkdir(parents=True, exist_ok=True)
    board_path.write_text('testboard', encoding='utf-8')
    db_path = hermes_home / 'kanban' / 'boards' / 'testboard' / 'kanban.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute('create table tasks (id text primary key, title text, assignee text, status text, workspace_kind text, workspace_path text, completed_at integer, result text, created_at integer)')
    conn.execute('create table task_links (parent_id text, child_id text)')
    conn.execute('create table task_runs (id integer primary key autoincrement, task_id text, outcome text, summary text, ended_at integer)')
    conn.commit()
    conn.close()

    spec = importlib.util.spec_from_file_location('pm_kanban_completion_notifier_test', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(sys, 'argv', ['pm_kanban_completion_notifier.py'])
    monkeypatch.setattr(module, 'HERMES_HOME', hermes_home)
    monkeypatch.setattr(module, 'PM_PLANS_PATH', plans_path)
    monkeypatch.setattr(module, 'CURRENT_BOARD', board_path)
    monkeypatch.setattr(module, 'STATE_PATH', state_path)
    return module, plans_path, state_path, db_path


def _write_plans(plans_path: Path, plans: dict) -> None:
    plans_path.parent.mkdir(parents=True, exist_ok=True)
    plans_path.write_text(json.dumps({'plans': plans}, ensure_ascii=False, indent=2), encoding='utf-8')


def _insert_done_pm_task(db_path: Path, task_id: str, title: str, parent_id: str = 't_parent') -> None:
    conn = sqlite3.connect(db_path)
    conn.execute('insert into tasks(id,title,assignee,status,workspace_kind,workspace_path,completed_at,result,created_at) values(?,?,?,?,?,?,?,?,?)', (parent_id, 'architect', 'backend-specialist', 'done', 'dir', '/tmp/workspace', 100, 'parent done', 90))
    conn.execute('insert into tasks(id,title,assignee,status,workspace_kind,workspace_path,completed_at,result,created_at) values(?,?,?,?,?,?,?,?,?)', (task_id, title, 'project_manager', 'done', 'dir', '/tmp/workspace', 111, 'gate done', 100))
    conn.execute('insert into task_links(parent_id,child_id) values(?,?)', (parent_id, task_id))
    conn.execute('insert into task_runs(task_id,outcome,summary,ended_at) values(?,?,?,?)', (task_id, 'completed', 'gate done', 111))
    conn.commit()
    conn.close()


def test_completion_notifier_skips_design_gate_pm_task(notifier_module, capsys):
    module, plans_path, state_path, db_path = notifier_module
    _insert_done_pm_task(db_path, 't_gate', '설계 승인 게이트 확인 및 구현 작업 패키지화')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({'initialized_at': 0, 'reported': []}, ensure_ascii=False), encoding='utf-8')
    _write_plans(plans_path, {
        'plan_gate': {
            'plan_id': 'plan_gate',
            'status': 'executed',
            'project_path': '/tmp/workspace',
            'tasks': [
                {'key': 'T1', 'mode': 'architect', 'assignee': 'backend-specialist', 'parents': []},
                {'key': 'T2', 'assignee': 'project_manager', 'parents': ['T1']},
                {'key': 'T3', 'mode': 'implementer', 'assignee': 'backend-specialist', 'parents': ['T2']},
                {'key': 'T4', 'mode': 'reviewer', 'assignee': 'backend-specialist', 'parents': ['T3']},
                {'key': 'T5', 'title': 'final', 'assignee': 'project_manager', 'parents': ['T4']},
            ],
            'workflow_contract': {'path': ''},
            'created_followup_tasks': [
                {'key': 'T2', 'task_id': 't_gate', 'title': '설계 승인 게이트 확인 및 구현 작업 패키지화', 'assignee': 'project_manager'},
            ],
        }
    })

    rc = module.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert out == ''


def test_completion_notifier_accepts_final_parent_with_korean_review_title(notifier_module):
    module, plans_path, _state_path, db_path = notifier_module
    conn = sqlite3.connect(db_path)
    conn.execute(
        'insert into tasks(id,title,assignee,status,workspace_kind,workspace_path,completed_at,result,created_at) values(?,?,?,?,?,?,?,?,?)',
        ('t_parent_review', '최종 재리뷰 및 해소 확인', 'backend-specialist', 'done', 'dir', '/tmp/workspace', 105, 'review complete', 95),
    )
    conn.execute(
        'insert into tasks(id,title,assignee,status,workspace_kind,workspace_path,completed_at,result,created_at) values(?,?,?,?,?,?,?,?,?)',
        ('t_final', '최종 증적 종합 및 완료 정리', 'project_manager', 'done', 'dir', '/tmp/workspace', 111, 'final done', 100),
    )
    conn.execute('insert into task_links(parent_id,child_id) values(?,?)', ('t_parent_review', 't_final'))
    conn.execute('insert into task_runs(task_id,outcome,summary,ended_at) values(?,?,?,?)', ('t_final', 'completed', 'final done', 111))
    conn.commit()
    conn.close()
    _write_plans(plans_path, {
        'plan_ok': {
            'plan_id': 'plan_ok',
            'status': 'completed_with_followup',
            'project_path': '/tmp/workspace',
            'tasks': [
                {'key': 'T1', 'mode': 'architect', 'assignee': 'backend-specialist', 'parents': []},
                {'key': 'T2', 'mode': 'implementer', 'assignee': 'backend-specialist', 'parents': ['T1']},
                {'key': 'T3', 'mode': 'reviewer', 'title': '최종 재리뷰 및 해소 확인', 'assignee': 'backend-specialist', 'parents': ['T2']},
                {'key': 'T4', 'title': '최종 증적 종합 및 완료 정리', 'assignee': 'project_manager', 'parents': ['T3']},
            ],
            'workflow_contract': {'path': ''},
            'created_followup_tasks': [
                {'key': 'T3', 'task_id': 't_parent_review', 'title': '최종 재리뷰 및 해소 확인', 'assignee': 'backend-specialist'},
                {'key': 'T4', 'task_id': 't_final', 'title': '최종 증적 종합 및 완료 정리', 'assignee': 'project_manager'},
            ],
        }
    })
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    task = conn.execute('select * from tasks where id=?', ('t_final',)).fetchone()
    parents = module._parents(conn, 't_final')
    validation = module._validate_completion(conn, task, parents)
    conn.close()

    assert validation['ok'] is True
    assert 'final task does not directly depend on a reviewer/verification task' not in validation['issues']


def test_completion_notifier_accepts_dedicated_backend_worker_profiles(notifier_module):
    module, plans_path, _state_path, db_path = notifier_module
    project = db_path.parent / 'workspace'
    (project / 'src').mkdir(parents=True)
    (project / 'tests').mkdir(parents=True)
    (project / 'src' / 'BookApi.cs').write_text('// api', encoding='utf-8')
    (project / 'tests' / 'BookApiTests.cs').write_text('// tests', encoding='utf-8')
    contract_path = project / '.soul' / 'workflows' / 'plan_dedicated' / 'contract.json'
    contract_path.parent.mkdir(parents=True)
    contract_path.write_text(json.dumps({
        'project_path': str(project),
        'expected_deliverables': ['src/BookApi.cs', 'tests/BookApiTests.cs'],
    }), encoding='utf-8')

    conn = sqlite3.connect(db_path)
    conn.execute(
        'insert into tasks(id,title,assignee,status,workspace_kind,workspace_path,completed_at,result,created_at) values(?,?,?,?,?,?,?,?,?)',
        ('t_review', 'GET /books API 리뷰', 'backend-reviewer', 'done', 'dir', str(project), 105, 'review complete', 95),
    )
    conn.execute(
        'insert into tasks(id,title,assignee,status,workspace_kind,workspace_path,completed_at,result,created_at) values(?,?,?,?,?,?,?,?,?)',
        ('t_final_dedicated', '최종 종합 보고', 'project_manager', 'done', 'dir', str(project), 111, 'final done', 100),
    )
    conn.execute('insert into task_links(parent_id,child_id) values(?,?)', ('t_review', 't_final_dedicated'))
    conn.execute('insert into task_runs(task_id,outcome,summary,ended_at) values(?,?,?,?)', ('t_final_dedicated', 'completed', 'final done', 111))
    conn.commit()
    conn.close()

    _write_plans(plans_path, {
        'plan_dedicated': {
            'plan_id': 'plan_dedicated',
            'status': 'completed_with_followup',
            'project_path': str(project),
            'tasks': [
                {'key': 'T1', 'mode': 'architect', 'assignee': 'backend-architect', 'parents': []},
                {'key': 'T2', 'mode': 'implementer', 'assignee': 'backend-implementer', 'parents': ['T1']},
                {'key': 'T3', 'mode': 'reviewer', 'title': 'GET /books API 리뷰', 'assignee': 'backend-reviewer', 'parents': ['T2']},
                {'key': 'T4', 'title': '최종 종합 보고', 'assignee': 'project_manager', 'parents': ['T3']},
            ],
            'workflow_contract': {'path': str(contract_path)},
            'created_followup_tasks': [
                {'key': 'T2', 'task_id': 't_impl', 'title': 'GET /books API 구현', 'assignee': 'backend-implementer'},
                {'key': 'T3', 'task_id': 't_review', 'title': 'GET /books API 리뷰', 'assignee': 'backend-reviewer'},
                {'key': 'T4', 'task_id': 't_final_dedicated', 'title': '최종 종합 보고', 'assignee': 'project_manager'},
            ],
        }
    })

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    task = conn.execute('select * from tasks where id=?', ('t_final_dedicated',)).fetchone()
    parents = module._parents(conn, 't_final_dedicated')
    validation = module._validate_completion(conn, task, parents)
    conn.close()

    assert validation['ok'] is True
    assert 'PM plan has expected deliverables but no implementer follow-up task' not in validation['issues']
    assert 'PM plan has expected deliverables but no reviewer follow-up task' not in validation['issues']


def test_completion_notifier_emits_once_and_records_event_key(notifier_module, capsys):
    module, plans_path, state_path, db_path = notifier_module
    project = db_path.parent / 'workspace_once'
    project.mkdir(parents=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({'initialized_at': 1, 'reported': []}, ensure_ascii=False), encoding='utf-8')
    conn = sqlite3.connect(db_path)
    conn.execute(
        'insert into tasks(id,title,assignee,status,workspace_kind,workspace_path,completed_at,result,created_at) values(?,?,?,?,?,?,?,?,?)',
        ('t_review_once', '독립 리뷰', 'backend-reviewer', 'done', 'dir', str(project), 105, 'review complete', 95),
    )
    conn.execute(
        'insert into tasks(id,title,assignee,status,workspace_kind,workspace_path,completed_at,result,created_at) values(?,?,?,?,?,?,?,?,?)',
        ('t_final_once', '최종 PM 보고', 'project_manager', 'done', 'dir', str(project), 111, 'final done', 100),
    )
    conn.execute('insert into task_links(parent_id,child_id) values(?,?)', ('t_review_once', 't_final_once'))
    conn.execute('insert into task_runs(task_id,outcome,summary,ended_at) values(?,?,?,?)', ('t_review_once', 'completed', 'review complete', 105))
    conn.execute('insert into task_runs(task_id,outcome,summary,ended_at) values(?,?,?,?)', ('t_final_once', 'completed', 'final done', 111))
    conn.commit()
    conn.close()
    _write_plans(plans_path, {
        'plan_once': {
            'plan_id': 'plan_once',
            'status': 'completed',
            'project_path': str(project),
            'tasks': [
                {'key': 'T1', 'mode': 'architect', 'assignee': 'backend-architect', 'parents': []},
                {'key': 'T2', 'mode': 'implementer', 'assignee': 'backend-implementer', 'parents': ['T1']},
                {'key': 'T3', 'mode': 'reviewer', 'title': '독립 리뷰', 'assignee': 'backend-reviewer', 'parents': ['T2']},
                {'key': 'T4', 'title': '최종 PM 보고', 'assignee': 'project_manager', 'parents': ['T3']},
            ],
            'workflow_contract': {'path': ''},
            'created_followup_tasks': [
                {'key': 'T3', 'task_id': 't_review_once', 'title': '독립 리뷰', 'assignee': 'backend-reviewer'},
                {'key': 'T4', 'task_id': 't_final_once', 'title': '최종 PM 보고', 'assignee': 'project_manager'},
            ],
        }
    })

    first_rc = module.main()
    first_out = capsys.readouterr().out
    saved_state = json.loads(state_path.read_text(encoding='utf-8'))

    assert first_rc == 0
    assert 'PM 최종 보고: 작업이 완료되었습니다.' in first_out
    assert 't_final_once' in saved_state['reported']
    assert any(key.startswith('completion:t_final_once:111:') for key in saved_state['reported_events'])

    second_rc = module.main()
    second_out = capsys.readouterr().out

    assert second_rc == 0
    assert second_out == ''
