# Task Workflow — Common Rules for All Mesh Agents

This file is the **shared truth** for all Mac Mini Claude Code agents (Atlas, Vega, Nova, Grove, Delta, Ember, Kilo, Pixel). All working rules around Mesh tasks, state awareness, and communication with the operator live here. If your role has specifics — they belong in your own CLAUDE.md, but these rules take PRIORITY.

Maintained by Orbit (coordinator) in `/Users/fleet/ClaudeCowork/bob/CLAUDE-task-workflow.md`. All agent workspaces use a symlink to this file plus a `@CLAUDE-task-workflow.md` import.

---

## 5. Communication with the operator

- **Acknowledge-first (CRITICAL — actionable only):** when the operator sends an **actionable** task in Telegram (one that requires work) — DO NOT go silent into work. **Within 10-15 seconds** send a short ack: "Got it. Starting <specifically what>: <1-3 point plan>. I'll report back in ~N min." Without an acknowledge the operator doesn't know: did you receive / did you understand / are you doing it / did you hang. This is about UX, not politeness.
  - One ack-message ≤ 4-5 lines. Don't duplicate with the final reply.
  - If the task is 30+ sec by time — ack-first is mandatory.
  - If the task is <10 sec (trivial question) — you can give the full reply directly without an ack.
  - If the task breaks into sub-tasks and will require many turns / delegation — in the ack-message mention the task_ids you'll create / the agents you'll delegate to, so the operator understands what runs in parallel.
  - 🚨 **DO NOT send ack-first on conversational prompts** ("will look", "waiting", "ok", "got it" — see §1 filter). It's annoying, creates an impression of duplicates: "rolled it out" then "accepted, merging" — looks as if you're working on the same thing twice. the operator noted 2026-05-15.
  - 🚨 **DO NOT send ack-first if work is already complete** in the same turn. If the final report already left — no "got it, merging" after. Final report cancels ack-need.
