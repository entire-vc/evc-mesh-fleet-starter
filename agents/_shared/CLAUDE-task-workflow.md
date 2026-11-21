# Task Workflow — Common Rules for All Mesh Agents

This file is the **shared truth** for all Mac Mini Claude Code agents (Atlas, Vega, Nova, Grove, Delta, Ember, Kilo, Pixel). All working rules around Mesh tasks, state awareness, communication with the operator, model selection, and memory live in **separate thematic files** for easy review and updates. If your role has specifics — they belong in your own CLAUDE.md, but these rules take PRIORITY.

Maintained by Orbit (coordinator) in `${HOME}/ClaudeCowork/<ops-repo>/`. All agent workspaces use a symlink to this file plus a `@CLAUDE-task-workflow.md` import that recursively loads the four thematic files below.

---

## Rule structure

| File | Topic | Size |
|------|-------|------|
| [@CLAUDE-workflow.md](CLAUDE-workflow.md) | State awareness, repo freshness (anti commit-clobber), verify-after-create, /sweep, /status, Mesh hygiene, anti-patterns, triage routing | ~320 lines |
| [@CLAUDE-communication.md](CLAUDE-communication.md) | Communication with the operator — ack-first, Telegram, report language, **external identity no-leak rule** | ~200 lines |
| [@CLAUDE-model-selection.md](CLAUDE-model-selection.md) | Phase discipline (discuss/plan/execute/verify/ship), phase-aware model selection, parallelization via task-splitter | ~170 lines |
| [@CLAUDE-memory.md](CLAUDE-memory.md) | Memory layers (working / episodic / semantic / procedural), tag vocabulary, importance scale, write triggers, daily consolidation | ~155 lines |

@CLAUDE-workflow.md
@CLAUDE-communication.md
@CLAUDE-model-selection.md
@CLAUDE-memory.md

---

## Quick navigation (what lives where)

