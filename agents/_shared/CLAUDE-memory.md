# Task Workflow — Common Rules for All Mesh Agents

This file is the **shared truth** for all Mac Mini Claude Code agents (Atlas, Vega, Nova, Grove, Delta, Ember, Kilo, Pixel). All working rules around Mesh tasks, state awareness, and communication with the operator live here. If your role has specifics — they belong in your own CLAUDE.md, but these rules take PRIORITY.

Maintained by Orbit (coordinator) in `/Users/fleet/ClaudeCowork/bob/CLAUDE-task-workflow.md`. All agent workspaces use a symlink to this file plus a `@CLAUDE-task-workflow.md` import.

---

## 9. Memory layers — what goes where (added 2026-05-18, after CoALA/Hindsight audit)

Base: `PROPOSAL — Layered Memory Architecture v2 (2026-05-18).md` + `DEEP-DIVE — Session checkpoint + KG at scale (2026-05-18).md`. Four memory layers, each with its own store. **Don't duplicate**; each knowledge category lives in one place.

### 9.1 Four layers

| Layer | What | Where it lives | When to write |
|------|-----|-----------|--------------|
| **Working** | current session context | context window + auto-memory `MEMORY.md` | reactively: on a significant intermediate result |
| **Episodic** | events «what happened when» | Mesh `remember` (primary) | after `move_task done` / `review`, on incident / blocker |
| **Semantic** | long-lived facts about the world | **Workers: Mesh ONLY** — `set_project_knowledge` (project-scoped fact) or `remember` with high relevance (workspace fact). Mempalace is NOT part of the worker protocol (Phase A consolidation 2026-06-10: fleet usage measured at zero; one write path = real discipline). Mempalace stays as the Orbit/coordination archive only. | on discovering a stable fact |
| **Procedural** | rules, patterns | `CLAUDE.md` + `CLAUDE-task-workflow.md` (via PR) | reactively on feedback from the operator |

**Marker episodic vs semantic**: «this will be true a month from now» → semantic. «This happened on 18.05» → episodic.

### 9.2 Write triggers (mandatory)

⚠️ **Key format — hyphens only, NEVER colons.** `remember`/`set_project_knowledge` validate `key` server-side against `^[a-z0-9][a-z0-9-]*[a-z0-9]$` (a deliberate slug-format constraint, not a bug) — a colon-delimited key 400s with `Validation failed`. This file previously showed `episode:<agent>:<date>:<slug>`-style examples that contradicted the enforced format — every agent who copied them verbatim silently lost their first `remember()` write. Fixed 2026-07-25 (Kilo, task `1d573509`); the client-side error message that hid the field-level detail (so the failure was silent instead of self-explanatory) is fixed in `evc-mesh-mcp` PR #33. All examples below are the corrected kebab-case form — copy them as-is.

After each `move_task done` (or `review`):

```yaml
# Mesh remember call
key: episode-<agent>-<YYYY-MM-DD>-<task_short_slug>
scope: workspace
content: |
  Task: <title> (<task_id>)
  Did: <2-4 bullets — what was actually done>
  Decisions: <key choices — options rejected and why>
  State at exit: <what's left hanging, what I tried and didn't work>
  Next-touch hint: <if this area comes up again — look at X, Y>

  self_rated:
    outcome: complete | partial | blocked | wrong_direction
    confidence_correct: 0.0..1.0  # how sure work was right
    rework_likely: true | false   # «the operator may ask to redo»
tags: [kind:session-checkpoint, project:<slug>, owner:<agent>, phase:<phase>, relevance:<1-10>]
relevance: <1-10>
expires_at: <auto, 7d for session-checkpoint>
```

**`self_rated` (added 2026-05-20)** — self-assessment block for the eval baseline. This is **not an objective** metric, but a **strong signal** for observability:
- `outcome` — what you took away from the session: `complete` (done as intended), `partial` (some, but not all), `blocked` (hit an external blocker), `wrong_direction` (went the wrong way, had to switch approach)
- `confidence_correct` — 0.0 (not sure at all) → 1.0 (fully sure). Be honest — for self-rated «all 1.0» = useless signal
- `rework_likely` — true if you know the operator/lead will likely ask for a redo (e.g. did it in a rush, didn't test an edge case, the choice was between two options and you're unsure of the chosen one)

