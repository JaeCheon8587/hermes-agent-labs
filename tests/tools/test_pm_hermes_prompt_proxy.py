import subprocess

from tools import pm_hermes_prompt_proxy as proxy


def test_proxy_reads_stdin_and_invokes_hermes_chat(monkeypatch, capsys):
    captured = {}

    def fake_run(argv, timeout):
        captured["argv"] = argv
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(argv, 0, stdout="session_id: abc123\n설계 문서\n", stderr="")

    monkeypatch.setattr(proxy, "_run_hermes_command", fake_run)
    monkeypatch.setattr(proxy.sys, "stdin", type("Stdin", (), {"read": lambda self: "prompt body"})())

    rc = proxy.main(["--profile", "backend-specialist", "--model", "gpt-5.5", "--provider", "openai-codex", "--max-turns", "4", "--toolsets", "file,terminal", "--ignore-rules"])

    assert rc == 0
    assert captured["argv"][:5] == ["hermes", "-p", "backend-specialist", "chat", "--quiet"]
    assert "-q" in captured["argv"]
    assert captured["argv"][captured["argv"].index("-q") + 1] == "prompt body"
    assert "--model" in captured["argv"]
    assert "gpt-5.5" in captured["argv"]
    assert "--provider" in captured["argv"]
    assert "openai-codex" in captured["argv"]
    assert "--toolsets" in captured["argv"]
    assert "file,terminal" in captured["argv"]
    assert "--ignore-rules" in captured["argv"]
    assert captured["timeout"] == 300
    assert capsys.readouterr().out == "설계 문서\n"


def test_proxy_rejects_empty_prompt(monkeypatch, capsys):
    monkeypatch.setattr(proxy.sys, "stdin", type("Stdin", (), {"read": lambda self: "  \n"})())

    rc = proxy.main([])

    captured = capsys.readouterr()
    assert rc == 2
    assert "empty prompt" in captured.err


def test_proxy_reports_timeout_without_traceback(monkeypatch, capsys):
    def fake_run(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout=7, output="session_id: abc\npartial", stderr="session_id: err\n")

    monkeypatch.setattr(proxy, "_run_hermes_command", fake_run)
    monkeypatch.setattr(proxy.sys, "stdin", type("Stdin", (), {"read": lambda self: "prompt body"})())

    rc = proxy.main(["--timeout", "7"])

    captured = capsys.readouterr()
    assert rc == 124
    assert "partial" in captured.out
    assert "timed out after 7s" in captured.err
    assert "Traceback" not in captured.err
    assert "session_id:" not in captured.out
    assert "session_id:" not in captured.err
