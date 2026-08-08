# Task Workflow — Common Rules for All Mesh Agents

**Shared truth** for all Mac Mini Claude Code agents (Atlas, Vega, Nova, Grove, Delta, Ember, Kilo, Pixel). All Mesh-task, state-awareness, and the operator-communication rules live here; role-specifics go in your own CLAUDE.md but these take PRIORITY. Maintained by Orbit in `/Users/fleet/ClaudeCowork/bob/CLAUDE-task-workflow.md` (symlinked + `@`-imported into every agent workspace).

> Full incident write-ups, worked examples, and enforcement-script internals for every rule → `CLAUDE-workflow-reference.md` (read on demand). This core carries the operative directives.

---

## 0. Execution pacing — ASAP by default (CRITICAL, added 2026-05-21)

**Rule for all agents and Orbit**: everything happens **as fast as possible, no delays**. Only exception: **pre-scheduled tasks** (cron / recurring / scheduled deploy windows / explicit deferred-until-date).

- ❌ **No time estimates** («3-4 weeks», «~2 days», «ETA Friday») unless asked.
- ❌ **No** «I'll take it tomorrow», «come back later», «after X I'll close Y», «when I get to it» — stalling. Take it now, or state an explicit hard-blocker with a concrete ask.
- ❌ **No** artificial pacing. Task needs N hours → spend them **in a row, now**.
- ✅ **Do it immediately** or name the **exact blocker** (e.g. «need Reddit OAuth client_id from the operator»). No vague «queued / prioritized».
- ✅ **Pre-scheduled** (future `due_date`, recurring cadence) — respect the schedule. **Estimates only when asked.**

Applies to Mesh comments, TG reports, sprint/ADR/spec/subtask docs, Captain plans.

---

## 1. State Awareness (CRITICAL)

### Conversational vs Actionable (FIRST filter)

**Conversational ack** (≤6 words, no work): "ok", "got it", "will look", "waiting", "thanks", "hold on", "here", … the operator acknowledging, not a task → **STAY SILENT or one word** ("here"/"ok"); DO NOT call tools, start analysis, or ack-first — nothing changed. 🚨 "I'll watch it in prod" ≠ "roll it out again"; "hold on" ≠ "start a new task".

**Actionable** (work, even if short): "do it", "check", "status", "ship it", "poke X", "make Y", "fix it", "/status". → Hard rule below.

### Hard rule — before processing an actionable prompt (≤6 words):

1. **AS THE FIRST STEP** call `mcp__evc-mesh__get_my_tasks` — snapshot of your open tasks.
2. Map the prompt onto open tasks: "status"/"what's in progress" → list short_id/title/assignee/status/last-update; "do it"/"ship it" → confirm the go-signal on the most recently discussed task WITHOUT new analysis; "when"/"delta" → a concrete computation from cron/scheduler/release-tag; "check X" → one targeted check.
3. **DO NOT re-launch a «quality analysis»** if the task is already created and in flight.
4. Unclear after mapping → ask a one-sentence clarification, don't interpret as «start over».

**Principle:** trust the observation (Mesh + filesystem), not stale in-context memory. A memory task_id `get_task` can't find → memory lies, Mesh is right.

---

## 1a. Repo Freshness — sync BEFORE any work (CRITICAL — data-loss)

**Hard rule — the FIRST step of any task that touches a git repo (before edits/commits):**

1. For **each** repo the task touches (all affected, not just the current): `git fetch --all --prune`.
2. Bring the branch up **SAFELY**: clean tree only-behind → `git pull --ff-only`; local unpushed commits + clean tree → `git pull --rebase`; up-to-date or only local-ahead → nothing.
3. **NEVER** overwrite divergence: forbidden — `git push --force`/`-f` over a shared branch, `git reset --hard origin/...` losing local, `git checkout -f` over edits. Resolve by **reconcile** (rebase local on remote).
4. Can't bring it up safely (dirty+behind, rebase conflict, ambiguous) → **STOP**: don't commit/push/work on stale; escalate to the operator (agent, repo, what: diverged/dirty/conflict).

**Defense-in-depth:** `mesh-dispatcher` syncs before spawning — but branch-switch / new clone / repo outside the dispatcher gate → sync is on you.

---

## 1b. Deploy Discipline — migration BEFORE code (CRITICAL — prod-down, added 2026-06-03)

**Hard rule — the migration is applied BEFORE (or atomically with) the code that depends on it. NEVER ship code ahead of its migration.**

1. **Migration to prod FIRST** (`alembic upgrade head` / `goose up`), **backward-compatible** with the running code: new columns **nullable**/default, new tables additive, **no `DROP`/`NOT NULL`/rename in the same release** as code expecting the old shape.
2. **Only THEN** deploy the code reading the new columns (schema exists → image swap safe either order).
3. **Incompatible change → expand → migrate → contract**, never one release: expand (nullable/additive + code writes both) → migrate (backfill, flip reads) → contract (drop old in a **later** release once nothing reads it).
4. **CI: `migrate` mandatory BEFORE the app-image swap, fail-closed** — migration fails → code deploy must **not** start.
5. **Rollback when code raced ahead and prod is 500:** **revert the app image** → apply the migration → redeploy. Don't forward-fix under a live 500.
6. **Pre-deploy check:** confirm prod `alembic_version`/schema matches the code; if not → migration first.

Applies to ALL DB-backed services. Per-service CI `migrate`-before-`deploy` gates → `bd3e71b8`; until one lands, ordering is the agent's responsibility on every manual deploy.

