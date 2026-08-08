# Task Workflow — Common Rules for All Mesh Agents

This file is the **shared truth** for all Mac Mini Claude Code agents (Atlas, Vega, Nova, Grove, Delta, Ember, Kilo, Pixel). All working rules around Mesh tasks, state awareness, and communication with the operator live here. If your role has specifics — they belong in your own CLAUDE.md, but these rules take PRIORITY.

Maintained by Orbit (coordinator) in `/Users/fleet/ClaudeCowork/bob/CLAUDE-task-workflow.md`. All agent workspaces use a symlink to this file plus a `@CLAUDE-task-workflow.md` import.

---


## Phase Discipline (added 2026-05-15, inspired by GSD `get-shit-done`)

Any non-trivial task goes through **5 phases**. Each has a concrete deliverable and an owner. This eliminates «started coding without a spec», «merged without verify», «closed but doesn't work».

### Phases

| # | Phase | Subagent | Deliverable | Mesh label |
|---|-------|----------|------------|------------|
| 1 | **discuss** | `pm-spec` (says what we're building) | `dev-docs/specs/<feature>.md` with scope + acceptance criteria | `phase:discuss` |
| 2 | **plan** | `architect` (how to build) | Design decisions in spec; ADR in `dev-docs/adrs/` if significant | `phase:plan` |
| 3 | **execute** | stack-specific dev (`developer`, `django-developer`, `radix-frontend`, etc.) | Code + tests in a feature branch, PR open | `phase:execute` |
| 4 | **verify** | `tester` + `verifier` (independent) | All acceptance criteria passed, tests green, manual smoke ok | `phase:verify` |
| 5 | **ship** | `code-reviewer` then merge | PR review, squash merge, deploy if applicable | `phase:ship` |

If stuck in any phase → invoke the `debugger` agent (root-cause analysis before fix).

### Rules

1. **Crossing phases is explicit**. The lead (or executor) adds the label `phase:X` via `update_task` / `add_comment` on transition. The labels show where the task got stuck.
2. **No skipping verify (PRE-HANDOFF VERIFY GATE, B3 #41e58997)**. Before moving any task to **`review` OR `done`** you MUST **spawn the cheap `verifier` subagent** (`Agent` tool, `subagent_type="verifier"`, model sonnet) against the task's acceptance criteria + proving commands/URLs — **do NOT self-verify in-context**. An independent fresh-context pass catches the confirmation-bias misses that cause review-bounces; it's also cheap and keeps the orchestrator session short. Paste the verifier's per-criterion VERDICT + overall SHIP/DO-NOT-SHIP/ROUTE as a task comment. DO-NOT-SHIP → fix and re-spawn the verifier; don't hand off. This is the **default path** — skipping is the exception and the exemption must be named in the handoff comment. **Exemptions:** trivial/no-op tasks (typo, label-only, pure-monitor / passive-wait) and tasks already verified by a verifier subagent this session. (Why: opus orchestrators Atlas/Orbit delegated to subagents ~1-5% and carried 14-18% rework in long single sessions; this gate makes the cheap delegation the default. See `docs/B3_subagent_verify_gate.md`.) `tester` is additionally mandatory for code changes.

   **2b. ROUTE ≠ review (A4-2 #b6be48ec, 2026-07-28).** The verifier has a third verdict. Before it may say SHIP it runs the two probes the reviewer will run anyway — **P1** `gh pr view <n> --repo <org>/<repo> --json state,mergedAt,baseRefName` (accepted only on `state==MERGED` + non-null `mergedAt` + base `main`/`master`) and **P2** the live probe named in the AC. If the work itself holds up but P1/P2 is outstanding and **you don't hold the rights to clear it**, the verdict is **ROUTE**, carrying `missing` / `probe` / `rights` / `next`.

   On ROUTE you do **not** `move_task(review)`. You:
   1. `move_task` → **`todo`** (not `review`, not `blocked`),
   2. `assign_task` → the agent named in `rights` (merge → Delta; deploy/release → the repo's named owner or CI per `CLAUDE-workflow` §1f). `rights: unknown` → assign to the task creator and say so,
   3. comment the verifier's ROUTE block verbatim, so the next holder sees the probe output rather than re-deriving it,
   4. **`release_task` — drop your own checkout in the same action** (added 2026-08-04, #cee5c03f). Checkout auto-releases **only** on a terminal move (`done`/`review`/`cancelled`); ROUTE moves to `todo`, which is not terminal, so the lock survives the handoff. A foreign lock makes fiddler **drop the feed** — `fiddler.py:1082-1084` refuses any task whose `checked_out_by` is another agent, logging `skip feed — checked out by <id> — someone else holds the lock`, and it stays that way until the 2h TTL expires. Live instance: Vega performed steps 1-3 correctly, kept the lock, and Ember's lane silently skipped the card for two feed cycles (`02:35:56`, `03:06:15`). ROUTE means *by construction* "someone else does the rest" — that is exactly the moment your lock is pure cost.

   **Clearing someone else's stuck lock** (for whoever finds one): `release_task` from your own session fails — *«no checkout_token found»*, the token isn't yours. Take the explicit token from `get_task` and call the REST endpoint directly: `DELETE /api/v1/tasks/<id>/checkout` with body `{"checkout_token": "<token>"}` → 204. Reversible: if the holder is still working it, they simply check out again.

   **Verifying a handoff actually landed — presence in the queue is not feedability.** `agents/me/tasks?status_category=todo` under the recipient's key returns the card even while it is foreign-locked, because `checked_out_by` is not in that response. A green there is not evidence. Check **three** fields — `assignee`, status, and `checked_out_by is None` — or read the literal `feeding task #… → <agent>` line in `~/.fiddler/logs/fiddler.log`. (Companion to `CLAUDE-communication.md` §"How @-mentions wake": a mention does not wake a fiddler lane, so the card in `todo` is the whole delivery mechanism — and a lock disables it.)

   Why: 19 of Grove's 24 review-bounces in 07-20→07-27 (79%) were *not* code defects — the work was correct and the outstanding step was merge/release/deploy/backfill, none of which the executor can perform (A4 #232fa45c, `scripts/agent-eval/rework-a4-20260727.md`). Handing that to `review` guarantees a bounce however good the work is. `review` then keeps one meaning: **"this is live, please accept."**

   **Honest limitation — do not misread the metric.** ROUTE does not reduce work. It moves the visibility of the delay out of the executor's rework lane and into the merge/release owner's backlog. That is the correct place for it, but the next weekly review must read the drop as a *transfer*, not as "the executor improved". Re-runnable measurement: `scripts/agent-eval/route-gate-metric.py`.
3. **One phase per task**. If the task starts spreading across multiple phases with different owners — split into child tasks.
4. **Spec before code**. If you entered execute without an explicit `dev-docs/specs/<feature>.md` — stop, go back to discuss. Exception: trivial fixes (typo, single-line bug fix) — can go straight.
5. **Update STATE.md** on phase transition. This is the «where we are now» file, read by every new session/agent.

### When to use the `debugger` agent

- Tests fail after a change — debugger before developer fix
- Build broken with an unclear error — debugger diagnoses
- Behavior unexpected — debugger reproduces + isolates cause
- Performance regression — debugger profiles + finds hot path
- Flaky test (passes local, fails CI) — debugger looks for timing/state leak

`debugger` does NOT apply the fix — it produces a **structured Bug Report** with root cause + recommended fix path. Then `developer` applies it.

### Example flow (typical feature)

```
the operator: "I want an email digest of new assets"
  ↓
Atlas (orchestrator) → create_task in the Spark project, label phase:discuss, assignee=Vega (lead)
  ↓
Vega invokes pm-spec → writes dev-docs/specs/email-digest.md (scope, criteria)
  ↓
Vega updates task: label phase:plan, comments spec link
  ↓
Vega invokes architect → decides: cron? what do we send on? template engine?
  ↓
Vega updates task: label phase:execute, assigns child task to Ember (dev)
  ↓
Ember picks up child → branch → code → tests → push → gh pr create
  ↓
Ember updates task: label phase:verify, comments PR link
  ↓
Vega invokes tester → runs tests + manual smoke (test digest to himself)
  ↓
Vega invokes verifier (independent fresh-context check)
  ↓
verifier OK → Vega updates task: label phase:ship
  ↓
Vega invokes code-reviewer → review PR → squash merge → deploy
  ↓
Vega move_task → done. STATE.md updated by Ember.
```

### Existing tasks without labels

Old tasks (created before 2026-05-15) don't have phase labels — no need to retroactively. Apply only to new ones.

### When to skip phase discipline

- Hotfix in production (urgency overrides) — fix, label `hotfix`, retroactive verify after
- Pure infra config change (env var, secret rotate) — no spec needed
- Documentation only changes
- Refactor without functional change — combine plan+execute, but **must** verify (tests + manual)

---

## Phase-aware Model Selection (mesh-dispatcher v5, 2026-05-15)

`mesh-dispatcher` on Mac Mini automatically picks a Claude model on spawn based on task labels. This reduces token budget without quality loss — cognitive phases get opus, mechanical — sonnet.

### Mapping

| Phase label | Default model | Reason |
|-------------|---------------|--------|
| `phase:discuss` | opus | scope clarification, design tradeoffs |
| `phase:plan` | opus | architecture decisions, risk analysis |
| `phase:debug` | opus | root-cause hunting, hypothesis ranking |
| `phase:execute` | sonnet | well-specced code production |
| `phase:verify` | sonnet | mechanical test runs + acceptance check |
| `phase:ship` | sonnet | PR review, merge, deploy |
| `kind:visual` | **opus** | UI/redesign — taste and interpreting a mockup; layout against an ALREADY-approved structure is fine on a cheaper model |

### Override priority (highest first)

1. **Explicit override**: label `model:opus` or `model:sonnet` — always wins. Use when execute requires an architectural decision mid-flight, or ship needs a senior eye.
2. **Phase-driven**: label `phase:X` picks the model from the table above.
3. **Default**: `mesh-agents.json` per-agent `model` field — fallback if phase isn't specified.

On spawn the dispatcher logs `model override: X → Y (reason)` to `~/logs/mesh-dispatcher.log` — easy traceback of why a given run was on this model.

### Rules of thumb

- Always try to attach `phase:X` when creating a task — otherwise everything goes to the default (usually sonnet) and complex tasks get an undersized model.
- Use `model:opus` override sparingly: opus is 3x more expensive. If you regularly need opus in execute — revisit the spec (likely it's insufficient and the agent is filling in design).
- For recurring mechanical tasks (sync, rsync, doc updates) — `phase:execute` is enough, sonnet will handle it.
- For discovery / RnD / "look at the best way" — mandatorily `phase:discuss` or `phase:plan`, otherwise sonnet gives a shallow answer.

---

## Parallelization via `task-splitter` (2026-05-15)

Big tasks (`>1 deliverable, >1 agent could work concurrently`) are split by the **`task-splitter`** subagent into a DAG of subtasks with explicit `depends_on`. This is the second part of the GSD pattern (after Phase Discipline).

### When to invoke `task-splitter`

- Task description contains multiple deliverables ("backend + frontend + migration + docs")
- Cross-product feature (affects Mesh + Spark + Site simultaneously)
- Estimated agent-session work > 1 (if it seems like >1 session is needed — split)
- the operator/orchestrator explicitly says "split"
- **Auto-threshold met** (see below) — triggered automatically, no manual decision needed

### Auto-trigger threshold (MANDATORY — added 2026-05-26)

After checking out a task, **before any implementation**, check if the auto-threshold is met. This eliminates the manual «should I split?» decision.

**MUST invoke task-splitter** if **ANY** of the following are true:

| Signal | Threshold |
|--------|-----------|
| Description word count | > 300 words |
| `##` section headers in description | > 3 |
| Task already has `phase:plan` or `phase:execute` label AND involves ≥2 named components | e.g. "backend + frontend", "API + migration + docs" |

**Skip the auto-trigger** (do NOT call task-splitter) if:
- Task comments already contain `verdict: split` or `verdict: do_not_split` — splitter already ran
- Task already has ≥1 subtask (`subtask_count > 0` in `get_task`)
- Task description < 100 words (trivial)
- Label `no-split` is present

**Sequence when auto-threshold met:**

```
checkout_task → get_task (check subtask_count + comments for prior verdict)
  ↓ no prior verdict found
invoke task-splitter (Agent tool, subagent_type="task-splitter", prompt="split task <id>")
  ↓
verdict: split
  → 1. add_comment(task_id, body="[task-splitter plan]\n```yaml\n<full YAML output>\n```")
  → 2. write mempalace drawer: kind=decomposition, verdict=split, task_id=<id>, subtask_count=N
  → 3. create_subtask × N + add_dependency per DAG → assign root subtasks → proceed
verdict: do_not_split
  → 1. write mempalace drawer: kind=decomposition, verdict=do_not_split, task_id=<id>, reason=<splitter reason>
  → 2. add_comment(task_id, body="task-splitter: do_not_split — <reason>. Proceeding as single task.")
  → 3. proceed with task as-is
```

### Subtask ownership when an orchestrator decomposes (R3, 2026-06-11)

When a cross-project orchestrator (Atlas/Orbit) captains an epic, the exec subtasks must be **owned by the product LEAD**, not the orchestrator — otherwise every unit's review-handoff lands back on the captain (wrong attribution + review pile-up; this is what `[Mesh R1]` also fixes server-side). On `create_subtask`:
- Set each subtask's `assignee_id` = the **product lead** (Comet=Argus, Nova/Kilo=Mesh, Vega/Ember=Spark, Delta/Grove=Obsidian, Pixel=sites) or the concrete builder — never the captain.
- On the **master/umbrella**, set the reviewer = the product lead (so when units finish, review goes to the lead who owns the product, not to you the captain).
- The captain's job is coordination + final cross-product sign-off, not being the assignee of every leaf. If you (orchestrator) are `created_by` on the subtasks, lean on `[Mesh R1]` + explicit `assignee_id` (R2) so the handoff still routes to the lead.

**Why mempalace logging is mandatory**: enables cross-session recall of decomposition decisions, prevents re-running task-splitter on already-analysed tasks, feeds the coordination wing for Captain Pattern visibility.

### How it works

`task-splitter` does NOT create subtasks in Mesh — it returns a YAML plan with:
- `subtasks[]` — id, title, deliverable, suggested_assignee, phase, depends_on
- `parallelism.max_concurrent` — how wide the DAG is
- `parallelism.critical_path` — longest chain (defines minimum time)
- `risks[]` — known unknowns that may force a re-split later

The caller (lead agent or the operator) reviews the plan → applies via `mcp__evc-mesh__create_subtask` with proper `depends_on` via `add_dependency`.

### Verdict gates

`task-splitter` may return `verdict: do_not_split` if:
- DAG width < 2 (everything linear, split gives nothing)
- Task too abstract — must first run `phase:discuss`
- Trivial fix — split overhead > benefit

In these cases the lead does the task as-is.

### Example trigger

```
the operator: "split the task 'migrate to the new auth model' and fan it out"
  ↓
Atlas invokes task-splitter with task_id
  ↓
task-splitter returns YAML plan (5 subtasks, 2-wide DAG, critical path = 4 steps)
  ↓
Atlas applies: create_subtask × 5, add_dependency × N
  ↓
Atlas assigns root subtasks (depends_on=[]) → executors start in parallel
  ↓
As dependencies merge, dispatcher auto-spawns next-layer subtasks
```

### Anti-patterns

- **Don't split** small tasks (≤1 deliverable) — orchestration overhead outweighs the gain
- **Don't make >7 subtasks** under one parent — that's a signal the parent is wrong-scoped
- **Don't ignore critical_path** — if it's = 5 sequential steps, parallelism doesn't help, replan
- **Don't auto-assign from `suggested_`** — the lead decides; suggested is a hint

---

> ⚠️ **Note: this label→model routing applies to the DISPATCHER path, not the feeder.** The table is a `mesh-dispatcher` mechanism (it picks the model at spawn time from labels). The feeder (fiddler) feeds tasks into an already-running session with a FIXED model (it reads `model` from the transcript only to cost it). So under the feeder, per-task cognitive routing does not apply — an agent runs on its start model. The immediate lever for a visual task is to switch that lane's model (`agent-model <agent> opus`).