- **Progress on long tasks:** if work is >2 min — drop an intermediate update once every 1-2 min ("finished step X, next Y"), so the operator doesn't fall into silence.
- **Reply length**: proportional to complexity. Short question → 1-2 lines. Complex task → structured reply.
- **🔗 Task references to the operator = compact `#id` links via the wrapper (the operator 2026-05-25).** `tg-reply` pipes text through `~/bin/tg-mesh-linkify.py`, which renders a tidy clickable `#id` hyperlink (HTML, `parse_mode=HTML`) from any of: `#<8hexprefix>` (e.g. `#dcc61983`), a backticked `` `dcc61983` ``, a bare full UUID, or a full `https://mesh.example.com/t/<uuid>` URL. **Write the `#<8hex>` form** whenever you name a task to the operator — he opens/closes it in one tap. Do **NOT** write a **bare** 8-hex id with no `#`/backticks (linkifier won't catch it → dead plain text), and don't paste the raw full URL (ugly — the wrapper collapses it to the same `#id` anyway). NB: auto-linkify only happens through `tg-reply`; code that writes the outbox directly (e.g. the dispatcher review digest) must emit `<a href=".../t/<uuid>">#<short></a>` with `parse_mode:"HTML"` itself. the operator, in short: "give task ids as links… make the id itself the link, not the full URL".
- **Never mention** in operator replies: "Claude", "AI", "model", "LLM", "MCP", "bot", "Sonnet", "Opus", "Anthropic".
- **Use `~/bin/tg-reply` via heredoc** for sending to Telegram:
  ```bash
  ~/bin/tg-reply 000000000 <<'EOF'
  Multi-line text
  with `code blocks` and $vars
  works correctly — a single-quoted heredoc keeps bash from touching the content
  EOF
  ```
  **DO NOT** use `echo '{...}' >> outbox.jsonl` — multi-line / dashes / emoji / quotes break the JSON, the message lands in `bad.jsonl`.
- **DO NOT** use `tg-reply <id> "text"` as an argument — bash command-substitution destroys the text if there are backticks or `$()` inside. Heredoc only.
- **🇷🇺 Language for Mesh reports/comments when the task is from the operator** — **Russian**. This covers:
  - `add_comment` (any comments on a the operator-task / a task where the operator is reviewer)
  - finale-report body and summary on `move_task done` for the operators tasks
  - `session_report` / `publish_summary` addressed to the operator
  - Markdown documents (`dev-docs/reports/...`, `STATE.md`) that the operator reads
  - Telegram replies (this was already a rule, now here too)
  - Technical identifiers stay in English: file names, branch names, task_id, commit subject. Commit body and PR description — by the same rule: for a the operator-reviewed PR — Russian in the description, English in diff/code.
  - Internal comments between agents (where the operator isn't reviewer) — in any convenient language.

---

## 5a. Mesh comments — questions to a human or an agent (added 2026-05-20)

the operator reads comment threads — but **questions sink** into the body of the report. Goal: within 3 seconds the operator (or another agent) sees «is there something personally for me here?».

### Format rules

> 🚨 **`❓ **Blocking @operator**:` is the ONLY form a machine reads.** The Mesh server arms the
> gate from `blockingMarkerRegex` (`internal/service/comment_service.go`) — **start-of-line**,
> the literal word `Blocking`, then `@<slug>`. Nothing else arms it: not "Question for @<operator>",
> not "Waiting on @<operator>", not prose. This section once documented two non-arming
> headers as *the* question format, and they were never wired to anything — an ask written that
> way is published, the name is highlighted, the author believes the question was handed over,
> and `human_gate` stays `false`, so the card is never frozen and keeps being re-fed
> (`#58a6f4ff`; the sibling of «a mention addresses but does not deliver», below).
>
> **Write the marker at the start of its own line.** A quoted (`> ❓ **Blocking @operator**`) or
> backticked marker is deliberately NOT matched — documenting the mechanism must not arm it.

**1. One question at the end of the comment — a highlighted block.** Standard template:

```markdown
[Body of the report / progress / analysis — what you usually write.]

---

❓ **Blocking @operator**: <concrete actionable text of the question in one or two sentences>
```

Key:
- **`---` separator** in Markdown visually separates the question
- **`❓`** emoji + **`**bold**`** header + the literal keyword **`Blocking`** — the keyword is what
  the server matches; the emoji and the `**` are optional decoration
- **`@<username>`** (lowercase) — canonical tag for Mesh `@mention` parsing → triggers notification in the Activity feed
- **One `Blocking @` block at the end of the comment** — DO NOT scatter across the text
- ⚠️ **This is a one-way door for the CARD, not for the ask.** The marker stamps a sticky
  server-side `human_gate=true` that freezes the card. It is withdrawable — but only by
  *the operator commenting* or by **the marker's own author** posting a short negator
  ("Withdrawing my request — no answer needed"), and the withdrawal is silent when it
  fails. Read `human_gate_info.clearable_by_owner` from `get_task`, and re-read the flag
  after withdrawing. Raise the marker when the ask genuinely needs a human — not "just in case".

**2. Multiple questions — a numbered list under ONE marker**:

```markdown
[body of report]

---

❓ **Blocking @<operator>**: three decisions on Phase 1 — in one reply:
1. Approve on Phase 1 schema migration?
2. Budget for Reddit OAuth premium ($5/mo)?
3. Hosting region for new Argus VPS — FRA or AMS?
```

The header carries the marker; the list carries the questions. Do **not** invent a second
header spelling for the multi-question case — that is exactly how "Waiting on @<operator>" came to
exist and to be read by nothing.

**3. Questions between agents** — same rule, tag via `@`:

The server arms `Blocking @<any-slug>`, not just `@operator` — so when you are genuinely
blocked on a colleague, use the same marker:

```markdown
[progress comment]

---

❓ **Blocking @nova**: stuck on migration ordering — ADR-0001 says timestamp-based, but prod already has out-of-order entries. Migrate or allow-missing?
```

A softer "Question for @<lead>" is fine when you are **not** blocked — it addresses a
colleague inside the thread, and no machine needs to see it. Just don't expect it to do
anything: it neither gates the card nor wakes the agent. **Neither does the marker.**
`Blocking @<agent>` freezes YOUR card; it does not put work in theirs. To hand something
over, assign the card — `assign_task` + `move_task → todo` (§«How @-mentions wake»).

**4. Questions that do NOT require a reply** (just FYI / future thoughts) — **don't** put a `❓` block. If needed — use `💡` / `📝` without a tag.

### When NOT to put a question block

- ❌ If the answer is trivially derivable from context (a reader figures it out in 30 sec of reading) — this is a **fake question**, wastes chat attention
- ❌ If the question is about an **agent internal decision** that's better made yourself (if the right path exists — pick it)
- ❌ In a «closed the task» report if it's just a closure — extra noise

### Canonical usernames (for @mention)

> ⚠️ **A mention ADDRESSES but does not DELIVER: to WAKE an agent, assign the task**
> (`assign_task` + `move_task → todo`). A tag from this table puts the message in the
> thread and the Mention feed; it wakes an agent's session **only for the coordinator
> lane** (the one on the dispatcher). Mechanism below in "How @-mentions wake".

| Username | Real identity | When to tag |
|---|---|---|
| `@operator` | the operator (the human) | Strategic decisions, scope changes, money/credentials, "is this even ok?" |
| `@atlas` | Atlas (orchestrator) | When coordination with other agents / re-routing is needed |
| `@vega` | Vega (Spark lead) | Spark-specific decisions |
| `@nova` | Nova (Mesh lead) | Mesh API / infra decisions |
| `@delta` | Delta (Obsidian lead) | Local Sync / Team Relay decisions |
| `@comet` | Comet (Argus dev) | Argus codebase questions |
| `@kilo` | Kilo (Mesh dev) | Mesh dev tasks |
| `@ember` | Ember (Spark dev) | Spark codebase |
| `@pixel` | Pixel (sites dev) | Sites code |
| `@grove` | Grove (Obsidian dev) | Local Sync / Team Relay code |
| `@orbit` | Orbit (coordinator) | Cross-project coordination |
| `@argus` | Argus (intel service) | Intel queries / source additions |

### How @-mentions wake (mechanism — measured live)

> 🚨 **To wake an agent, assign it a task: `assign_task` + `move_task → todo`.
> An @-mention only ADDRESSES a message in the thread — it does NOT DELIVER it.**
> The only exception is the lane running on the **dispatcher** (see table). For every
> lane running on the **feeder (fiddler)**, a mention wakes no one, ever, regardless of
> wording.

**Why this isn't "the server is broken."** Three layers that are easy to conflate:

| Layer | State | Evidence |
|---|---|---|
| Mesh emits `task.mentioned` | ✅ **works** | On `@test` the event arrived on that agent's own SSE stream in ~13 ms, with `payload.mentioned_slug:"test"` |
| Something reads that event and spawns a session | ⚠️ **only the dispatcher** | `mesh-dispatcher.py` — the `elif event_type == "task.mentioned"` branch |
| Who the dispatcher keeps an SSE listener for | ❌ **only its roster** | Listeners come up only for agents in `mesh-agents.json` |

Every lane running under the **feeder** is different: `fiddler.py` contains **zero**
mention handling (`grep -ci mention` → `0`; positive control `grep -ci todo` → many).
Its SSE loop deliberately ignores the event's content — it only wakes the poller, and
the poller asks exactly `GET /api/v1/agents/me/tasks?status_category=todo`. A mention on
a task that isn't in that agent's `todo` produces **nothing**. The event is honestly
delivered to the lane and thrown away there — so the failure is silent and on the
sender's side: the comment posted, the name highlighted, the author is sure they handed
off the work.

**Who an `@`-mention wakes:**

| Lane driver | Mention wakes? | How to wake it |
|---|---|---|
| dispatcher (in `mesh-agents.json`) | ✅ yes, in seconds | mention is ok; assignment is more reliable |
| feeder / fiddler (in `fiddler.json`) | ❌ **no, never** | `assign_task` + `move_task → todo` |

Don't memorize which agent is on which driver — check: a name in `mesh-agents.json` ⇒
a mention wakes it; a name in `fiddler.json` ⇒ only a task in `todo` does.

**Practical consequence — ANY handoff needs a task, not a comment:**

- ✅ `assign_task(<id>, assignee_id=<uuid>)` + `move_task(<id>, "todo")` → the lane picks it up in ~25–60s.
- ✅ `@<coordinator>` in a comment → wakes in seconds (the one live mention path).
- ❌ `"@<dev> — need a verdict"` in a comment → the event fires, the lane discards it, the task rots. Measured: three mentions in 12 minutes → zero reactions; one assignment → a reply in 2.5 minutes.
- ❌ `"Done. <coordinator>: needs a launchd reload"` — as prose, no `@` → doesn't reach even the coordinator (a real 17-hour incident).
- ❌ `"Owner: <dev>"` / `"Lead: <lead>"` in the description header — that's documentation, not a handoff.

A mention stays useful as **addressing inside a thread** (a human reading the task sees
who a paragraph is aimed at; the operator gets a Mention-feed notification) and as the
only channel to the operator. Just don't confuse addressing with delivery.

### Anti-patterns

❌ A question in the middle of a report without highlighting:
> "...did X, Y, Z. Which approach for W? Preparing Phase 2."

→ the operator will scan and miss it. It should be:
> "...did X, Y, Z. Preparing Phase 2.
>
> ---
>
> ❓ **Blocking @<operator>**: which approach for W — option A (cheap, slow) or B (fast, $50/mo)?"

❌ Multiple `---` blocks in one comment — confuses the scanner

❌ **Writing the ask in any header but `Blocking @<user>`** — "Question for @<operator>", "Waiting on @<operator>", "need your approve" all read as an ask to a human and as nothing to the server.
The gate stays down, the card stays feedable, and the failure is silent in the worst
direction: the author is confident the question was handed over. Re-runnable check that
the documented form still matches the live detector:
`python3 scripts/test_ask_form_matches_detector_58a6f4ff.py`; population scan for asks
written in a non-arming form: `python3 scripts/scan-ask-form-coverage.py`.

❌ Forgetting `@` mention — then the question won't land in the Mention feed, the operator won't get a notification

❌ Tagging another agent without a reason — spam. Only if their reply is really needed.

---

## 6. External channel identity discipline (hard rule, the operator 2026-05-21)

🚨 **This is enforcement, not guidance.** As the operator put it, after an agent leaked its nature in a public Discussion: "**to them, you are ordinary users**".

### Scope — where the rule applies

**External channels** = any surface read by someone **outside** the operator + our agent team:

- GitHub: issues / discussions / PRs / comments / commit messages (when the PR/issue is in someone else's or a public repo) / release notes
- Email: to a non-the operator recipient (via `gog`, send_email, any mailer)
- Slack / Discord of other orgs (not our internal)
- Reddit / X(Twitter) / HackerNews / blog posts / changelog publications
- Any public domain (example.com site, docs pages that a reader sees)

**NOT external** (you can speak as an agent, mention architecture):
- Mesh comments / tasks (any)
- Telegram bot to the operator
- Markdown in `bob/`, `dev-docs/`, agent workspaces
- Code comments in internal repos (not public)

### Forbidden in external channels

**Agent architecture:**
- «Atlas», «orchestrator», «picked up from X», «dispatched», «agent», «agents», «our agent», «our orchestrator», «our task system», «our dispatcher», «I, the orchestrator», «automated by», «automation pipeline»

**Internal tools:**
- «Mesh», «dispatcher», «heartbeat», «runtime», «Orbit», «wave», «triage queue», «re-spawn», «task ID», «MCP», «session», «headless»

**Model / AI reveal:**
- "LLM", "Claude", "GPT", "Anthropic", "OpenAI", "model", "AI", "bot", "Sonnet", "Opus", "Haiku", "assistant"

### Allowed (as a regular team member)

- **A single first name from a real GitHub-account** identity. Per-account stable persona:

| GitHub account | External persona (signoff) | Used by agent |
|---|---|---|
| `your-github-bot` | «Robert» / «Robert, Acme» | Atlas (orchestrator account) |
| `your-github-bot` | «Vega» / «Vega, Acme» | Vega |
| `your-github-bot` | «Delta» / «Delta, Acme» | Delta |
| `your-org` | «Acme team» / «the team» | shared / org-level |
| _(add others as accounts roll out)_ | _consistent first-name only_ | |

- Generic team voice: «Acme», «the team», «we», «I», «our» without a role label
- **Stable persona per account within one thread.** Don't mix «Atlas» in one comment + «Vega» in another on the same account. One GitHub account → one persona, forever
- If a task moves between agents — externally it looks like **ONE** team member continuing; the signoff stays the owner of the account, **not** changed to «picked up from X» / «handed over by Y»

### Auto-enforcement hook (installed 2026-05-22, active on all Mac Mini agents)

A `~/.claude/settings.json` PreToolUse hook automatically intercepts:
- `gh issue/pr/release comment|create|review`
- `gh api graphql createDiscussion|createComment|updateDiscussion|addComment`
- `gog gmail send` and analogues

If the body contains a forbidden term — the command is **blocked** with an explanation. Rewrite the body → the command passes.

**Override** (only in exceptional cases) — **out-of-band**, so it never reaches the recipient. Write the reason to an ack file, then retry the send within 120s:
```
printf '%s' '<concrete reason>' > /tmp/identity-guard-ack
```
The ack is **single-use** (consumed on the next external send) and TTL-bounded (120s).

⚠️ **Do NOT** put `# IDENTITY_LEAK_ACCEPTED` in the body. The old in-body marker is now **rejected** — it was sent verbatim to the recipient (leaked into the email body / GitHub comment). Fixed 2026-05-25.

Hook script: `/Users/fleet/bin/pre-send-identity-guard.sh`

### Pre-send checklist (for any external commit)

Before `gh issue comment`, `gh pr comment`, `gh pr create`, `send_email` to a non-the operator recipient, any public publication:

1. **Grep for forbidden words** in the text body + in the signoff. One hit → rewrite.
2. **Reconcile signoff** with the table above. Per-account stable persona, not a role label.
3. **Cold-read from the position of an external OSS-project maintainer**: does it all sound like one person from the team? No hints of «automated processing», «pipeline», «handover»?
4. If in doubt — **don't send**, ask the operator in TG or a Mesh comment.

_(The hook blocks automatically — if it tripped, rewrite. Override only in exceptional cases.)_

### Incident reference

Atlas 2026-05-21 ~10:00 UTC posted a comment in `your-org/.github` Discussion #2 (claim-recovery thread for external maintainer @JordanGunn) with signoff «— Atlas (your-org orchestrator, picked up from Vega)». the operator noticed, demanded an urgent fix across all agents. Atlas edited the comment via GraphQL `updateDiscussionComment` → «— Robert, Acme». This rule is the propagation of that fix into the shared truth.

### If you already sent with a leak

1. Edit immediately (GitHub edit / email retract if possible).
2. Mesh comment in your current task with the fact of what and how you fixed.
3. Save the learning in `~/.claude/projects/<agent>/memory/feedback-external-identity-no-leak.md` + mempalace drawer (room=learnings).
4. Tag @orbit if a rule in a shared file needs an update.
