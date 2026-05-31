# Labs PM/Kanban operating environment

This document describes the local operating environment for the `JaeCheon8587/hermes-agent-labs` branch. The goal is not to preserve the sample `HermesTest` project; that project is a disposable smoke-test workspace. The goal is to make the Hermes PM/Kanban runtime reproducible on another WSL machine.

## Scope

Managed as code in this repository:

- PM workflow hardening code and tests.
- Claude runner / artifact gate behavior.
- Labs installer wrapper.
- PM/Kanban runtime helper scripts used by cron jobs.
- Operator bootstrap instructions.

Runtime state that must stay outside git:

- `~/.hermes/profiles/*/state/`
- `~/.hermes/kanban/`
- `~/.hermes/state/`
- `~/.hermes/auth.json` and profile-local auth files.
- Slack tokens, API keys, and `.env` files.
- Disposable project workspaces such as `/mnt/c/Users/cross/OneDrive/Desktop/HermesTest`.

## Install the labs branch

```bash
curl -fsSL https://raw.githubusercontent.com/JaeCheon8587/hermes-agent-labs/local/pm-workflow-hardening/scripts/install-labs.sh | bash
```

Skip the normal setup wizard when re-installing onto an already configured machine:

```bash
curl -fsSL https://raw.githubusercontent.com/JaeCheon8587/hermes-agent-labs/local/pm-workflow-hardening/scripts/install-labs.sh | bash -s -- --skip-setup
```

If the machine does not have GitHub SSH configured yet, use HTTPS for the checkout:

```bash
HERMES_LABS_REPO=https://github.com/JaeCheon8587/hermes-agent-labs.git \
  curl -fsSL https://raw.githubusercontent.com/JaeCheon8587/hermes-agent-labs/local/pm-workflow-hardening/scripts/install-labs.sh | bash
```

`install-labs.sh` clones or updates `local/pm-workflow-hardening`, delegates to the standard Hermes installer, then syncs PM/Kanban runtime helper scripts into `~/.hermes/scripts/`.

To opt out of runtime script sync:

```bash
HERMES_LABS_SYNC_PM_RUNTIME=0 \
  curl -fsSL https://raw.githubusercontent.com/JaeCheon8587/hermes-agent-labs/local/pm-workflow-hardening/scripts/install-labs.sh | bash
```

## Runtime helper scripts

Canonical copies live in:

```text
scripts/pm-workflow/
```

Installed runtime copies live in:

```text
~/.hermes/scripts/
```

Current scripts:

- `pm_design_ready_notifier.py` — emits Slack design-approval requests once design artifacts are ready.
- `pm_kanban_completion_notifier.py` — emits Slack final-completion summaries for PM final tasks.
- `pm_kanban_blocked_notifier.py` — emits Slack alerts when PM workflow tasks become blocked.
- `pm_scope_phase_sync.py` — syncs `.soul/approved_scope.json` between implementer, reviewer, and final phases.

Manual sync command:

```bash
cd ~/.hermes/hermes-agent
scripts/sync-pm-workflow-runtime.sh
```

Optional overrides:

```bash
HERMES_ROOT=$HOME/.hermes scripts/sync-pm-workflow-runtime.sh
HERMES_RUNTIME_SCRIPTS_DIR=$HOME/.hermes/scripts scripts/sync-pm-workflow-runtime.sh
```

## Cron jobs

The runtime scripts are designed for Hermes cron jobs with `no_agent=true` and silent-on-empty stdout.

Recommended jobs:

```text
pm-design-ready-notifier       every 1m  script=pm_design_ready_notifier.py       no_agent=true  deliver=slack:<PM channel>
pm-kanban-completion-notifier  every 1m  script=pm_kanban_completion_notifier.py  no_agent=true  deliver=slack:<PM channel>
pm-kanban-blocked-notifier     every 1m  script=pm_kanban_blocked_notifier.py     no_agent=true  deliver=slack:<PM channel>
pm-scope-phase-sync            every 1m  script=pm_scope_phase_sync.py            no_agent=true  deliver=slack:<PM channel>
```

Create jobs with the Hermes CLI or the `cronjob` tool. Example CLI shape:

