# Architecture

## The loop

A **Mesh instance** holds the work (tasks on a Kanban board) and the shared memory.
A **driver** on your machine watches Mesh and gets tasks in front of **agents**
(Claude Code sessions). Each agent works the task and talks back to Mesh — moving the
card, commenting, and reading/writing memory — through the **Mesh MCP**.

```
Mesh (tasks + memory + SSE)  →  driver  →  agent session (Claude Code)  →  Mesh MCP  →  Mesh
```

Everything else in this kit exists to make that loop reliable.

## Feeder vs dispatcher

There are two ways to get a task in front of an agent.

### Feeder (`fiddler.py`) — recommended

Each agent has a **persistent `tmux` session** running the Claude Code TUI, logged
into a subscription. The feeder is a daemon that:

1. Polls each agent's `todo` tasks (and listens on Mesh's SSE stream as a nudge).
2. When an agent has a task and its session is **idle**, it **pastes** a task prompt
   into that agent's tmux pane (see `fiddler_prompt.py` for the prompt it builds).
3. Watches the pane to know when the turn is done, detects completion via Mesh
   `status_category` (`review`/`done`/`cancelled`), and feeds the next task.
4. Handles the messy parts: nudging a stalled turn, timing out a wedged one,
   recycling context between unrelated tasks, resuming an idle in-progress task,
   and recovering a session stuck on re-authentication.

Because the sessions ride a Max **subscription**, running the fleet costs nothing per
task. The trade-off is that a session has a **fixed model** chosen at start (the
feeder can't pick a model per task — see `CLAUDE-model-selection.md`).

### Dispatcher (`mesh-dispatcher.py`) — advanced

Instead of persistent sessions, the dispatcher **spawns a fresh `claude -p` session
per event** it reads off Mesh's SSE stream (`task.assigned`, `task.mentioned`, …). It
picks the model at spawn time, regenerates the agent's `.mcp.json` from the registry
before each spawn, and tears the session down when the turn ends. This uses **metered
API** rather than a subscription, but gives event-driven spawning and true
`@mention`-wakeups.

**You can run both**: most agents on the feeder, and one coordinator on the
dispatcher (so `@mention` handoffs to the coordinator wake it instantly). A key
gotcha falls out of this split and is documented in `CLAUDE-communication.md`:

> An `@mention` only **addresses** a message; it does not **wake** a feeder agent.
> To wake any agent, **assign it a task and move that task to `todo`.** Only a
> dispatcher-hosted agent is woken by a mention.

## Agents

An agent is a directory (its **workspace**) containing:

- **`CLAUDE.md`** — its identity + role, which `@import`s the shared instruction set in `agents/_shared/`. This is where behavior lives.
- **`.mcp.json`** — the MCP servers it can use (at minimum, the Mesh MCP). Rendered from the central registry by `mcp_registry.py` so one file is the source of truth for every agent's tools.

The example agents model a small team: an **orchestrator** (turns requests into
plans, routes work, verifies, never grinds the product work itself), a **product
lead** (owns a product, reviews the dev's PRs), a **dev** (implements under the lead),
and a **sites dev** (web). Rename and reshape them for your team.

## The shared instruction set (`agents/_shared/`)

Every agent imports these. They are the accumulated operating discipline — each rule
traceable to an incident it prevents:

- **`CLAUDE-workflow.md`** — the big one: sync-before-work, migration-before-code, agent-runnable acceptance criteria, deep-verify (content, not status code), self-verify-with-rollback (the autonomy gate), PR definition-of-done, `review` is the hand-off, merge-train discipline, mock only at true external boundaries, visual + behavioral verification of UI work, external-channel persona.
- **`CLAUDE-communication.md`** — acknowledge-first, how to put a blocking question so a human *and* the machine see it, the canonical `@mention` table, and how mentions actually wake (or don't).
- **`CLAUDE-model-selection.md`** — when a task deserves the expensive model.
- **`CLAUDE-memory.md`** — the `recall`-before-work / `remember`-after protocol, and what belongs in shared vs personal memory.
- **`CLAUDE-task-workflow.md`** — the short common preamble the others build on.

## Config and secrets

- **`fiddler.json`** / **`mesh-agents.json`** — which agents exist, their keys, workspaces, sessions.
- **`mcp-registry.yaml`** — the one place that defines every agent's MCP servers; `mcp_registry.py` renders each `.mcp.json` from it, resolving `${VAR}` secrets from `keys.env`.
- **`keys.env`** — secrets, mode `600`, never in git.

The drivers run under **launchd** (see `config/launchd/`) so they survive logout and restart.