This block is collected by eval-snapshot.py and aggregated into a weekly report. **Honest self-rating is more valuable than optimistic** — the data is used for improving model selection / prompt engineering, not for blame.

On incident / blocker — in parallel with the usual `add_comment`:

```yaml
key: episode-<agent>-<YYYY-MM-DD>-incident-<short>
tags: [kind:incident, project:<slug>, owner:<agent>, relevance:8+]
relevance: 8..10
expires_at: <auto, 180d>
content: |
  What broke: ...
  Detection: ...
  Root cause (hypothesis or confirmed): ...
  Mitigation: ...
  Permanent fix: <link to task or "TBD">
```

On discovery of a new **stable** fact (workers — Mesh, the ONLY durable store; Phase A 2026-06-10):

```yaml
# project-scoped fact → set_project_knowledge (UPSERT by key)
project_id: <project uuid>
key: <kebab-slug, e.g. spark-ingestion-author-source>
category: deploy | stack | conventions | gotchas | api | auth
value: |
  <factual statement — what is true and will stay true; ≤200 lines>
tags: [<product>, <topic>]
source_url: <task id / PR / file path>

# workspace-wide fact → remember (no project binding)
key: fact-<agent>-<kebab-slug>
scope: workspace
relevance: 0.7-0.9
tags: [kind:learnings|kind:incident, owner:<agent>, project:<slug>]
```

> Mempalace (`add_drawer` etc.) is **not** in the worker write path anymore — it remains the Orbit/coordination archive. If you are not Orbit, you never need a mempalace tool to fulfil the memory protocol.

### 9.3 Importance scale (reference, for the `relevance` field — **float 0.0–1.0**)

The Mesh API is implemented with `relevance` as float `[0.0, 1.0]`. Reference scale:

| relevance | What it is | Examples |
|:--:|---|---|
| 0.05 | trivial / routine | typo fix, dependency bump, comment cleanup |
| 0.15 | low | rename, refactor purely cosmetic, doc trivia |
| 0.25 | minor change | small bug fix without user-impact, lint config |
| 0.40 | normal task | minor feature, single test |
| 0.50 | normal feature/fix (default) | feature done, bug fix with impact |
| 0.60 | substantial | module refactor, small-scale migration |
| 0.70 | impactful | new endpoint affecting prod, deploy pipeline change |
| 0.80 | important | incident resolved, architectural decision, scaling break |
| 0.90 | critical decision | major arch pivot, vendor swap, breaking API change |
| 1.00 | poignant / incident | production down, data loss, the operator-level pivot |

Default = `0.5` if unsure.

⚠️ **Tag `relevance:` prefix** (see §9.4) is written as `relevance:0.8` — a duplicate for compat while recall doesn't yet support filtering by the numeric field.

### 9.4 Tag vocabulary (canonical prefixes)

Use ONLY these prefixes — agents and the future KG will find it easier to parse. Free-form tags without a prefix are allowed, but prefix-tags are the standard.

| Prefix | Semantics | Example |
|---|---|---|
| `kind:` | record type | `kind:decision` `kind:incident` `kind:learning` `kind:session-checkpoint` `kind:fact` |
| `project:` | project scope | `project:evc-spark` `project:evc-mesh` `project:cross-project` |
| `owner:` | who owns / who wrote | `owner:atlas` `owner:vega` |
| `phase:` | workflow phase (same as for tasks) | `phase:execute` `phase:verify` |
| `relevance:` | duplicate of `relevance` int field (for filter compat) | `relevance:8` |
| `expires:` | YYYY-MM-DD when to forget (duplicate `expires_at`) | `expires:2026-08-18` |
| `caused_by:` | causal link to a previous entry/incident | `caused_by:incident-utf8-freeze-2026-05-16` |
| `depends_on:` | dependency target | `depends_on:evc-mesh-mcp` |
| `decided:` | decision pointer | `decided:vpn-single-writer` |
| `deployed:` | YYYY-MM-DD deploy date | `deployed:2026-05-15` |

