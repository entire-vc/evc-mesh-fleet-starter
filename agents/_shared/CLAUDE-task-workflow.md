# Task Workflow — Common Rules for All Mesh Agents

This file is the **shared truth** for all Mac Mini Claude Code agents (Atlas, Vega, Nova, Grove, Delta, Ember, Kilo, Pixel). All working rules around Mesh tasks, state awareness, communication with the operator, model selection, and memory live in **separate thematic files** for easy review and updates. If your role has specifics — they belong in your own CLAUDE.md, but these rules take PRIORITY.

Maintained by Orbit (coordinator) in `/Users/fleet/ClaudeCowork/bob/`. All agent workspaces use a symlink to this file plus a `@CLAUDE-task-workflow.md` import that recursively loads the four thematic files below.

---

## Rule structure (split 2026-05-18)

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
- **The ask for this blocker already sits on ANOTHER card — don't double-ping** → **`add_dependency(this_card, depends_on=that_card)`, and do it BEFORE (or in the same breath as) declining the marker.** The `❓ Blocking @operator` marker does **two** jobs: it queues an ask in the operators digest **and** it freezes the feed (the server stamps `human_gate=true`). Declining to repeat the ping — correctly, the operator must not be asked twice about one state — used to surrender the freeze too: the card stayed in `todo`, every feed path re-picked it, and money burned on work that could not move (measured: two cards on one blocker, differing only by the marker — `#9ba8761d` 0 spawns vs `#739ee655` 3 spawns / **$87.91**). Since 2026-08-02 (`#8f0f9ef6`) a formal `blocks` edge onto a still-open blocker **freezes the feed by itself, adding nothing to the operators queue** — honoured by the dispatcher and all **three** fiddler paths that can paste into a session by task_id (`gather_candidates`/todo-feed, zombie-rescue, and soft-nudge since `#025659c8`), and excluded from `feedable` so the lane is not painted BROKEN. Only `blocks` gates (`relates_to`/`is_child_of` never do), and it fails **open** (unreadable deps → the card feeds). ⚠️ **Order matters:** assigning/creating a card emits `task.assigned`, which spawns within ~2s — a dependency added *after* that arrives too late for the first spawn (observed live: fed 1s before its own edge existed). Link first, then decline.
- **Creating a Mesh task with assignee=the operator** → default `todo`. `triage` **only if** it really blocks other work and needs the operators decision. See `CLAUDE-workflow-reference.md §0c the operator-assigned status`
- **Tasks in `review` >24h without movement** → Atlas /sweep flag + the operator ping; >72h without discussion → auto-move to triage. See `CLAUDE-workflow-reference.md §0d Stale review`
- **Verify / monitor / passive-wait task** (idle on a window/clock/scheduled reactivation — nothing to do right now) → status=`backlog` + label `phase:verify`|`kind:monitor`|`no-pavel-triage`; **NEVER `in_progress`** (it churns the dispatcher → 20 noise checkpoints + false auto-triage) and **NEVER `❓@operator`** (no decision pending). Dispatcher auto-parks these to backlog at count==3. Prefer just closing if the deliverable shipped + smoke passed. See `CLAUDE-workflow-reference.md §0m`
- **Parking a task in `backlog` (stop it being fed / re-enqueued)** → **the park now holds.** `backlog` is overloaded — it means both *"not ready yet, waiting on a blocker"* and *"parked, do not feed this"* — and `mesh-intake-sweep` used to see only the first: it promoted any dep-less backlog task straight back to `todo` ("all dependencies satisfied" is **vacuously true** when a task has no deps), silently undoing the park within one 30-min cycle and never telling the parker (#b832d451; it reverted a park in **26 minutes** and left #1727e318 sitting in `todo` on a dead agent for 20 days). Since 2026-07-13 the sweep reads the **activity log** and skips any task whose last status move was a **demotion into backlog** (`todo`/`in_progress`/`review` → `backlog`) **and** which has **no dependencies** — there is no future event that could make such a task newly-ready, so promoting it is pure noise. **So: to park, just move the task down to `backlog` — no label needed, your intent is read from the move itself.** Two cases still auto-promote by design: a task **born** in `backlog` (that's genuine intake — the sweep's actual job) and a **dep-blocked** task once its blockers close (Mesh's server-side auto-transition also does this). To park a task that **has** dependencies, add label `freeze` / `no-promote` / `no-intake-promote` (`PASSIVE_WAIT_LABELS`). the operator can also freeze anything by commenting "do not pull from backlog" / "freeze".
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
- **Add/change an MCP connector or rotate a key** → do NOT hand-edit 10 `.mcp.json` files. Single source of truth = `~/.config/mcp-registry/registry.yaml` (+ secrets in `~/.config/agents/keys.env`, mode 600). `mesh-dispatcher` regenerates `.mcp.json` for each agent before spawn (fail-safe, atomic, backup). Guide + rollback: `docs/MCP_REGISTRY.md`. CLI: `mcp-registry validate|list|diff --all|apply`. Task 75de4532.
- **Triage routing for recurring/audit findings** → `CLAUDE-workflow-reference.md §8`
- **Subagent output must have a schema** → `CLAUDE-workflow-reference.md §9 Structured outputs`
- **Task requires ≥2 subagents — decompose into a society** → `CLAUDE-workflow-reference.md §10 Society of Mind`
- **Atlas / orchestrator on a multi-step initiative** → `CLAUDE-workflow-reference.md §11 Captain Pattern`
- **Strategic decision (build/buy, refactor, deprecate)** → `~/.claude/agents/reasoner.md` (subagent invocation)
- **What to mock vs test real code** → `CLAUDE-workflow.md §1o Mocking convention` (TL;DR: mock ONLY at true external boundaries — PSP/HTTP/time; never mock DB layer, internal sagas, webhook handlers)

---

**Last updated:** 2026-05-18 by Orbit — split into 4 thematic files after CoALA/Hindsight audit + memory layers integration.
**2026-05-18 by Nova** — added `§1a Repo Freshness (anti commit-clobber)` to `CLAUDE-workflow.md` + nav/anti-pattern/pattern links; `mesh-dispatcher` enforces sync before spawning an agent (task 30663152).
**2026-05-21 by Orbit** — added `§6 External channel identity discipline` to `CLAUDE-communication.md` (a hard rule after an agent leaked its nature in a public Discussion, the operator: "to them, you are ordinary users"); persona-per-real-account table + pre-send checklist; task b3964d49.
**2026-05-24 by Orbit** — added nav pointer to **centralized MCP registry** (`registry.yaml` = source of truth, dispatcher regenerates `.mcp.json` per-spawn); guide `docs/MCP_REGISTRY.md`; task 75de4532 (Wave 2.3).
**2026-05-26 by Nova** — added `§Auto-trigger threshold` to `CLAUDE-model-selection.md` (mandatory task-splitter call on checkout when >300 words OR >3 ## sections; post YAML comment + mempalace log); dispatcher prompt injection; task 2dde31d9.
**2026-06-01 by Nova** — added `§0k Delegation level` to `CLAUDE-workflow.md` (Miras F3 phase 3, task 7aba271c); 3 modes: auto/review/supervised, captain master=review minimum, agent behavior rules per mode.
**2026-06-03 by Orbit** — added `§1b Deploy Discipline` to `CLAUDE-workflow.md` (the operator directive after Spark prod-down `#a1c3bc52`, task bd3e71b8): migration BEFORE/atomic-with code; never ship code ahead of its migration; backward-compatible additive; incompatible = expand→migrate→contract; CI `migrate` gate before app-image swap (fail-closed); rollback = revert image first then migrate. Per-service CI-gate enforcement subtasks spun off.
**2026-06-07 by Nova** — (B1.3/B1.4) hardened `safe_sync_repo` detached-HEAD → skip (not ABORT) in `mesh-dispatcher`; added nav pointer + `§0k «review trap»` to `CLAUDE-workflow.md` documenting `delegation_level≠status` confusion + `_warn_delegation_review_trap` dispatcher guard; task `0d9defac`.

**2026-06-13 by Orbit** — added `§1k Visual tasks` to `CLAUDE-workflow.md` (the operator, after 3 failed detail-page redesigns): a UI task does not go to review/done without 1440+393 screenshots + comparison to the reference; a `page-shot` one-liner removes the excuse.
**2026-06-18 by Nova** — added `§1o Mocking convention` to `CLAUDE-workflow.md` (F4 Fleet Testing, task `97a01105`): mock ONLY at true external boundaries (PSP/network/time); never mock DB layer/Saga/internal handlers; anti-pattern table + per-stack guidance (Python/Go/TS).

**2026-07-10 by Orbit** — added nav rule **«Closing a task — comment-gate is NOT a permission wall»** (the operator directive after Atlas escalated closing redundant Spark task #25729a69 as "governance-blocked"). Investigated live mesh-api/mesh-mcp code + rules DB: **no hard ownership block** — any agent closes any task (checkout auto-releases); only gate to `done` = require-comment rule (≥20 chars). Verified by closing #25729a69 as Orbit (non-assignee agent). Kills the "governance-blocked → escalate" misread; orchestrator closes redundant/superseded tasks directly. supervised/`human_gate` remain the only human-only cases.

**2026-06-18 by Orbit** — added `§1n User-facing tasks — passing authed browser scenario before done` to `CLAUDE-workflow.md` . Closes the false-done gap (200 ≠ proof): a user-facing task does not go to `done` without an e2e scenario doc + an independent verifier's authed run passing green with behavioral assertions (console/5xx fixture).