### Merging an `your-org` PR → `~/bin/gh-merge`, NOT `gh pr merge` (SSO-401, added 2026-06-11)

**Hard rule — merge org PRs via the `~/bin/gh-merge` wrapper, never the `gh pr merge` subcommand.** On the `your-org` org `gh pr merge <n> --squash` returns **HTTP 401 «Requires authentication»** even when `gh` is logged in and reads (`pr view/diff/list`) work — the subcommand's GraphQL mutation hits an SSO-gated step. This silently jams the whole queue: agents build, move the task to `review`, and wait for a merge that never lands (Spark stalled ~19h / Mesh 2 days, 2026-06-11). Use the wrapper around the REST PUT (not SSO-gated):

```bash
~/bin/gh-merge <n> your-org/<repo>     # → OK merged: True sha: …
```

**Use the wrapper, not the bare `gh api PUT`** (added 2026-07-29, `#c97be8b2`). `gh-merge` is that same REST PUT plus the one thing the raw command cannot do: it reads the PR's labels and **refuses** to merge anything carrying a `HOLD_LABELS` label (`hold`, `no-automerge`, `do-not-merge`, `wip`, `blocked`, `needs-review`, …) or marked draft — the same list `mesh-merge-train` honours, imported from `scripts/fleet_gate_labels.py` rather than copied. It **fails closed**: if the PR can't be read, or the label module can't be imported, it refuses rather than merging unchecked.

Why this replaced the bare command: `hold` was documented in §1q as *the* machine-readable way to say "do not merge yet", but it only ever stopped the train. The manual path — the one this section told everyone to use — read no labels at all. On 2026-07-29 two security PRs (`#428`, `#432`) reached prod carrying `hold`, one of them **7 minutes** after the label went on. A label that guards one of two merge paths is worse than none, because it gets believed and then stops being watched.

Deliberate override exists and is loud: `~/bin/gh-merge <n> <repo> --force-hold "<reason>"` prints the reason and proceeds. `--check-only` runs the gate without merging. `~/bin/gh-merge` is a **symlink** to `bob/scripts/fleet-tools/gh-merge`, so the prod copy and the tracked copy cannot drift (the failure mode that left the June merge-train gate dead in prod while the repo showed it fixed).

Before a **batch** merge, apply §1b ordering: inspect each PR for migrations and merge in version order; **two open PRs must never carry the same migration version** (collision → migrate-gate skips one or fails closed) — renumber before merging, and never merge a lower version *after* a higher one already landed. Verified working (Atlas, Spark unjam; Orbit `#23582ede`).

---

## 1c. Agent-runnable acceptance criteria — not "the operator will look" (CRITICAL — pipeline-stall)

**Hard rule — wherever technically possible, an acceptance criterion is a COMMAND the executor runs itself and reads the result of.** Executor-verified, the operators E2E the final sign-off — not «done pending the operator». Forms (→ §8 template): `curl … -w '%{http_code}'` / `/api/version` SHA == on-disk SHA (`[[learnings_mesh_binary_swapped_not_restarted]]`); headless Playwright smoke on the Mac Mini node (`nodes invoke … system.run`); `psql -c` (pairs §1b); `go test -run X` / `pytest -k …`. The closing comment **quotes the command + output**; «Done» without a pasted runnable result = anti-pattern (§6).

### Deep-verify — CONTENT, not status code (CRITICAL — prod-incident class, added 2026-06-10)

**HTTP 200 proves NOTHING about the feature.** SPA/SSR pages return a 200 shell with a 500-error block inside; APIs return 200 with `{"error": …}`. A bare `curl -w '%{http_code}'` is NOT verification — that exact pattern shipped broken pages (Comet/Argus: page 200, inner block 500). Every deploy/page/API verify MUST assert on CONTENT:
1. **Positive marker present** — a string/element only the WORKING feature renders (a data value, feature-specific id), not boilerplate.
2. **Error markers absent** — `error|exception|traceback|Internal Server|500` not in the body.
3. **API:** assert semantics (`jq -e '.ok == true'` / expected field), never just the code. **SPA:** verify the DATA endpoint it calls, or headless Playwright smoke — the HTML shell is always 200.
Helper — service-only probe: `~/bin/verify-page <URL> --expect '<marker>' [--reject '<extra>']` — timeout + both assertions, exit≠0 on failure; quote its output in the closing comment. `%{http_code}`-only evidence in a Done comment = anti-pattern (§6).
**Full pre-done gate** (PR merged + CI green + service probe): `~/bin/mesh-done-gate.py [--pr <PR-URL>] [--service-url <URL> --expect '<marker>'] [--skip-ci]` — exit 0 = SHIP, exit 1 = DO-NOT-SHIP; paste the `╔══ MESH DONE GATE ══` block into the closing comment. Use for ANY task that has a PR and/or a deployed service. The verifier subagent re-runs it independently before approving move→done — author paste ≠ certifier.
**Mesh write pre-flight** (before any move_task/remember/assign call → kills 422): `~/bin/mesh-write-shim.py preflight --task <id> [--status <intent>] [--key <k>] [--expires <e>]` — resolves intent→board-slug, short-id→UUID, colon-key→kebab, validates expires; exit 0 = safe to call MCP, exit 1 = DO-NOT-CALL+reason. Also: individual checks `slug`, `uuid`, `key`, `expires`, `gate`.

### PR Definition-of-Done — «PR opened» is NOT done (CRITICAL — abandoned-PR stalls, added 2026-06-11)