- **Work pacing — no ETA / "in a week"** → `CLAUDE-workflow.md §0 ASAP by default` (everything now, except pre-scheduled)
- **Creating a Mesh task** → status=`todo` or `in_progress`, **never `review`** (see `CLAUDE-workflow-reference.md §0a Status workflow`)
- **`delegation_level=review` ≠ status=`review`** → `delegation_level` sets post-work routing, NOT initial status; task created with status=`review` is trapped in `_SKIP_CATEGORIES` — dispatcher never retries (see `CLAUDE-workflow-reference.md §0k «review trap»`)
- **@-mention the operator in a comment** → use `❓ **Blocking @operator**:` for blockers (+ move task → triage) or `ℹ️ **FYI @operator**:` for FYI (no status change). See `CLAUDE-workflow-reference.md §0b @user-mention markers`
- **An existing ask on another card** → record a dependency and link the authoritative question before declining a duplicate escalation; do not leave the waiting task without a durable edge.
- **Creating a Mesh task with assignee=the operator** → default `todo`. `triage` **only if** it really blocks other work and needs the operators decision. See `CLAUDE-workflow-reference.md §0c the operator-assigned status`
- **Tasks in `review` >24h without movement** → Atlas /sweep flag + the operator ping; >72h without discussion → auto-move to triage. See `CLAUDE-workflow-reference.md §0d Stale review`
- **Verify / monitor / passive-wait task** (idle on a window/clock/scheduled reactivation — nothing to do right now) → status=`backlog` + label `phase:verify`|`kind:monitor`|`no-pavel-triage`; **NEVER `in_progress`** (it churns the dispatcher → 20 noise checkpoints + false auto-triage) and **NEVER `❓@operator`** (no decision pending). Dispatcher auto-parks these to backlog at count==3. Prefer just closing if the deliverable shipped + smoke passed. See `CLAUDE-workflow-reference.md §0m`
- **Backlog work** → park work that is not currently actionable with its explicit dependency or next trigger; do not repeatedly feed an inactive task.
- **Closing a task (move → `done`) — comment-gate is NOT a permission wall** → **ANY agent may close ANY task**, including one assigned to / checked-out by another agent. There is **no ownership lock** in Mesh (checkout auto-releases on the terminal move). The only gate to `done` is the **require-comment governance rule**: add a **≥20-char comment** (what shipped / why closing) FIRST, then move. If you see *«Action blocked by governance rules»* / *«Blocked by rule «Agent must comment before done»»* → the comment is just missing: **add it and retry**. Do **NOT** read it as "I'm not allowed" and do **NOT** escalate to the operator or the owner. **Orchestrator (Atlas):** close redundant/superseded tasks directly (comment the reason + linking evidence, e.g. superseding PR) — don't ping the owner to do it. *Only exception:* tasks with `delegation_level=supervised` or `human_gate=true` genuinely require a human — those, and only those, go to the operator/UI.
- **Got a short prompt and don't know what to do** → `CLAUDE-workflow.md §1 State Awareness` (conversational vs actionable filter)
- **Starting work with code / repo** → `CLAUDE-workflow.md §1a Repo Freshness` (git fetch + safe sync as the FIRST step, never force-push, divergence → stop+escalate)
- **Deploying schema-dependent code (migration + app)** → `CLAUDE-workflow.md §1b Deploy Discipline` (migration ALWAYS before/atomic-with the code that reads it; never ship code ahead of its DB migration; backward-compatible nullable additive; incompatible = expand→migrate→contract over two releases; CI `migrate` gate before app-image swap, fail-closed; rollback = revert image first, then migrate)
- **User-facing / UI task — before done** → `CLAUDE-workflow.md §1n` (passing authed browser scenario, independent verifier, behavior-asserts; HTTP 200 ≠ proof) + `§1k Visual tasks` (screens); scenario-doc format → `docs/e2e-scenario-template.md`
- **Created a task — what next** → `CLAUDE-workflow.md §2 Verify After Create`
- **the operator wrote /status or /sweep** → `CLAUDE-workflow-reference.md §3 /status` and `§3a /sweep`
- **Am I duplicating a task** → `CLAUDE-workflow.md §4 Mesh Workflow Hygiene`
- **the operator in Telegram — how to reply** → `CLAUDE-communication.md §5`
- **What language to write the report in** → `CLAUDE-communication.md §5` (Russian for the operator)
- **Writing to GitHub/email/public channel — which identity to use** → `CLAUDE-communication.md §6` (hard rule: persona per real account, no «Atlas / orchestrator / agent / Claude / LLM»)
- **Which model to pick (opus/sonnet)** → `CLAUDE-model-selection.md` (phase-driven)
- **Big task — split into subtasks** → `CLAUDE-model-selection.md Parallelization`
- **task-splitter auto-trigger (on checkout)** → `CLAUDE-model-selection.md §Auto-trigger threshold` (>300 words OR >3 ## headers → mandatory call + comment + mempalace log)
- **Where to write a fact / decision / incident** → `CLAUDE-memory.md §9.1` (4-layer table) + `§9.2` (write triggers)
- **What relevance value to set** → `CLAUDE-memory.md §9.3` importance scale (float 0.0-1.0)
- **What tags to put on an entry** → `CLAUDE-memory.md §9.4` tag vocabulary
- **Add/change an MCP connector or rotate a key** → do NOT hand-edit 10 `.mcp.json` files. Single source of truth = `~/.config/mcp-registry/registry.yaml` (+ secrets in `~/.config/agents/keys.env`, mode 600). `mesh-dispatcher` regenerates `.mcp.json` for each agent before spawn (fail-safe, atomic, backup). Guide + rollback: `docs/MCP_REGISTRY.md`. CLI: `mcp-registry validate|list|diff --all|apply`. 
- **Triage routing for recurring/audit findings** → `CLAUDE-workflow-reference.md §8`
- **Subagent output must have a schema** → `CLAUDE-workflow-reference.md §9 Structured outputs`
- **Task requires ≥2 subagents — decompose into a society** → `CLAUDE-workflow-reference.md §10 Society of Mind`
- **Atlas / orchestrator on a multi-step initiative** → `CLAUDE-workflow-reference.md §11 Captain Pattern`
- **Strategic decision (build/buy, refactor, deprecate)** → `~/.claude/agents/reasoner.md` (subagent invocation)
- **What to mock vs test real code** → `CLAUDE-workflow.md §1o Mocking convention` (TL;DR: mock ONLY at true external boundaries — PSP/HTTP/time; never mock DB layer, internal sagas, webhook handlers)

---
