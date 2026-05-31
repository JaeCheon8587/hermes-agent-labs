from __future__ import annotations

import json
from pathlib import Path

from tools import pm_claude_delegation as runner


def test_prepare_command_argv_adds_readonly_write_guards_for_claude():
    argv = runner._prepare_command_argv("claude -p --max-turns 8", readonly=True)
    assert argv[-2:] == ["--allowedTools", "Read,Grep,Glob"]


def test_prepare_command_argv_preserves_explicit_tool_policy():
    argv = runner._prepare_command_argv("claude -p --max-turns 8 --allowedTools Read", readonly=True)
    assert argv == ["claude", "-p", "--max-turns", "8", "--allowedTools", "Read"]

def _valid_architect_artifact(extra: str = "") -> str:
    sections = [
        "## 상태\n설계 완료. 구현은 시작하지 않음.",
        "## 설계 요약\n요구사항 기준 설계 요약을 작성함.",
        "## 목표 / 범위\n목표와 제외 범위를 분리함.",
        "## 현황 분석\n기존 구조를 관찰한 근거를 작성함.",
        "## API / DTO 계약\nGET /books/{isbn}, 200/404 응답 계약을 작성함.",
        "## 검토한 대안\n대안 A와 B를 비교함.",
        "## 선택한 설계\n최소 변경 설계를 선택함.",
        "## 영향 범위\nHost/Application/Infrastructure/test 영향을 작성함.",
        "## 구현 작업분해\n1. repository 2. service 3. endpoint 4. tests 순서.",
        "## 검증 계획\n정상 200과 미존재 404를 검증함.",
        "## 리스크와 완화 방안\nISBN exact match 정책을 리스크로 기록함.",
        "## 사용자 확인 필요사항\n없음. 기본 정책으로 진행 가능.",
        "## 구현 승인 전제\n사용자 승인 전 구현하지 않음.",
    ]
    return "\n\n".join(sections) + ("\n" + extra if extra else "")


