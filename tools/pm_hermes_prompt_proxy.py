"""Proxy stdin prompts into Hermes chat for PM delegation runners.

This lets the existing ``tools.pm_claude_delegation`` wrapper keep its
artifact/manifest contract while replacing the inner LLM command with a
Hermes/OpenAI-Codex backed command when Claude Code quota is exhausted.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Hermes profile chat from a stdin prompt")
    parser.add_argument("--profile", default="backend-specialist")
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--source", default="pm-runner")
    parser.add_argument("--toolsets", default="")
    parser.add_argument("--ignore-rules", action="store_true")
    parser.add_argument("--ignore-user-config", action="store_true")
    parser.add_argument("--yolo", action="store_true")
    return parser


def _strip_hermes_runtime_lines(text: str) -> str:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and lines[0].strip().startswith("session_id:"):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines) + ("\n" if lines and text.endswith("\n") else "")


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_hermes_command(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr) from exc
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout=stdout, stderr=stderr)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("pm_hermes_prompt_proxy: empty prompt on stdin", file=sys.stderr)
        return 2

    cmd = [
        "hermes",
        "-p",
        args.profile,
        "chat",
        "--quiet",
        "--source",
        args.source,
        "--max-turns",
        str(max(args.max_turns, 1)),
        "-q",
        prompt,
    ]
    if args.model:
        cmd.extend(["--model", args.model])
    if args.provider:
        cmd.extend(["--provider", args.provider])
    if args.toolsets:
        cmd.extend(["--toolsets", args.toolsets])
    if args.ignore_rules:
        cmd.append("--ignore-rules")
    if args.ignore_user_config:
        cmd.append("--ignore-user-config")
    if args.yolo:
        cmd.append("--yolo")

    try:
        completed = _run_hermes_command(cmd, max(args.timeout, 1))
    except subprocess.TimeoutExpired as exc:
        stdout = _strip_hermes_runtime_lines(_timeout_text(exc.output))
        stderr = _strip_hermes_runtime_lines(_timeout_text(exc.stderr))
        if stdout:
            print(stdout, end="")
        if stderr:
            print(stderr, end="", file=sys.stderr)
        print(f"pm_hermes_prompt_proxy: hermes chat timed out after {int(exc.timeout or args.timeout)}s", file=sys.stderr)
        return 124
    stdout = _strip_hermes_runtime_lines(completed.stdout or "")
    stderr = _strip_hermes_runtime_lines(completed.stderr or "")
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    return int(completed.returncode or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