This is a semi-structured proxy for the future Knowledge Graph (we'll activate it at 12+ agents — see Deep-dive). For now filterable via `recall(tags=…)`.

#### §9.4.1 importance_score scoring table (added E1 Valuation Gate, 2026-06-01)

When `remember(text, tags)` is called, an importance score (0.0–1.0) is computed and stored in the dedicated `memories.importance_score` column — **distinct from `relevance`** (`relevance` = access-time boost/decay signal; `importance_score` = write-time semantic value). Computed as follows:

| kind: tag | Base score | Notes |
|---|---|---|
| `kind:incident` | **0.85** | Highest — incidents must be retrievable |
| `kind:decision` | **0.80** | Architectural and project decisions |
| `kind:learning` | **0.70** | Validated patterns, post-mortems |
| `kind:fact` | **0.60** | Stable facts about the project |
| `kind:canonical-decision` | **0.80** | Same as decision |
| `kind:session-checkpoint` | **0.30** | Default — bulk ephemeral output, low value |
| (no kind: tag) | **0.50** | Default fallback |

**Boost rules (additive, capped at 1.0):**

| Condition | Boost |
|---|---|
| Text mentions canonical entity keys: ICP, architecture, license, security, money | **+0.10** |
| Tags include `relevance:0.8` or `relevance:0.9` or `relevance:1.0` | **+0.10** (explicit agent override) |
| Repeated `remember()` with existing entry having tag overlap ≥80% | **+0.10** per repeat (capped at 1.0) |

**Recall threshold:** `recall(query)` defaults to `min_importance=0.4` — entries below this are excluded from default results. Pass `min_importance=0` to retrieve all including low-score entries. (`min_importance` is the MCP-tool param added in s3 / evc-mesh-mcp PR #8.)

> **Backing column (s1, rolling out):** `importance_score REAL NOT NULL DEFAULT 0.5 CHECK (0..1)` + `idx_memories_importance(workspace_id, importance_score DESC)` — migration `20260601058`. It is **orthogonal** to the pre-existing `relevance REAL` column (migration `20260315041`, updated by `BoostRelevance` at access time). As of 2026-06-01 the migration is **not yet deployed to prod** (s1 reopened) — the scoring goes live once s1 lands. Agents using `remember()`/`recall()` never touch the column directly; treat the scoring behavior above as the contract.

### 9.5 Read protocol (when to recall)

**At session start** — what loads automatically:
- Auto-memory `MEMORY.md` + topic files → working memory (free)
- `CLAUDE.md` + `CLAUDE-task-workflow.md` → procedural (free)
- Current task (`get_task` + `list_comments`) → working

**During task work — recall router** (via subagent `recall-router`):
```
recall-router(query="what I know about X", task_id=<curr>, agent_id=<self>)
  → top-10 facts from 4 sources (mempalace search + grep auto-memory + Mesh recall + task comments)
  → ranked through RRF with recency decay 0.95^days
```

**Direct calls** (when recall-router is overkill):
- «recall my last session-checkpoint» → `mesh recall(tag=kind:session-checkpoint, created_by=self, limit=1)`
- «what I said about X» → `grep -ri "X" ~/.claude/projects/<self>/memory/`
- «semantic search by topic» → `recall(query="<q>")` (Mesh episodic; mempalace reads retired, Phase 4a #cd53e9aa — `mempalace_search` MUST NOT be called)

### 9.6 Memory management (recency / consolidation / expiration)

- **Recency decay**: on retrieval `score = relevance * 0.95^days_since` is applied. Recall-router does this itself, direct calls — no.
- **Daily consolidation** (cron 23:30 MSK): Orbit does a sweep — session-checkpoints over the day → groups by agent+project → writes a digest into mempalace `wing=<agent> room=daily-digest`. Old episodic auto-expires after 7d.
- **Append-only update**: a new digest does NOT overwrite old drawers. If new data changes understanding — write a **new** drawer with a `source_url`-link to the old one. No `mempalace_update_drawer` (destructive).
- **Expiration policy** (server-side in Mesh API via `expires_at`):
  - `kind:session-checkpoint` → 7d
  - `relevance ≤ 3` AND age > 180d → auto-delete
  - `kind:incident` → never (or explicit `expires_at`)
  - `scope=workspace permanent` → never

### 9.7 Anti-patterns (what NOT to do)

❌ Duplicating a fact in auto-memory + mempalace + Mesh — pick ONE store per category (see §9.1).
❌ Writing into mempalace wing `ember` / `delta-legacy` (now all together under `archive-import-2026-04` — read-only).
❌ Using `mempalace_kg_*` or `mempalace_diary_*` — **deprecated**.
❌ `mempalace_update_drawer` — destructive. Append via `add_drawer` + `source_url`-link.
❌ Writing a session-checkpoint to `session-state.md` (only Orbit uses this pattern, headless agents — Mesh episodic).
❌ Free-form tags not from the vocabulary §9.4 — allowed, but prefix-tags are the standard.

❌ **Writing a rule that concerns ANOTHER agent or the whole fleet into your personal auto-memory.** Personal memory loads into context only for its own agent — there is no cross-agent transfer, so the rule physically never reaches the executor (measured: of 16 such entries over two months, one arrived). A rule about someone else's behavior → a shared `CLAUDE-*.md` (§1r) or an ADR in the product repo; keep only what YOU do in personal memory. Catching yourself writing "a rule for <other agent>" into your own memory is an addressing error, not a note.

See also: `docs/memory-writing-guide.md` by Vega — practical templates, failure modes, examples from real practice.

### 9.8 Migration period (2026-05-18 → 2026-06-15)

- Mesh API extensions (relevance / tags / expires_at) — in development by Nova/Kilo. Until deploy spec: write `tags` with prefixes via text, Mesh doesn't validate yet — we'll add server-side filter later.
- Daily consolidation cron — after Phase 4 (in 1-2 weeks).
- recall-router subagent — available right after Phase 3 (now).
- All these details plug in as they become ready; **for now the base rule**: after `move_task done` → `remember(kind:session-checkpoint, …)`.

---

## 9.9 Canonical facts — where they live and how to propagate (added 2026-06-01, Memory E3)

**Root problem (Phase A discovery, master `#afdf8e15`):** the premise was *conflicts between competing canonical sources*; reality is worse — **there is no canonical read layer agents can use.** Evidence: Mesh `project_memories` ≈5 records total across 11 projects (the intended curated store, effectively unused); `workspace_memories` = 380 records but 94% are 7-day `kind:session-checkpoint` ephemera and **0** are tagged `canonical`; the Obsidian Knowledge vault (PRD/RFD/ROADMAP/Concepts/Decisions/Analyses) lives **only on the operators device** — not reachable by any agent. Net effect: agents re-derive strategy from stale checkpoints or invent it, and the operators RFD edits never propagate.

This section is the **shared-truth protocol** for the canonical layer being built in E3 (C1 Obsidian→Mesh sync, C2 `get_canonical` read tool). Follow it the moment the tools land; the slug + write-path rules apply **now**.

### 9.9.1 Authoritative source per doc type

| Doc type | Authoritative source | Who writes | How agents see it |
|---|---|---|---|
| Strategy / PRD / RFD / roadmap / concept | **Obsidian Knowledge vault** (`Acme/Knowledge/**`) | **the operator** (his device) | auto-synced into the Mesh canonical store by the C1 Local Sync watcher → readable via `get_canonical` |
| Operational decision / incident / durable fact | **Mesh canonical store** | agents | `set_project_knowledge(key=canonical:<topic>, kind:canonical)` → readable via `get_canonical` |
| Working / session context | Mesh `workspace_memories` (checkpoints) | agents | ephemeral (7-day TTL) — **never** treat as canonical |

**One rule of thumb:** if a fact must still be true next month and other agents must agree on it → it is **canonical**, not a checkpoint. A `kind:session-checkpoint` is *what I did*, never *what is true*.

### 9.9.2 Read path — `get_canonical(topic)` before authoring a competing doc

**Before** writing any strategy/PRD/design doc, or before acting on "what's our plan for X", call the canonical read tool:

```
get_canonical(topic: string, project?: string)
  → { results: [{ source, key, content, updated_at, project }], merged_markdown }
```

- `source` ∈ {`obsidian`, `project_memory`, `workspace_memory`}; `merged_markdown` is the recency-ranked merged view (Obsidian + `kind:canonical` records), with `kind:session-checkpoint` excluded by default.
- **Never re-derive strategy from a stale checkpoint or from in-context memory** — those are bounded by one session's writes and silently miss the operators RFD edits. The canonical view is the only source that reflects the operators vault.
- If `get_canonical` returns nothing for a topic that should exist → that's a propagation gap, not "no decision". Flag it (comment / ping owner), don't fill the vacuum with a guess.

> **Status (2026-06-01):** `get_canonical` is the C2 read tool (`#b067a9c1`), **landing** — not yet shipped. It ships with graceful degradation (Mesh-only view) independent of the C1 Obsidian sync (`#48f2bf09`). Until it deploys, use `get_project_knowledge` + `recall(tags=["kind:canonical"])` as the manual fallback, and apply the slug-discipline rule below so those records are findable.

### 9.9.3 Write path — `set_project_knowledge`, not a bare checkpoint

When an agent produces a durable canonical fact (operational decision, incident postmortem, stable fact):

```
set_project_knowledge(key="canonical-<topic-slug>", kind:canonical, project:<canonical-slug>, content=…)
```

- ❌ Do **not** record canonical facts as a bare `remember()` / `kind:session-checkpoint` — they drown in the 356-checkpoint pool, carry a 7-day TTL, and are excluded from `get_canonical` by default.
- ✅ `key=canonical-<topic>` + `kind:canonical` lands it in the curated store and makes it show up in the merged read.
- Strategy/PRD/RFD content is **the operators to write in Obsidian** — agents do **not** author those into the canonical store directly; they flow in via C1 sync. Agents write only *operational* canonical (decisions/incidents/facts).

### 9.9.4 Privacy — only `Acme/Knowledge/**` syncs, personal vault never

The C1 watcher syncs **only** paths matching the glob `Acme/Knowledge/**` (the `3-Concepts` / `4-Decisions` / `5-Analyses` subtree). Everything else — personal vault sections, daily notes, anything outside `Acme/Knowledge/` — is **never** synced. The filter is **fail-closed**: on any path ambiguity or vault-layout change, sync nothing rather than risk leaking a personal section. If you extend the sync scope, the exact glob must be pinned in the C1 PR and confirmed with the operator. (Privacy spec: C1 `#48f2bf09`, design §B.3.)

### 9.9.5 Slug discipline — one canonical project slug

A single logical project is currently written under 2–3 different slugs (`mesh-dev` / `evc-mesh` / `mesh`; `spark` / `evc-spark`; `team-relay` / `evc-team-relay`), so any project-scoped `recall`/`get_project_knowledge` silently misses the records filed under the other variants. **Use one canonical slug per project** going forward — drop the `evc-*` prefix and bare short forms:

| Canonical slug | Kill these variants |
|---|---|
| `mesh-dev` | `evc-mesh`, `mesh` |
| `spark` | `evc-spark` |
| `team-relay` | `evc-team-relay` |

`get_canonical` (C2) folds the historical variants internally so reads aren't lossy, but **new writes must use the canonical slug** — don't add to the fragmentation.

### 9.9.6 Anti-patterns

❌ Authoring a PRD/strategy doc without first calling `get_canonical(topic)` — you'll duplicate or contradict the operators vault.
❌ Recording a durable decision as a `remember()` checkpoint instead of `set_project_knowledge(kind:canonical)` — it expires in 7 days and is invisible to canonical reads.
❌ Treating an empty `get_canonical` result as "no decision exists" — it may be a propagation gap; flag it.
❌ Writing canonical records under a fragmented slug (`evc-mesh`, bare `mesh`) — breaks project-scoped recall.
❌ Agents writing strategy/PRD/RFD content directly into the canonical store — that's the operators Obsidian vault, synced via C1; agents author only operational canonical.

> Source: E3 inventory + design artifact `E3-canonical-layer-inventory-and-design.md` (§B), master `#afdf8e15`. Read tool C2 `#b067a9c1`, sync C1 `#48f2bf09`. Coordinates with F2 downstream-propagation (`#95a2b8f5`) — propagation of *rule* changes; this section is the *canonical-fact* layer.