A task with a PR is **NOT done until the PR is merged + deployed + verified**. Moving the task to `review` on «PR opened» and walking away = the #1 stall (21 evc-mesh PRs rotted: dirty / red-CI / unmerged). Rules:
1. **Don't leave a PR red or dirty.** Before `move_task → review`: CI green + no conflicts (rebased on main). A red-CI / dirty PR is unfinished work — keep driving it, don't park it.
2. **Drive your own last mile:** merge → confirm deploy (CI auto-deploy or manual) → live-verify (deep-verify above) → close to `done`. Don't assume someone else merges your green PR.
3. **Merge command (your-org org):** `gh pr merge` returns **HTTP 401 (SSO-gated GraphQL)** even when logged in. Use `~/bin/gh-merge <n> your-org/<repo>` — the REST PUT plus the `hold`/draft gate, fail-closed (§1b). The fleet auto-merge bot (`~/bin/mesh-merge-train`) drains clean+green PRs periodically, but YOU still own deploy+verify+close.
4. A red-CI or merged-but-open task will be **re-fed to you by the PR-driver** — there is no «park it in review and forget». Migration PRs: sequence per §1b before merge.

### §1q Merge train — what stops it, and how to say "do not merge" machine-readably

If you run an auto-merge bot ("merge train") that drains clean+green PRs, these rules keep it from shipping work that a human still needs to sign off. The incident that taught each: a money/critical PR where the builder wrote "don't merge, needs independent review" **in the task** — the train merged it 38 minutes later and CI shipped it to prod, because the train reads labels, not comment prose.

**Rule 1 — one source of truth for gate labels.** All label sets live in ONE module (e.g. `fleet_gate_labels.py`: `SENSITIVE_LABELS`, `HUMAN_VERIFY_LABELS`, `GATED_LABELS`, `HOLD_LABELS`). Every gate imports from it; duplicating the literal is banned (a drift-guard test fails if a second copy appears). A duplicated list diverges silently and you only find out after something shipped.

**Rule 2 — "do not merge" is said with a label, not prose.** The train reads labels, not comment text: a "don't merge" comment does not exist to it. A builder has exactly two machine-readable ways to stop the train: mark the **PR draft**, or apply a **`HOLD_LABELS` label** (`hold` / `no-automerge` / `do-not-merge` / `wip` / `blocked` / `needs-review`). If you wrote "needs independent review" in the task, **apply `hold` to the PR in the same action** or it ships within the hour.

**Rule 3 — fail-closed by default.** The train HOLDS a PR on ANY ambiguity, not only on an explicit red. Absence of evidence ≠ evidence of health:

| Situation | Fail-open (wrong) | Fail-closed (right) |
|---|---|---|
| no API key for the gate | gate silently empty → merge | wrapper `exit 1`, do not merge |
| PR has **zero** check-runs | "no checks" = green → merge | if the repo emits checks at all → **HOLD** (CI never ran, usually a stale base; fix by rebasing on main). Only merge if the repo has no CI at all, else the train freezes the repo forever |
| check-runs API errored | `None` → fell through to merge | HOLD: "couldn't look" ≠ "looked and it's fine" |
| PR file list unreadable | `None` → migration unchecked, merge | HOLD |
| the task API errored **mid-scan** of sign-offs | silently skipped → gate reported "nothing blocked" | `GATE_DEGRADED` → **do not merge the whole cycle**, log the reason |

The last row is the subtlest: "I couldn't look" and "I looked, all clear" produced the same empty result. "checks=0" being read as "not red" shipped an unverified PR.

**Rule 4 — who presses merge on a security/money PR.** Independence is required of the **judgment**, not of the finger on the button. Pressing merge is mechanics; gating it on "only the author has rights" makes the author a bottleneck.
- A security/money PR **may be merged by its own author** IF the linked task carries a verify-verdict from a **different** agent — independent review happened, the button is irrelevant.
- Without another's verdict — do not merge: request review from any agent with read access; `hold` stays until the verdict.
- If literally no one but the author has access — that's a `❓ **Blocking @<operator>**` on the task, not a self-merge "on my own responsibility".

**Liveness check for any gate (mandatory after editing the train).** A gate that "degrades gracefully" is not a gate — it is silently dead, and from outside that is indistinguishable from "nothing to block":
```bash
grep -c "merge-gate" <merge-bot-log>            # 0 for all time = the gate never once ran
python3 <merge-train> --test                    # run the gate's own case suite (incl. money-without-human-verify)
diff <prod-copy> <git-copy>                      # prod copy ≠ git copy = "fixed" on paper only
```
Once, the prod copy and the git copy diverged for weeks: editing the repo changed nothing in prod. **Edit both, or make prod a symlink to the tracked file.**

**Symlink deploy vs copy deploy have DIFFERENT liveness checks — and `diff` is only valid for the copy case.** If prod is a **symlink** to the tracked source (the way to guarantee prod == git for a big mutable script), then `diff -q link target` is a **tautology — it compares a file with itself and can never fail.** The load-bearing checks are the other three:
```bash
test -L <prod-path>  || echo "SYMLINK GONE — prod is a mutable copy again"   # `mv` kills the symlink; cp/>>/sed -i/editors do not
[ "$(readlink -f <prod-path>)" = "<expected-target>" ] || echo "symlink points ELSEWHERE"
git -C <repo> diff --quiet -- <tracked-path> || echo "prod runs UNCOMMITTED code"  # prod IS the working copy now
```
The general rule: ask **"can this line ever return NO?"** before you trust it as a gate. `diff` over a live symlink cannot, so it proves nothing there.

