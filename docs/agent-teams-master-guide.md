# Agent Teams — Master Reference Guide

> A condensed, action-oriented reference for building effective Claude Code agent
> teams. Source: https://code.claude.com/docs/en/agent-teams
> Requires Claude Code **v2.1.32+** (`claude --version`). Experimental, off by default.

---

## 1. What agent teams are

Multiple Claude Code instances coordinating as a team:

- **Team lead** — the session that creates the team, spawns teammates, assigns work, and synthesizes results.
- **Teammates** — separate full Claude Code instances, each with its own context window, working independently.
- **Task list** — a shared list of work items teammates claim and complete (states: pending → in progress → completed; tasks can declare dependencies).
- **Mailbox** — messaging system; teammates message each other and the lead directly.

The differentiator vs. subagents: teammates **talk to each other and self-coordinate**, not just report back to a parent.

### Teams vs. subagents — pick the right tool

|                | Subagents                              | Agent teams                                  |
| -------------- | -------------------------------------- | -------------------------------------------- |
| Context        | Own window; result returns to caller   | Own window; fully independent                |
| Communication  | Reports back to main agent only        | Teammates message each other directly        |
| Coordination   | Main agent manages all work            | Shared task list, self-coordination          |
| Best for       | Focused tasks where only result matters| Work needing discussion & collaboration      |
| Token cost     | Lower (summarized back)                | Higher (each teammate is a full instance)    |

**Rule of thumb:** sequential work, same-file edits, or many dependencies → single session or subagents. Parallel, independent exploration with cross-talk → agent team.

---

## 2. Enable agent teams

Set `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in the environment or `settings.json`:

```json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

---

## 3. Strongest use cases

- **Research & review** — investigate different aspects in parallel, then share/challenge findings.
- **New modules/features** — each teammate owns a separate piece, no collisions.
- **Debugging competing hypotheses** — teammates test rival theories and converge faster.
- **Cross-layer coordination** — frontend/backend/tests, one owner each.

**Start here if new:** read-only tasks with clear boundaries (review a PR, research a library, investigate a bug). These show parallel value without parallel-write coordination risk.

---

## 4. Starting a team

Just describe the task and structure in natural language. The lead creates the team, spawns teammates, coordinates, and cleans up.

```
I'm designing a CLI tool that tracks TODO comments across a codebase.
Create an agent team to explore from different angles: one on UX, one on
technical architecture, one playing devil's advocate.
```

Claude won't create a team without your approval — either you request one, or Claude proposes and you confirm.

---

## 5. Controlling the team

### Display modes (`teammateMode` in `~/.claude/settings.json`)

- **`in-process`** (works in any terminal) — all teammates in your main terminal. `Shift+Down` cycles teammates (wraps back to lead); type to message; `Enter` to view a session, `Esc` to interrupt; `Ctrl+T` toggles task list.
- **`tmux`** / split panes — each teammate gets its own pane. Requires tmux or iTerm2 + `it2` CLI.
- **`auto`** (default) — split panes if already inside tmux, else in-process.

Force in-process for one session: `claude --teammate-mode in-process`

> Split panes are **not** supported in VS Code integrated terminal, Windows Terminal, or Ghostty. tmux works best on macOS; `tmux -CC` in iTerm2 is the suggested entrypoint.

### Specify count and model

```
Create a team with 4 teammates to refactor these modules in parallel. Use Sonnet for each.
```

Teammates don't inherit the lead's `/model`. Set **Default teammate model** in `/config` (pick "Default (leader's model)" to follow the lead).

### Require plan approval (for risky work)

```
Spawn an architect teammate to refactor auth. Require plan approval before any changes.
```

Teammate stays in read-only plan mode → submits plan → lead approves or rejects with feedback → on reject, revises and resubmits → on approve, implements. The lead decides autonomously; steer it with criteria in your prompt (e.g. "only approve plans with test coverage").

### Talk to teammates directly

Each teammate is a full session. In-process: `Shift+Down` then type. Split panes: click the pane.

### Assign / claim tasks

- **Lead assigns**: tell the lead which task → which teammate.
- **Self-claim**: a teammate picks the next unassigned, unblocked task when done.
- File locking prevents claim races. Dependencies auto-unblock when prerequisites complete.

### Shut down & clean up

```
Ask the researcher teammate to shut down
```
Teammate can approve (exit gracefully) or reject with reason. Shutdown can be slow — teammates finish the current request/tool call first.

```
Clean up the team
```
**Always clean up via the lead** (never a teammate — their team context may not resolve). Cleanup fails if any teammate is still running, so shut them down first.

---

## 6. Quality gates via hooks