def test_runner_executes_command_with_prompt_and_writes_manifest(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    prompt = workdir / ".soul" / "prompts" / "architect.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("design this\n- `## 상태`, `## 설계 요약`, `## 목표 / 범위`, `## 현황 분석`, `## API / DTO 계약`, `## 검토한 대안`, `## 선택한 설계`, `## 영향 범위`, `## 구현 작업분해`, `## 검증 계획`, `## 리스크와 완화 방안`, `## 사용자 확인 필요사항`, `## 구현 승인 전제`", encoding="utf-8")
    artifact = workdir / ".soul" / "artifacts" / "design" / "plan_1_design.md"
    manifest = workdir / ".soul" / "artifacts" / "claude" / "plan_1_t_abc_architect_manifest.json"

    valid = _valid_architect_artifact()
    script = workdir / "emit_valid.py"
    script.write_text("print(" + repr(valid) + ")", encoding="utf-8")

    result = runner.run_delegation(
        mode="architect",
        plan_id="plan_1",
        task_id="t_abc",
        workdir=str(workdir),
        command=f"python3 {script}",
        prompt_file=str(prompt),
        output_format="design",
        artifact_path=str(artifact),
        manifest_path=str(manifest),
        readonly=True,
        timeout=30,
    )

    assert result["ok"] is True
    assert artifact.read_text(encoding="utf-8").strip() == valid
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["runner"] == "pm_claude_delegation"
    assert data["mode"] == "architect"
    assert data["plan_id"] == "plan_1"
    assert data["task_id"] == "t_abc"
    assert data["status"] == "completed"
    assert data["exit_code"] == 0
    assert data["readonly"] is True
    assert Path(data["stdout_path"]).is_file()



def test_architect_runner_composes_claude_prompt_from_pm_envelope(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    prompt = workdir / ".soul" / "prompts" / "architect_envelope.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text(
        "# Architect Task Envelope\n\n"
        "[역할 경계]\n"
        "- 이 파일은 PM이 작성한 작업 지시 envelope이다. Claude Code 실행 prompt가 아니다.\n\n"
        "[작업 지시]\n"
        "- GET /books API 설계. 제목/저자/availability 응답, 빈 목록 200 + [] 검증.\n"
        "- production 코드 루트: src\n"
        "- 사용자 승인 전 구현을 시작하지 않는다.\n",
        encoding="utf-8",
    )
    artifact = workdir / ".soul" / "artifacts" / "design" / "plan_design.md"
    manifest = workdir / ".soul" / "artifacts" / "claude" / "plan_t_architect_manifest.json"
    seen_prompt = workdir / "seen_prompt.md"
    script = workdir / "capture_prompt.py"
    script.write_text(
        "import sys\n"
        f"from pathlib import Path; Path({str(seen_prompt)!r}).write_text(sys.stdin.read(), encoding='utf-8')\n"
        "print(" + repr(_valid_architect_artifact()) + ")\n",
        encoding="utf-8",
    )

    result = runner.run_delegation(
        mode="architect", plan_id="plan", task_id="t", workdir=str(workdir),
        command=f"python3 {script}", prompt_file=str(prompt), output_format="design",
        artifact_path=str(artifact), manifest_path=str(manifest), readonly=True, timeout=30,
    )

    sent_prompt = seen_prompt.read_text(encoding="utf-8")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert "# Architect Task Envelope" in prompt.read_text(encoding="utf-8")
    assert "# Claude Code Architect Delegation Prompt" in sent_prompt
    assert "[핵심 작업 맥락]" in sent_prompt
    assert "[인터페이스 계약]" in sent_prompt
    assert "[엣지 케이스]" in sent_prompt
    assert "`## API / DTO 계약`" in sent_prompt
    assert data["prompt_file"] == ".soul/prompts/architect_envelope.md"
    assert data["prompt_composition"] == "architect_runner"
    assert data["composed_prompt_file"].endswith("architect_envelope_composed.md")
    assert "[PM 작업 지시 envelope]" not in sent_prompt
    assert "[핵심 작업 맥락]" in sent_prompt
    assert "[역할 경계]" not in sent_prompt
    assert len(sent_prompt) < 2600


def test_runner_records_failed_command_without_successful_status(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    prompt = workdir / "prompt.md"
    prompt.write_text("fail", encoding="utf-8")
    artifact = workdir / "artifact.md"
    manifest = workdir / "manifest.json"

    result = runner.run_delegation(
        mode="implementer",
        plan_id="plan_2",
        task_id="t_def",
        workdir=str(workdir),
        command="python3 -c 'import sys; print(\"bad\", file=sys.stderr); sys.exit(7)'",
        prompt_file=str(prompt),
        output_format="implementation",
        artifact_path=str(artifact),
        manifest_path=str(manifest),
        readonly=False,
        timeout=30,
    )

    assert result["ok"] is False
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["exit_code"] == 7
    assert Path(data["stderr_path"]).read_text(encoding="utf-8").strip() == "bad"


def test_runner_writes_phase_debug_log(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    prompt = workdir / "prompt.md"
    prompt.write_text("hello", encoding="utf-8")
    artifact = workdir / "artifact.md"
    manifest = workdir / "manifest.json"

    result = runner.run_delegation(
        mode="implementer",
        plan_id="plan_3",
        task_id="t_xyz",
        workdir=str(workdir),
        command="python3 -c 'import sys; print(sys.stdin.read())'",
        prompt_file=str(prompt),
        output_format="implementation",
        artifact_path=str(artifact),
        manifest_path=str(manifest),
        readonly=False,
        timeout=30,
    )

    assert result["ok"] is True
    log_text = manifest.with_suffix(".runner.log").read_text(encoding="utf-8")
    assert "run_delegation begin" in log_text
    assert "command start" in log_text
    assert "command finished rc=0" in log_text
    assert "manifest write status=completed exit_code=0" in log_text


def test_runner_timeout_writes_manifest_and_debug_log(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    prompt = workdir / "prompt.md"
    prompt.write_text("hello", encoding="utf-8")
    artifact = workdir / "artifact.md"
    manifest = workdir / "manifest.json"

    result = runner.run_delegation(
        mode="implementer",
        plan_id="plan_timeout",
        task_id="t_timeout",
        workdir=str(workdir),
        command="python3 -c 'import time; time.sleep(5)'",
        prompt_file=str(prompt),
        output_format="implementation",
        artifact_path=str(artifact),
        manifest_path=str(manifest),
        readonly=False,
        timeout=1,
    )

    assert result["ok"] is False
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["status"] == "timeout"
    assert data["exit_code"] == 124
    assert data["timeout_seconds"] == 1
    assert Path(data["stdout_path"]).is_file()
    assert Path(data["stderr_path"]).is_file()
    log_text = manifest.with_suffix(".runner.log").read_text(encoding="utf-8")
    assert "command timed out timeout=1" in log_text
    assert "manifest write status=timeout exit_code=124" in log_text


def _write_counting_repair_script(workdir: Path, outputs: list[str]) -> Path:
    script = workdir / "counting_runner.py"
    state = workdir / "runner_count.txt"
    script.write_text(
        "from pathlib import Path\n"
        f"state = Path({str(state)!r})\n"
        "count = int(state.read_text()) if state.exists() else 0\n"
        "state.write_text(str(count + 1))\n"
        f"outputs = {outputs!r}\n"
        "print(outputs[count] if count < len(outputs) else outputs[-1])\n",
        encoding="utf-8",
    )
    return script


def _architect_prompt(workdir: Path) -> Path:
    prompt = workdir / ".soul" / "prompts" / "architect.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text(
        "설계하라. 필수 heading: "
        "`## 상태`, `## 설계 요약`, `## 목표 / 범위`, `## 현황 분석`, `## API / DTO 계약`, "
        "`## 검토한 대안`, `## 선택한 설계`, `## 영향 범위`, `## 구현 작업분해`, `## 검증 계획`, "
        "`## 리스크와 완화 방안`, `## 사용자 확인 필요사항`, `## 구현 승인 전제`",
        encoding="utf-8",
    )
    return prompt


def test_architect_artifact_repair_succeeds_on_first_retry(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    prompt = _architect_prompt(workdir)
    artifact = workdir / ".soul" / "artifacts" / "design" / "plan_design.md"
    manifest = workdir / ".soul" / "artifacts" / "claude" / "plan_t_architect_manifest.json"
    script = _write_counting_repair_script(workdir, [
        "설계 산출물을 작성 완료했습니다.\n\n핵심 요약만 있습니다.",
        _valid_architect_artifact(),
    ])

    result = runner.run_delegation(
        mode="architect", plan_id="plan", task_id="t", workdir=str(workdir),
        command=f"python3 {script}", prompt_file=str(prompt), output_format="design",
        artifact_path=str(artifact), manifest_path=str(manifest), readonly=True, timeout=30,
    )

    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert artifact.read_text(encoding="utf-8").startswith("## 상태")
    assert data["artifact_repair"]["ok"] is True
    assert len(data["artifact_repair"]["attempts"]) == 1
    assert (artifact.parent / "plan_design.failed0.md").is_file()


def test_architect_artifact_repair_succeeds_on_second_retry(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    prompt = _architect_prompt(workdir)
    artifact = workdir / ".soul" / "artifacts" / "design" / "plan_design.md"
    manifest = workdir / ".soul" / "artifacts" / "claude" / "plan_t_architect_manifest.json"
    script = _write_counting_repair_script(workdir, [
        "작성 완료했습니다.",
        "## 상태\n본문만 있음",
        _valid_architect_artifact(),
    ])

    result = runner.run_delegation(
        mode="architect", plan_id="plan", task_id="t", workdir=str(workdir),
        command=f"python3 {script}", prompt_file=str(prompt), output_format="design",
        artifact_path=str(artifact), manifest_path=str(manifest), readonly=True, timeout=30,
    )

    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert len(data["artifact_repair"]["attempts"]) == 2
    assert artifact.read_text(encoding="utf-8").startswith("## 상태")


def test_architect_artifact_repair_fails_after_two_attempts(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    prompt = _architect_prompt(workdir)
    artifact = workdir / ".soul" / "artifacts" / "design" / "plan_design.md"
    manifest = workdir / ".soul" / "artifacts" / "claude" / "plan_t_architect_manifest.json"
    script = _write_counting_repair_script(workdir, [
        "작성 완료했습니다.",
        "여전히 요약뿐입니다.",
        "또 실패입니다.",
    ])

    result = runner.run_delegation(
        mode="architect", plan_id="plan", task_id="t", workdir=str(workdir),
        command=f"python3 {script}", prompt_file=str(prompt), output_format="design",
        artifact_path=str(artifact), manifest_path=str(manifest), readonly=True, timeout=30,
    )

    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert data["status"] == "artifact_contract_failed"
    assert data["exit_code"] == 2
    assert data["artifact_repair"]["ok"] is False
    assert len(data["artifact_repair"]["attempts"]) == 2
    repair1 = (workdir / ".soul" / "prompts" / "repair" / "plan_t_architect_repair1.md").read_text(encoding="utf-8")
    repair2 = (workdir / ".soul" / "prompts" / "repair" / "plan_t_architect_repair2.md").read_text(encoding="utf-8")
    assert "## 원래 작업 prompt" not in repair1
    assert "## 실패한 이전 artifact" not in repair1
    assert len(repair2) < 5000


def test_architect_artifact_repair_timeout_writes_manifests(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    prompt = _architect_prompt(workdir)
    artifact = workdir / ".soul" / "artifacts" / "design" / "plan_design.md"
    manifest = workdir / ".soul" / "artifacts" / "claude" / "plan_t_architect_manifest.json"
    script = workdir / "invalid_then_sleep.py"
    state = workdir / "runner_count.txt"
    script.write_text(
        "from pathlib import Path\n"
        "import time\n"
        f"state = Path({str(state)!r})\n"
        "count = int(state.read_text()) if state.exists() else 0\n"
        "state.write_text(str(count + 1))\n"
        "if count == 0:\n"
        "    print('작성 완료했습니다.')\n"
        "else:\n"
        "    time.sleep(5)\n",
        encoding="utf-8",
    )

    result = runner.run_delegation(
        mode="architect", plan_id="plan", task_id="t", workdir=str(workdir),
        command=f"python3 {script}", prompt_file=str(prompt), output_format="design",
        artifact_path=str(artifact), manifest_path=str(manifest), readonly=True, timeout=1,
    )

    data = json.loads(manifest.read_text(encoding="utf-8"))
    repair_manifest = workdir / ".soul" / "artifacts" / "claude" / "plan_t_architect_manifest_repair1.json"
    repair_data = json.loads(repair_manifest.read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert data["status"] == "artifact_contract_failed"
    assert data["artifact_repair"]["attempts"][0]["status"] == "timeout"
    assert data["artifact_repair"]["attempts"][0]["exit_code"] == 124
    assert repair_data["status"] == "timeout"
    assert repair_data["exit_code"] == 124
    assert repair_data["timeout_seconds"] == 1