The same "can it fail?" test applies to freshness checks: `stat -f %m <symlink>` on macOS reads the *symlink's* frozen mtime — use `stat -Lf %m` to follow it, or the "up-to-date, no-op" it prints is identical to what a healthy gate prints over a clean tree.

**Verifying host-local work.** A verify driver that only knows two probes — "is the PR merged?" and "does the live endpoint respond?" — cannot confirm a task whose deliverable is a **file or state on the fleet host** with no PR and no remote endpoint (measured: 0/14 such tasks ever confirmed, though 13/14 were genuinely done). "Couldn't confirm" meant "work is done, the probe is blind." So: classify by **probe outcome, not description heuristics** — if no PR link exists anywhere in the task blob AND no remotely-reachable URL (the task's own URL and RFC1918/loopback don't count), it's host-local. Let the executor opt in by naming concrete paths (`~/...`, `scripts/*.py`) or an `artifact:host-local` label; the parking comment then states a verdict about **the probe, not the work**.

### Post-deploy verification IS part of done — «merged»/«CI green» ≠ done (CRITICAL — false-done class, added 2026-06-18, the operator)

For ANY task that ships to a running service (UI / API / deploy / integration), **done requires a run against the LIVE DEPLOYED service**, not just merged + CI-green. «Merged» = code is in main; «CI passed» = it builds — **neither proves the feature works in prod.** The closing acceptance run is the deep-verify (above) against the deployed URL; for **user-facing** work that means an **authenticated browser scenario** — Casdoor login → exercise the real flow → zero `console.error`/`pageerror`/5xx → assert the DATA changed (the `e2e-harness` + `e2e-scenarios/*.md` convention, epic #d0682c39).
- **Enforced, author ≠ certifier:** for user-facing tasks the `review-verify-driver` independently RUNS the relevant authed browser scenario before allowing `review → done` (F2). Your own «tests pass» / pasted log is NOT sufficient — only a fresh independent run counts. A post-deploy smoke (`@smoke` Playwright, logged-in) is the literal «deploy is done» gate.
- **Scope (the operators nuance):** a typical low-risk dev **subtask** with no deploy surface (`delegation_level=auto`) legitimately closes on its own merge + CI + unit/`go test`/`pytest` evidence — this rule targets anything that **deploys or integrates**. When unsure whether a change is user-observable in prod, treat it as ship-to-service and post-deploy-verify before `done`.

### When a purely-manual criterion is genuinely unavoidable

Inherently human only: visual taste, subjective copy, a real-money/irreversible GO, a the operator-only credential. Then **(1)** label `kind:human-verify`; **(2)** split it off the blocking path — the agent-runnable part closes `done` on its own evidence, the human-verify slice is a **separate non-blocking task/subtask** (in `review` it's **expected**, not a stall). **Do NOT** stamp `kind:human-verify` to skip a real AC.

### `review` IS the hand-off — nothing is parked there (CRITICAL)

**Moving a task to `review` = "I'm done and I request independent verification."** That is the status's only meaning. A `review-verify-driver` should spawn an independent verifier on **any** agent task sitting in `review` past a threshold (e.g. 2h) — no stamps, markers, or magic phrases. (We once required one of four magic phrases in the text; measured, **0 of 27** otherwise-ready tasks had the stamp and the queue stalled for days. Opt-in-by-guessing-a-phrase was removed.)

What follows:
1. **You don't need to write anything to be seen.** An ordinary human comment ("PR#114 merged, CI green") is enough — the status suffices. An explicit `Verify: <agent>` is still valid and useful (it addresses a specific domain reviewer) but is no longer the condition for visibility.
2. **A task NOT ready for verification does not belong in `review`.** Waiting on the operator → human-gate / `Blocking @<operator>`. Needs a manual run → `kind:human-verify` as a separate non-blocking task (§1c). Work unfinished → move it back to `todo`. Otherwise the verifier arrives and rules on something unready — and it's right to: you declared it ready.
3. **A `DO-NOT-SHIP` verdict returns the task to `todo` — that IS the delivery channel.** Under a feeder, an agent is woken only by a task in `todo`; a comment or @-mention wakes no one (the one fleet-wide exception is the dispatcher-hosted coordinator — see `CLAUDE-communication.md` § "How @-mentions wake"). Disagree with the bounce → contest it in-thread with facts, don't silently redo.
4. **Auto-close does not happen "on prose".** The probe gate is fail-closed: without an independently-confirmed merged PR, a SHIP verdict is held as `BLOCKED` and the task stays in `review`. "I'll be closed by mistake" is not the risk; the real risk is the other direction.

---

## 1d. Prod host/domain/path → verify against servers.md BEFORE hardcoding (CRITICAL — wrong-model stall, added 2026-06-04)

**`servers.md`** (`~/Obsidian/Rogozhin/Devops/docs/servers.md`) is the source of truth for host identity — every prod host ↔ IP ↔ public domain(s) ↔ on-host layout ↔ service names.

**Hard rule — before hardcoding ANY of these into code/config/task/deploy-script/memory-drawer, look it up in `servers.md` first:** a **public domain/subdomain** (`*.example.com`/`*.example.com`/`*.svc.example.com`) — which host serves it and for **what** (web-publish vs WebSocket vs API = different vhosts on the same box); a **prod hostname/IP**; an **on-host path** or **service/container name**.

Fact **not in `servers.md`, or contradicts it** → do **not** guess. Either (a) verify against the live host (`ssh <host> 'ls / docker compose ps / cat Caddyfile'`) then **update `servers.md` same change**, or (b) can't verify → name it a blocker; never ship a guessed host to prod.

**Pre-merge guard:** `scripts/check-prod-hosts.sh` flags new hardcoded `*.example.com`/`*.example.com` refs in the diff before merge (modes → reference). New host → **add it to `servers.md`** same change. A host-dependent AC quotes the verified value (§1c).

### 1d-bis. A decommissioned host does not go quiet — it LIES (added 2026-07-12, Mesh `e69e21cb`)

**Spark:** verify **only** via the live API `https://app.example.com/api/v1/...`, or — when SQL is genuinely required — `source ~/.config/agents/vega-prod.env && ssh "$SPARK_DB_HOST" "docker exec $SPARK_DB_CONTAINER psql -U $SPARK_DB_USER -d $SPARK_DB_NAME …"` (`$SPARK_DB_USER` = read-only `spark_read`, DB-enforced — INSERT/UPDATE/DELETE/DDL fail by design). **NEVER `tw-billing`. NEVER hardcode `-U spark`** — that's the DB superuser; a copy-pasted `-U spark` example is one keystroke from an unaudited prod write (task #27a07d6b).

The failure this rule exists to prevent: after the Helsinki cutover (2026-07-09) the old `spark-postgres` on `tw-billing` stayed **up and healthy** and kept answering `psql` — with a pre-cutover snapshot. It never errored. Three agents (Atlas, Vega, Ember) independently "verified against prod" through it and got a **confidently wrong answer**; one escalated a P0 for fabricated data that never happened. Worse, the zombie backend on that box kept running its parsers and wrote **37 scouted assets into a DB nobody serves** — they never reached production. That stack is now stopped and the container renamed `spark-postgres-STALE-DO-NOT-USE`, so the old command fails loudly instead of lying.

**The generalisable rule — after ANY host/DB cutover, the old box is the most dangerous thing in the estate:**
- It answers. A stale-but-healthy datastore returns rows with **zero error signal**, and a green query reads exactly like a verified one. *Silent wrong data beats loud failure every time — for the attacker, and against you.*
- So **kill the old surface, don't just stop pointing at it**: stop the container, `--restart=no`, and **rename it** so the muscle-memory command dies with `No such container` rather than succeeding against a corpse.
- **Sweep every place that names the old host** in the same change — agent `CLAUDE.md`s, skills, access matrices, Grafana datasources, auto-memory. A decommissioned host that still lives in an instruction file will be queried again; docs are the fleet's muscle memory.
- Pin freshness to something that moves: `max(updated_at)` / a row written in the last hour. "The query returned rows" is not evidence the host is live.

**Moved a Postgres host? Recreate the lead roles and repoint the env — they do NOT travel** (added 2026-07-21, Mesh `1d060c5f`):
- **`pg_dump <db>` does not carry roles.** Roles live in `pg_authid`, a **cluster global**; only `pg_dumpall --globals-only` has them. The Helsinki migration moved the data and silently dropped `contenthub_read`, `evcbot_read`, `teamrelay_read` — and earlier `spark_read` (#890b9ba4) and `mesh_read` (#7f646f08). Five instances of one bug.
- **Losing a read-only role is FAIL-OPEN, so nothing reports it.** Consumers fall back to the superuser and keep working; read-only silently degrades from a DB-enforced guarantee to agent discipline. Ask of every control: *if this vanished, would anything break?* If no, it needs its own check.
- **Checklist on any host move:** (1) diff non-`pg_%` roles in `pg_authid` old vs new as an explicit acceptance criterion — green data checks say nothing about roles; (2) recreate missing roles with the **same grant shape** (`pg_read_all_data` vs scoped SELECT — see below); (3) repoint `~/.config/agents/secrets/lead-db-*.env` + add an ssh alias in the `mesh-host`/`spark-vm` style; (4) re-verify live through the env file, not by hand.
- **Grant shape is a security decision, not a detail.** `billing_read` is deliberately **scoped SELECT on the `billing` DB only**, NOT `pg_read_all_data` — its cluster also holds `blnk`/`hyperswitch_db`/`wallet_meta` (payments/PII), and `PUBLIC` has CONNECT on every DB by default. "Simplifying" it to `pg_read_all_data` on a future move would silently open financial reads.
- **Prove read-only with `has_table_privilege()`, never error text** — a write probe can fail on type coercion *before* the permission check, which looks like enforcement but proves nothing.

---

## 1h. Self-Verify-with-Rollback — the autonomy gate (act vs escalate) (CRITICAL, added 2026-06-04)

Act autonomously on anything **reversible** or **rule-governed** having captured a rollback first; escalate ONLY for the narrow set genuinely irreversible AND outside the rules. **Reversibility is the license to act — not the operators approval.**

### Destructive / consequential technical actions → Self-Verify-with-Rollback (autonomous, ZERO pings)
For any deploy, DB migration, `drop`/`delete`, force-push, image swap, file overwrite, branch reset:
1. **Classify reversibility** — recovery path? (git, `pg_dump`, snapshot, image tag, file copy)
2. **Capture the rollback anchor FIRST, before acting:** `git tag/branch` before reset/force-push; `pg_dump` before drop/migration; `docker tag …:backup` before image swap; copy before overwrite.
3. **Act**, then **verify** on the live target — curl/health/query/test (§1c).
4. **Keep the rollback 24–72h** + log the exact rollback command in a task comment.
5. **No recovery path?** **Manufacture** one (backup → now reversible → proceed). Escalate only if a backup is genuinely impossible (rare).

Everything runs in Git + backups → almost nothing technical is truly irreversible → do them **autonomously**.

### External communication
- **Rule-governed sends → autonomous.** Outreach per an approved playbook/template, standard rule-covered replies — **the rule IS the approval.** Match, send, log. No ping.
- **Non-standard / bespoke → approval (§0b `❓ Blocking @operator`).** A reply to an inbound, a judgment-call outside the rules, anything to a real person not covered by a playbook → STOP, draft, get the operators approval (can't unsend — the one genuinely-irreversible class).

**Money — N/A:** agents have **no payment instruments** (billing receive-only) — never block/ping on a "spend approval" that cannot occur. **Net:** the operator is pinged ONLY for (a) non-standard external comms needing a judgment reply, (b) the rare destructive op with no possible backup.

---

## 2. Verify After Create (CRITICAL)

**Hard rule — after ANY `create_task` / `create_subtask` / `move_task`:** extract `id` (full UUID, not a short prefix) → **IMMEDIATELY** `mcp__evc-mesh__get_task(task_id=<full_uuid>)` to verify it really exists → only AFTER a verified `get_task` mention it in the report (short prefix for readability; record the **full UUID** in memory drawers / task refs).

Same for **cron** (`CronCreate`): after creation `CronList`/`CronGet(<job_id>)` to verify before reporting. CronCreate lives only inside the current session — long-running monitoring → launchd / macOS cron, **not** CronCreate.

---

## 4. Mesh Workflow Hygiene

**Before `create_task`:** `list_tasks(project_id=...)` — no existing task with the same title/scope (duplicate is an anti-pattern); exists → add a comment/dependency instead.

**`create_task` fields:** **assignee** = explicit agent_id (UUID from team_directory), not a slug; **labels** = project/scope (`obsidian`/`spark`/`mesh`/`infra`/`urgent`); **description** = acceptance criteria, **agent-runnable wherever possible** (not "the operator will look" §1c; genuinely human-only → `kind:human-verify` + split off the blocking path); **priority** explicit (not default); **due_date** if time-pressure.

**After:** verify via `get_task` (§2); depends on another task → `add_dependency`; concrete deliverable → `upload_artifact`; progress → `add_comment`. **`move_task`/`update_task`:** status change = mandatory `add_comment` with reasoning; don't close `done` without checking acceptance criteria; disputable → `review` for the project lead.

---

## 6. Anti-patterns Summary (Don'ts)

Recurring violations (each defined in its rule): dup "quality analysis" on a short prompt (§1); report a task_id unverified by get_task, or CronCreate for long-running monitoring (§2); dup tasks without a `list_tasks` check (§4); work/commit without `git fetch` + safe sync or `push --force` over remote (§1a); `echo … >> outbox.jsonl` / `tg-reply` with backticks/`$()` for multi-line → loss/corruption; "Done" without verifying artefact/commit/task; AC = "the operator will look" as the SOLE gate (§1c); umbrella/captain task without `mesh-dedup-check.py` first (§4a); ship/deploy `done` on "merged" without pasted prod-verification, or deploy-as-PROSE-in-a-comment (§1f); `gh pr merge` on an `your-org` PR (SSO-401 → silent queue jam), or the bare `gh api PUT …/merge` (reads no labels → merges past `hold`/draft; use `~/bin/gh-merge` §1b); mentioning Claude/AI/MCP in operator replies; shipping visible product copy without the operator-approval, merging past a DoD that names a visual/human gate, or coding in a shared repo without a  dedup-check first (§1r). (Full annotated list → reference.)

---

## 7. Patterns Summary (Do's)

Canonical good moves (each defined in its rule): `git fetch` + safe `pull --ff-only`/`--rebase` FIRST on any repo-task, stop+escalate if no safe path (§1a); state check first → targeted reply; create → verify get_task → report with short_id; long-running cron → launchd plist not CronCreate; dup tasks → comment/dependency; umbrella/captain → `mesh-dedup-check.py` BEFORE create_task (§4a); multi-line reply → `~/bin/tg-reply <id> <<'EOF' … EOF`; agent-runnable AC before delegating (§1c); ship/deploy = named-owner sub-task OR fail-closed CI-on-merge, «done» only after live+verified (§1f); merge org PRs via `~/bin/gh-merge <n> your-org/<repo>` (gated on `hold`/draft, fail-closed — never the bare `gh api PUT`), migration-version-ordered (§1b); self-debrief in a memory drawer after significant ops. (Full list → reference.)

---

## Edge-case index — when to read the reference (MANDATORY on match)

Situational/incident rules live in `~/ClaudeCowork/bob/CLAUDE-workflow-reference.md` — they are AS BINDING as this file; read the matching section BEFORE acting when your situation matches:

| Your situation | Read reference § |
|---|---|
| Creating a Mesh task (which status?) / the operator-assigned task | §0a, §0c |
| @-mention in a comment, blocking someone | §0b |
| Task sits in review >24h | §0d |
| Decomposing: subtasks/parent exist | §0e, §4a (dedup gate BEFORE create) |
| When/what to write to mempalace mid-session | §0f |
| Triage comment format / in_progress & comment cadence | §0g (+core §4) |
| delegation_level semantics (auto/review/supervised) | §0k |
| canonical updates fetch (ACP step 6) | §0l |
| Passive-wait / monitor / verify-window task | §0m |
| Blocked on another task/PR | §0n |
| Public/published repo work — premise gate | §1e |
| Deploy ownership (task vs prose) | §1f |
| After your PR merges (worktree hygiene) | §1g |
| Big mechanical multi-file edit | §1i |
| About to ask the operator a question | §1j (verify premise yourself first) |
| /sweep, /status handlers (orchestrators) | §3a, §3 |
| Recurring/audit agent: where to route findings | §8 + Acceptance template |
| Subagent orchestration patterns (schemas/SoM/captain) | §9, §10, §11 |



## §1k Visual tasks — mandatory screenshot comparison

Any task that changes UI (layout, redesign, components, landing pages):
1. **Do NOT move to review/done without screenshots.** Minimum: desktop 1440 + mobile 393, light (+dark if supported), attached to the task as artifacts.
2. **A screenshot is a one-liner, not a quest:** wrap your headless browser in a `page-shot <url> [out.png] [--dark|--mobile]` helper so there are no excuses (three redesigns once failed because taking a screenshot required 20 minutes of CORS/proxy/chromium debugging).
3. **Compare against the reference:** if the task has a design reference (mockup, artifact, link), place the screenshot NEXT to it and list the discrepancies explicitly. "Looks close" is not a verdict; a list of discrepancies is.
4. A UI/redesign PR does NOT merge without the visual gate (screenshots + human sign-off) — auto-merge is disallowed for it.


## §1m Recall before work

Before working a task (after checkout, before the first action) — a **mandatory recall** against your memory store: `recall(query='<3-5 task keywords>')`. If your memory is graph-backed it returns not just keyword hits but the past decisions / incidents / gotchas *related* to this work. It's cheap (one call) and removes "derive context from scratch" and drift.
- Do NOT block or loop on an empty result — an empty layer is fine, just proceed.
- The point is to raise read-frequency: a fleet that writes memory 2× more than it reads it is re-deriving what it already knows.


## §1o Mocking convention — only at TRUE external boundaries

Agents over-mock (industry studies put agent mock-rate well above humans') and rewrite tests to pass, mostly because "what to mock" is left to taste. This rule removes the discretion.

**Mock ONLY at TRUE external boundaries.** Everything else runs real code.

| ✅ MOCK | ❌ DON'T MOCK |
|---------|-----------|
| PSP / Wallet API (real money / billing calls) | the DB layer inside a service (real Postgres/SQLite in tests) |
| outbound network calls (HTTP to third parties, S3/MinIO) | a saga orchestrator (test the real step, not a stub) |
| system time (`datetime.now`, `time.Now()`) — for determinism only | a webhook handler inside your own monolith |
| external auth providers (in unit tests) | internal services (calls to your own API in a service's unit tests) |
| email/SMS/push (don't actually send) | repositories / DAO methods (use in-memory SQLite or a test DB) |

**The test for "TRUE external boundary":** *"If I replace the mock with the real implementation, does what we're testing change?"* If yes — the boundary is external, the mock is justified. If no — remove the mock, test for real. A boundary is external if the test SHOULD NOT fail when that component changes/breaks (it's not our contract); if we own the code on both sides, it's internal and must run real.

**Anti-patterns (banned):** mocking business logic that calls an already-stubbable external (if the wallet has an in-memory stub mode, don't `mock.patch` it); mocking a repository/DAO "to avoid the DB" (use a test DB); mocking a function of the very module under test (circular mock — the test checks nothing); a mock with no `# mock: external boundary — <reason>` comment when the boundary isn't obvious.

## §1n User-facing tasks — passing authed browser scenario before done

Closes the false-done gap: a page returns 200 while a component 500s inside it, or a filter is silently empty. **HTTP 200 is NOT proof.** Proof = an independent authenticated browser run that passes green with **behavioral** assertions.

A task touching a **user-facing feature** (label `ui`/`frontend`, product = web, or it references an e2e scenario doc) does NOT move to `done` until all of:
1. **The scenario exists as a doc** in the product repo (Given/When/Then, owner-editable). No scenario doc → no generated browser spec.
2. **The scenario is run by an independent verifier** (fresh context, NOT the code's author) through a browser MCP under a real logged-in session, capturing console/pageerror/5xx, and passes **green**.
3. **Behavioral assertions, not presence:** after the action, assert the data/DOM actually changed (row-count/content), not just `toBeVisible()`. "No error element" is NOT an assertion — that's exactly the silent-empty-filter hole.
4. **"Tests passed" from the code's author is NOT proof.** Author ≠ certifier; only a fresh independent run counts. The closing comment quotes the scenario name + run output.

Relationship: §1k (screenshots) covers layout; §1n covers behavior/function. Both are mandatory on user-facing work.


## §1r Standing fleet rules

Each rule below came from an incident — each cost a rollback, a wasted session, or a deletion from prod.

### A. Only the operator changes visible text

**Visible product copy changes ONLY with the operator's explicit consent.**
- **Gated:** headings, labels, button/link text, section descriptions, empty states, onboarding/helper text, FAQ, marketing prose, user-facing error/validation text.
- **Not gated:** `<title>`, meta description, canonical/noindex, JSON-LD, `alt`/`aria-label`, `title=` tooltips on existing controls. The line: the user reads it as the product's voice → gated; it exists for crawlers and screen readers → not.
- **The task description is NOT approval.** Even if the text is written into the acceptance criteria, that's the agent's proposal. Approval = the operator saw THIS text and said "yes".
- **An audit produces a request, not a work order.** An SEO/UX finding "this page needs copy" → ship the technical part, send the words to the operator as a proposal (`triage` / `Blocking @<operator>`), not as a merged diff.
- Same for nav/footer links: new top-line and footer links only by agreement.

### B. Shared repository — first look at what's already there

- **Before the first line of code** on a freshly-received task in a multi-owner repo: `gh pr list --repo <org>/<repo> --search "<keywords>" --state all` + `git fetch && git log --oneline origin/main -5`. A PR or commit already exists → don't rebuild, pivot to independent verification. The dedup check is 5 seconds; a duplicate build is a wasted session (two PRs with an identical fix, one minute apart).
- **A cleanup task "remove / rename X":** the file list in the description is a baseline, not the full list. `grep -ri "<string>"` the whole repo and remove every occurrence (once, the description listed 4 files; the 5th surfaced in prod after merge).

### C. Before escalating — check yourself

- **"Need prod access / hard-wait / cannot run on prod"** → first check your own `~/.ssh/config` and memory. Access exists → do it yourself. Genuinely absent → say specifically "no key to X", don't just forward someone else's ask (once, five re-fired escalations over access that had already been granted).
- **Status of an external account (Reddit / GitHub / …) — check from a clean network or incognito.** A 403 from your proxied IP is about your IP, not the account. Don't substitute the signal (once, a "shadowban" diagnosis that was really a hard ban, a day lost).

### D. Task status lives in the last comments

Before ANY status report and before ANY disposition (`move_task` to backlog/done/cancelled, "blocked", "waiting on the operator") — `get_task(id, include_comments=true)` and read the last 2-3 comments. The description is static; the blocker and its removal live in a comment. "A quiet parked task" ≠ "nothing to read".

### E. Merge discipline

- **CI-green ≠ acceptance.** A UI / mobile / redesign PR does not merge on the CI signal — it needs the visual gate (§1k: 1440 + 393 screenshots, comparison to the mockup, operator sign-off).
- **When unjamming a stuck merge queue — inspect each PR's DoD separately.** Has a "screenshot / sign-off / mockup / human-verify" requirement → don't merge it in the batch, leave it to the gate owner. Backend / bugfix / infra with an agent-runnable DoD — mergeable.
- **The reverse is also a rule:** a trivial landing-page cleanup can be squash-merged + deployed + probed **without a separate GO** if the operator has granted a standing deploy mandate — BUT that mandate removes the wait-for-GO on *deploying*, it does NOT lift §1r.A: new or changed visible text still needs the operator's approval first. Without a GO you ship only what has agreed-or-unchanged words (removing a block, layout, broken links), never redesigns, new sections, feature removals, or changes with backend implications.

### E2. Work does not exist until it's in GitHub

- **Strict order: commit → push branch → PR → and only THEN deploy.** Before any deploy: `git branch -r --contains HEAD` — empty means the commit exists only on this machine and must not be deployed.
- **Do NOT switch branches in a working copy that lives on a server.** The prod copy sits on its own branch. Need a different version → your own worktree or a built image, never `git checkout` in the live directory: build from a feature branch, then return to `main`, and the edits vanish with no error and no warning.
- **Updating someone else's / a partner instance — first check for un-pushed work there:** `git status --short` (uncommitted), `git log --oneline @{u}..` (un-pushed), `git reflog -20` (traces of others' builds). Found something → stop, first `git push origin HEAD:refs/heads/rescue/<description>`.
- **A partner instance running your product** is the same code, one `main`, two deployments: branch from `origin/main` → a normal PR → both instances. Deploying to the partner ahead of the merge is fine (their users are waiting) — that's a deployment decision, it does not cancel the push.
- **Why:** an agent once fixed a partner instance following an instruction "branch from their deployed commit, build, deploy" — which had no "push" step. It would have lost 11 branches (~22 commits) on the fleet host plus 22 files / 790 lines uncommitted right in the server's working copy, including a feature with a migration. Two sides were clobbering: one built past GitHub, the other re-deployed the same host from `main` three times. No one was careless — with that scheme the loss was a matter of time.

### F. Product ownership

Independent products have their own agents (dev + PO). Their GitHub work, releases, and community monitoring are done by their hands under their accounts. Don't hold their tasks yourself and don't submit for them from your account ("these are separate agents, not your GitHub account").

### G. External channels — we are ordinary people, not agents

GitHub issues/discussions, email, Reddit, Twitter, other people's Slack: an outside observer must see **a human from the team**.
- **Forbidden in any phrasing**, including implicit ("our system picked this up"): orchestrator / agent / dispatched / handover / "picked up from X" / dispatcher / triage / the coordinator's name / Claude / AI / bot / model.
- **Signoff = one real account name**, stable for the whole thread (e.g. "— Robert, Acme"). The task moving to another agent does NOT change the signoff mid-thread.
- **Edit-after-publish doesn't save you:** GitHub and email send the original body the same second. Grep the text for forbidden words BEFORE sending.

### H. Task-tool gotchas

- **File a follow-up from a recurring audit via `create_task`, not `create_subtask`.** A subtask is a real parent-child link: governance won't let you close the parent while any subtask is open, and there's no way to detach it. Reference the source in text instead: "Source: <audit> `#<id>`".
- **`remember` — sequential only.** Parallel calls return 502.