Enforce rules with [hooks](https://code.claude.com/docs/en/hooks) — exit code **2** sends feedback and blocks:

- **`TeammateIdle`** — runs before a teammate goes idle; exit 2 to keep it working.
- **`TaskCreated`** — runs as a task is created; exit 2 to prevent and give feedback.
- **`TaskCompleted`** — runs as a task is marked complete; exit 2 to prevent and give feedback.

---

## 7. Reusable roles via subagent definitions

Reference a [subagent](https://code.claude.com/docs/en/sub-agents) type (project/user/plugin/CLI scope) when spawning:

```
Spawn a teammate using the security-reviewer agent type to audit the auth module.
```

- Honors the definition's `tools` allowlist and `model`.
- The definition body is **appended** to the teammate's system prompt (doesn't replace it).
- `SendMessage` and task-management tools are **always** available even if `tools` restricts others.
- ⚠️ `skills` and `mcpServers` frontmatter are **not** applied to teammates — they load skills/MCP from project + user settings like a normal session.

---

## 8. Architecture & storage

- **Team config**: `~/.claude/teams/{team-name}/config.json` — runtime state (session IDs, tmux pane IDs, `members` array with name/agent ID/agent type). **Auto-generated; don't hand-edit or pre-author** — overwritten on next state update.
- **Task list**: `~/.claude/tasks/{team-name}/`
- No project-level team config exists; a `.claude/teams/teams.json` in the repo is treated as an ordinary file, not config.

### Context & communication

- Each teammate loads project context fresh: CLAUDE.md, MCP servers, skills + the spawn prompt. **The lead's conversation history does NOT carry over.**
- Messages deliver automatically (no polling). Idle teammates auto-notify the lead.
- Messaging is per-recipient by name — to reach everyone, send one message each. Tell the lead what to name teammates for predictable references.

### Permissions

All teammates start with the **lead's** permission mode (including `--dangerously-skip-permissions`). You can change individual modes after spawning, but **not per-teammate at spawn time**. Pre-approve common ops in permission settings to cut prompt friction.

---

## 9. Best practices

- **Give enough context** — teammates don't inherit chat history; put task-specifics in the spawn prompt (paths, constraints, tech details, expected output format + severity ratings, etc.).
- **Team size: start with 3–5.** Token cost scales linearly; coordination overhead and diminishing returns kick in beyond that. Three focused teammates beat five scattered ones.
- **Tasks per teammate: ~5–6.** Keeps everyone busy, lets the lead reassign if someone stalls. (15 independent tasks → ~3 teammates.)
- **Size tasks as self-contained deliverables** — a function, a test file, a review. Too small = overhead > benefit; too large = long runs without check-ins.
- **Avoid file conflicts** — give each teammate a distinct set of files; two editing the same file overwrite each other.
- **Make them adversarial for debugging** — have teammates challenge each other's theories to beat anchoring bias.
- **Wait for teammates** — if the lead starts doing the work itself: `Wait for your teammates to complete their tasks before proceeding`.
- **Monitor and steer** — check progress, redirect dead ends, synthesize as findings arrive. Don't let a team run unattended too long.

---

## 10. Token cost awareness

Each teammate is a separate context window; usage scales with active teammate count. Worth it for research/review/new features; wasteful for routine sequential work. See https://code.claude.com/docs/en/costs#agent-team-token-costs

---

## 11. Troubleshooting

| Symptom | Fix |
| --- | --- |
| Teammates not appearing | In-process: `Shift+Down` to cycle (they may be running, just not visible). Confirm the task was complex enough. Split panes: `which tmux`; for iTerm2 verify `it2` + Python API. |
| Too many permission prompts | Pre-approve common operations in permission settings before spawning. |
| Teammate stopped on error | View its output (`Shift+Down` / click pane), give instructions, or spawn a replacement. |
| Lead quits before work done | Tell it to keep going / wait for teammates instead of doing the work itself. |
| Orphaned tmux session | `tmux ls` then `tmux kill-session -t <name>`. |
| Task stuck/blocked | Teammate may have failed to mark complete — verify and update status manually or nudge the lead. |

---

## 12. Limitations (experimental)

- **No session resumption for in-process teammates** — `/resume` and `/rewind` don't restore them; lead may message ghosts. Tell it to spawn new ones.
- **Task status can lag** — completion sometimes not marked, blocking dependents.
- **Shutdown can be slow** — finishes current request/tool call first.
- **One team at a time** — clean up before creating another.
- **No nested teams** — only the lead manages the team; teammates can't spawn teams/teammates.
- **Lead is fixed** — creator is lead for the team's lifetime; no promotion/transfer.
- **Permissions set at spawn** — all start with lead's mode.
- **Split panes need tmux/iTerm2** — not in VS Code terminal, Windows Terminal, or Ghostty.

> `CLAUDE.md` works normally — teammates read it from their working directory. Use it to give all teammates shared project guidance.

---

## 13. Copy-paste prompt patterns

**Parallel code review:**
```
Create an agent team to review PR #142. Spawn three reviewers:
- One focused on security implications
- One checking performance impact
- One validating test coverage
Have them each review and report findings.
```

**Competing-hypothesis debugging:**
```
Users report the app exits after one message instead of staying connected.
Spawn 5 teammates to investigate different hypotheses. Have them talk to each
other to disprove each other's theories, like a scientific debate. Update the
findings doc with whatever consensus emerges.
```

**Well-scoped spawn with context:**
```
Spawn a security reviewer teammate with the prompt: "Review the authentication
module at src/auth/ for security vulnerabilities. Focus on token handling,
session management, and input validation. The app uses JWT tokens in httpOnly
cookies. Report issues with severity ratings."
```

---

## 14. Related

- Subagents (lightweight in-session delegation): https://code.claude.com/docs/en/sub-agents
- Git worktrees (manual parallel sessions): https://code.claude.com/docs/en/worktrees
- Hooks: https://code.claude.com/docs/en/hooks
- Settings: https://code.claude.com/docs/en/settings
