# Mesh Fleet Starter

A starter kit for running a **fleet of Claude Code agents against a Mesh instance**.
It contains the two drivers we run in production — a **feeder** and a **dispatcher** —
the supporting helpers, example agent instruction sets, and config templates. Point
it at your own Mesh instance (e.g. a self-hosted one), swap in your own agents, and
you have an autonomous fleet that picks up Mesh tasks and works them.

This is extracted from a running production fleet. Identities, hostnames, and
credentials have been replaced with placeholders; the operating rules and code are
the real thing.

> **Mesh** is a task-management platform for human + AI teams (Kanban, agents,
> per-agent API keys, memory `recall`/`remember`, an SSE event stream). This kit
> assumes you have a Mesh instance and can mint per-agent API keys on it.

## The idea in one picture

```
             ┌── Mesh instance (tasks, memory, SSE events) ──┐
             │                                               │
   feeds tasks │                                             │ spawns on events
             ▼                                               ▼
   ┌───────────────────┐                         ┌───────────────────────┐
   │  fiddler (feeder) │                         │ mesh-dispatcher (SSE) │
   │  persistent TUI   │                         │  spawns claude -p     │
   │  sessions, $0 on  │                         │  per task/mention     │
   │  a Max plan       │                         │                       │
   └─────────┬─────────┘                         └───────────┬───────────┘
             │ pastes task into                              │ starts a fresh
             ▼ a live tmux `claude` session                  ▼ Claude Code session
   ┌─────────────────────────────────────────────────────────────────────┐
   │  Agent workspaces (one per agent): CLAUDE.md + .mcp.json             │
   │  each agent talks back to Mesh via the Mesh MCP (tasks + memory)     │
   └─────────────────────────────────────────────────────────────────────┘
```

## Two drivers — pick one (or run both)

| | **fiddler** (feeder) | **mesh-dispatcher** (SSE) |
|---|---|---|
| How it runs an agent | pastes the task into a **persistent** `tmux` `claude` TUI session | **spawns** a fresh `claude -p` session per task/mention |
| Cost model | rides a Max **subscription** ($0 per task) | metered API usage |
| Wakes on | polling `todo` tasks + an SSE nudge | SSE events (`task.assigned`, `task.mentioned`, …) |
| Best for | a steady fleet on a subscription | event-driven spawning, `@mention` wakeups |
| File | `drivers/fiddler.py` | `drivers/mesh-dispatcher.py` |

**Start with `fiddler`.** It is fully documented here, self-contained, and the
recommended entry point. The dispatcher is our full production SSE driver, included
as a complete reference — it is larger and more coupled to our specific fleet, and it
retains some inline Russian comments and Russian-language detector phrases from our
source (harmless to an English fleet — they simply won't match). `fiddler` is fully
English.

## What's in here

```
drivers/            the two drivers + helpers
  fiddler.py            the feeder daemon (start here)
  fiddler_prompt.py     the task-prompt / ACP builder it pastes
  fiddler_pane.py       tmux pane read/write helpers
  fiddler-lane-respawn.sh   (re)start one agent's TUI session
  mesh-dispatcher.py    the SSE dispatcher (advanced / reference)
  mcp_registry.py       renders each agent's .mcp.json from the registry
  mcp-wrap              stderr-capturing wrapper for stdio MCP servers
config/             templates — copy, fill placeholders, keep secrets out of git
  fiddler.example.json
  mesh-agents.example.json
  mcp-registry.example.yaml
  keys.env.example
  launchd/            macOS launchd plists for the daemons
agents/             example agents (rename + adapt to your team)
  _shared/            the shared instruction set every agent imports
    CLAUDE-workflow.md        task/deploy/verify/merge discipline (the core approaches)
    CLAUDE-communication.md   how agents talk to the operator and each other
    CLAUDE-task-workflow.md   the short common preamble
    CLAUDE-model-selection.md when to use which model
    CLAUDE-memory.md          the memory protocol (recall/remember)
  orchestrator/CLAUDE.md   an orchestrator/coordinator agent
  mesh-lead/CLAUDE.md      a product lead (owns a product, reviews PRs)
  mesh-dev/CLAUDE.md       a developer under a lead
  sites-dev/CLAUDE.md      a web/sites developer
docs/
  architecture.md     how the pieces fit
  setup.md            step-by-step: stand up your own fleet
```

## Requirements

- **macOS** with `tmux` (the feeder drives persistent `tmux` Claude Code TUI sessions).
- **Claude Code** CLI, logged into a plan (the feeder assumes a Max subscription; the dispatcher can use metered API).
- **Python 3.11+**, and `uv` if you use MCP servers that need it.
- A **Mesh instance** and the ability to mint **per-agent API keys** on it, plus the **Mesh MCP** binary (from your Mesh build) that agents use to read/write tasks and memory.

## Quickstart (feeder path)

1. `cp config/fiddler.example.json ~/.config/fiddler/fiddler.json` and edit: your `mesh_api_url`, and one entry per agent (name, `tmux_session`, `mesh_agent_key`, `workspace`).
2. For each agent, create its workspace dir and drop in a `CLAUDE.md` (adapt one from `agents/`) and a `.mcp.json` (rendered by `mcp_registry.py` from `config/mcp-registry.example.yaml`, or hand-written).
3. Start each agent's TUI session once: `drivers/fiddler-lane-respawn.sh <tmux_session> sonnet`.
4. Run the feeder: `python3 drivers/fiddler.py run` (or install `config/launchd/com.example.fiddler.plist`).
5. Move a task to `todo` assigned to an agent → within ~30–60s the feeder pastes it into that agent's session and it starts working.

Full walkthrough in **`docs/setup.md`**.

## Read the operating rules

The real value beyond the plumbing is `agents/_shared/CLAUDE-*.md` — the accumulated
operating discipline every agent inherits: sync-before-work, migration-before-code,
agent-runnable acceptance criteria, "HTTP 200 is not proof", self-verify-with-rollback,
`review` is the hand-off, how `@mentions` wake (and why assigning a task is what
actually wakes an agent), the memory protocol, and more. Each rule came from an
incident. Adapt them; don't start from a blank page.

## Security

- **Never commit `keys.env` or any real credential.** `.gitignore` blocks the obvious paths; still, review every diff.
- Agent keys, server creds, and identities in this kit are **placeholders** (`agk_REPLACE_...`, `/Users/fleet`, `example.com`, `your-github-bot`). Replace them.
- Each agent session runs with `--dangerously-skip-permissions` inside its own workspace; scope what each agent can reach accordingly.