```bash
hermes cron create 'every 1m' \
  --name pm-design-ready-notifier \
  --script pm_design_ready_notifier.py \
  --no-agent \
  --deliver slack:<PM channel>
```

If the CLI flags differ in a future Hermes version, use `hermes cron create --help` and preserve the same semantics: every minute, script-only/no-agent, Slack delivery to the PM channel.

## Profile topology

Target topology:

```text
Slack user -> project_manager profile only
project_manager -> Kanban board / PM workflow tool
Kanban dispatcher -> backend-architect / backend-implementer / backend-reviewer internal worker profiles
workers -> artifacts / structured completion metadata
project_manager final task -> Korean final report back to Slack
```

Notes:

- `project_manager` should stay orchestration-only.
- The user should talk to Slack PM only.
- Worker profiles should run as spawned internal Kanban workers, not direct Slack bots.
- Code reading and implementation belong to worker profiles.
- PM claims must be verified against real `t_<hex>` Kanban task IDs, not prose alone.

## Profile and gateway bootstrap checklist

1. Install labs branch.
2. Configure default/profile credentials as needed with `hermes login` or `hermes -p <profile> login`.
3. Ensure required profiles exist:
   - `project_manager`
   - `backend-architect`
   - `backend-implementer`
   - `backend-reviewer`
4. Ensure `project_manager` has Slack tokens in its profile-local `.env`.
5. Ensure `project_manager` exposes PM workflow tools for Slack, not unrestricted raw terminal/code access.
6. Set or verify `terminal.cwd` for PM and worker profiles when a stable project workspace is needed.
7. Install and start the PM gateway as a systemd user service:

```bash
hermes -p project_manager gateway install
hermes -p project_manager gateway start
hermes -p project_manager gateway status
```

8. Enable linger once per Linux user if gateway services must survive logout:

```bash
sudo loginctl enable-linger "$USER"
```

9. Verify worker auth with quick profile probes:

```bash
hermes -p backend-architect chat -q '짧게 OK만 답해.' --quiet
hermes -p backend-implementer chat -q '짧게 OK만 답해.' --quiet
hermes -p backend-reviewer chat -q '짧게 OK만 답해.' --quiet
```

10. Sync PM runtime scripts:

```bash
cd ~/.hermes/hermes-agent
scripts/sync-pm-workflow-runtime.sh
```

11. Verify cron jobs exist and are enabled:

```bash
hermes cron list
```

12. Verify current Kanban board:

```bash
hermes kanban boards list
hermes kanban stats
```

## Live smoke-test pattern

Use a disposable project workspace, not the Hermes Agent repo itself.

Recommended smoke shape:

1. Ask Slack PM for a small design-only slice.
2. Verify a real architect `t_<hex>` task is created.
3. Wait for design artifact and design-ready Slack report.
4. Approve the design.
5. Verify a separate execution plan creates implementer, reviewer, and final tasks.
6. Verify implementer completes with implementation artifact.
7. Verify reviewer completes with `verdict=pass` or a clear blocked/concern reason.
8. Verify final PM task completes and Slack receives a Korean detailed final report.
9. Treat disposable workspace git state as irrelevant unless the smoke fixture itself is intentionally being preserved.

## Update workflow

Keep upstream and labs remotes separate:

```text
origin = https://github.com/NousResearch/hermes-agent.git
mine   = git@github.com:JaeCheon8587/hermes-agent-labs.git
```

Update labs branch:

```bash
cd ~/.hermes/hermes-agent
git fetch origin
git checkout main
git pull --ff-only origin main
git checkout local/pm-workflow-hardening
git rebase main
scripts/sync-pm-workflow-runtime.sh
hermes -p project_manager gateway restart
```

After a rebase, push with:

```bash
git push --force-with-lease mine local/pm-workflow-hardening
```

## What not to commit

Do not commit:

- `~/.hermes/profiles/project_manager/state/pm_plans.json`
- Kanban SQLite DBs.
- Cron state files.
- Slack/API credentials.
- `.soul/` artifacts from arbitrary disposable projects unless explicitly creating a fixture.
- `bin/`, `obj/`, `.vs/`, and other build/IDE outputs.

Commit only reusable operating-environment code, tests, scripts, and docs.
