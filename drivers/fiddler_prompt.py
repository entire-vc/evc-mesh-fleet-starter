"""fiddler_prompt — shared prompt templates for the fiddler (P2-3).

Single source of truth for everything the fiddler pastes into agent sessions.
The dispatcher's spawn preamble (~/bin/mesh-dispatcher, build inside
dispatch_claude) is the UPSTREAM of the full preamble here — invariant 2 forbids
modifying the live dispatcher pre-cutover, so this module holds a synced copy and
`fiddler doctor` compares DISPATCHER_PREAMBLE_SHA against the live file to catch
drift. After full cutover, the dispatcher should import from here instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# sha256 of the dispatcher's preamble block ("MANDATORY FIRST STEP" …
# "Follow CLAUDE.md workflow") at the time this module was last synced with it.
# `fiddler doctor` re-hashes the live dispatcher and warns on mismatch.
DISPATCHER_PREAMBLE_SHA = "cea9d70d213e0e71fd9db721f1228f320644bc126acaa668b879662c202e0069"
DISPATCHER_PATH = Path.home() / "bin" / "mesh-dispatcher"


def dispatcher_preamble_drift() -> str | None:
    """None if the dispatcher preamble still matches the synced copy; otherwise a
    human warning string. Missing dispatcher file → None (cutover complete box)."""
    try:
        src = DISPATCHER_PATH.read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"MANDATORY FIRST STEP.*?Follow CLAUDE\.md workflow", src, re.S)
    if not m:
        return "dispatcher preamble block not found — pattern changed, re-sync fiddler_prompt.py"
    sha = hashlib.sha256(m.group(0).encode()).hexdigest()
    if sha != DISPATCHER_PREAMBLE_SHA:
        return (f"dispatcher preamble CHANGED (sha {sha[:12]}… ≠ synced "
                f"{DISPATCHER_PREAMBLE_SHA[:12]}…) — review mesh-dispatcher and re-sync "
                f"build_prompt() + DISPATCHER_PREAMBLE_SHA in fiddler_prompt.py")
    return None


def build_prompt(task_id: str, task_title: str, continuity_hint: str = "") -> str:
    """FULL ACP preamble — for a FRESH context window (boot / post-clear /
    post-recovery). Synced copy of the dispatcher's spawn preamble."""
    short = task_id[:8]
    return (
        (continuity_hint if continuity_hint else "") +
        f"[FIDDLER] New Mesh task fed into your live session — #{short}.\n\n"
        "MANDATORY FIRST STEP — own-backlog triage. BEFORE this task, call "
        "get_my_tasks with status_category=in_progress. For EACH of YOUR "
        "in_progress tasks whose last comment is >24h old (or which has no "
        "comments at all), do ONE of:\n"
        "  (a) close it as done with a concrete result (link / artifact / "
        "verifiable outcome), OR\n"
        "  (b) leave a hard-blocker comment naming the EXACT ask — one specific "
        "decision or input you need from the owner — and move it to a blocked "
        "status if available, otherwise just comment.\n"
        "Phrasings like 'in queue', 'prioritizing', 'will get to it' "
        "are FORBIDDEN — they hide the agent. Either close, or name the blocker. "
        "Only AFTER triaging your own stale backlog do you start this task.\n\n"
        f"MANDATORY SECOND STEP — checkout this task. Call "
        f"checkout_task(task_id='{task_id}', ttl_minutes=120) before touching any "
        f"files or making any changes.\n"
        f"  • 200 OK → proceed.\n"
        f"  • 409 conflict → another agent holds the lock. Add ONE comment "
        f"«Skipping — task locked by @<name>, will retry after expiry.» then EXIT.\n"
        f"  • checkout is auto-released when you move the task to "
        f"done/review/cancelled — no manual release_task needed.\n\n"
        "MANDATORY THIRD STEP — wake-up memory read (load your own context BEFORE "
        "working). Make ONE light recall pass:\n"
        "  • Call recall(query='<3-5 keywords from this task>', min_importance=0.3) "
        "— your Mesh episodic memory of prior sessions on this topic.\n"
        "  • Call recall(tags_any=['pavel-decision'], scope='workspace') — "
        "canonical the operator decisions/directives (C4 channel). Apply any that touch your task.\n"
        "  • Call recall(tags_any=['solution'], query='<task topic>', "
        "order_by='decayed_relevance') — solution-journal: prior VERIFIED wins on a "
        "similar topic to reuse BEFORE starting.\n"
        "This is a light read — do NOT loop or block on empty results; proceed.\n\n"
        "READ-BEFORE-ACT — read the task's own comment thread before you touch it. "
        "Call get_task(task_id, include_comments=true) (one call returns the whole "
        "thread) and read it to the end BEFORE your first comment or edit. The "
        "thread is where the prior session's findings, the reviewer's bounce reason, "
        "and any blocking ask already live — 27% of first comments fleet-wide are "
        "currently posted onto a thread the agent never read, which is how work gets "
        "re-derived and answered questions get re-asked. Reading it is one call.\n\n"
        "PRE-DONE GATE — before moving any task to `done`, verify each point:\n"
        "  1. List every acceptance criterion and confirm each with evidence "
        "(file path, command output, or Mesh comment).\n"
        "  2. Open subtasks assigned to you? If yes → cannot be done.\n"
        "  3. Code/PR task: is the PR merged (not just submitted)?\n"
        "  4. Deploy task: is the change live on the prod endpoint?\n"
        "Only mark done when ALL criteria have verifiable evidence. In doubt → move "
        "to `review` with a comment. Never claim done on 'should work' logic.\n\n"
        f"You have a new task assigned: '{task_title}' (ID: {task_id}). Use "
        f"get_task to read full details, then work on it autonomously. Follow "
        f"CLAUDE.md workflow."
    )


def get_thread_id(task: dict) -> str | None:
    """Extract explicit thread_id from a task dict (Phase4 / Mesh task thread_id field).

    Precedence:
      1. task["thread_id"]       — first-class column added in Mesh PR (evc-mesh #226)
      2. custom_fields.thread_id — backward-compat for tasks set before the PR
    """
    tid = task.get("thread_id")
    if tid:
        return str(tid)
    cf = task.get("custom_fields") or {}
    if isinstance(cf, str):
        try:
            cf = json.loads(cf)
        except Exception:
            cf = {}
    return (cf.get("thread_id") or None) if isinstance(cf, dict) else None


def build_continuity_hint(prev_task_id: str, prev_task_title: str, reason: str) -> str:
    """One-paragraph hint prepended to the task prompt when the agent is continuing
    a thread (Phase4). Tells the agent it already has episodic memory from the prior
    task so it can skip re-deriving context from scratch."""
    short = (prev_task_id or "")[:8]
    words = [w for w in (prev_task_title or "").split() if len(w) > 3 and w.isalpha()][:4]
    recall_q = " ".join(words) or (prev_task_title or "")[:40]
    return (
        f"[CONTINUITY — {reason}] This task continues the same work thread as "
        f"#{short} ('{prev_task_title}'). Your episodic memory already holds context "
        f"from that session — in your MANDATORY THIRD STEP use "
        f"recall(query='{recall_q}', min_importance=0.3) to recover it efficiently. "
        f"Skip the 'derive context from scratch' phase.\n\n"
    )


def build_thin_prompt(task_id: str, task_title: str, continuity_hint: str = "") -> str:
    """P1-1 two-tier paste: the IN-CONTEXT prompt. The session already did the full
    wake-up read this context window — thin = checkout + canonical refresh +
    assignment. canonical stays per-task: the only LIVE channel for fresh fleet
    decisions while the boot snapshot ages.

    The routing anchor is always included — a cheap one-liner that prevents the
    most common drift pattern (assigning to devs instead of leads on a long thread).
    """
    return (
        (continuity_hint if continuity_hint else "") +
        f"[FIDDLER] Next task in your live session — #{task_id[:8]}.\n\n"
        f"MANDATORY FIRST — checkout: call checkout_task(task_id='{task_id}', "
        f"ttl_minutes=120) before touching anything. 409 → one comment "
        f"«Skipping — locked by @<name>» then EXIT.\n"
        f"MANDATORY SECOND — call recall(tags_any=['pavel-decision'], scope='workspace'); "
        f"apply any canonical the operator decisions that touch this task. Also call "
        f"recall(tags_any=['solution'], query='<task topic>', order_by='decayed_relevance') "
        f"— solution-journal: prior VERIFIED wins to reuse before starting.\n\n"
        f"MANDATORY THIRD — recall before work: call recall(query='<3-5 keywords from "
        f"this task>', min_importance=0.3). It is graph-backed (KG) — surfaces prior "
        f"decisions / incidents / gotchas CONNECTED to this work, not just keyword "
        f"hits. One cheap call; do not re-derive context from scratch.\n\n"
        f"MANDATORY FOURTH — READ-BEFORE-ACT: call get_task(task_id='{task_id}', "
        f"include_comments=true) and read the thread to the end BEFORE your first "
        f"comment or edit. One call returns the whole thread — it holds the prior "
        f"session's findings, the reviewer's bounce reason, and any already-answered "
        f"blocking question.\n\n"
        "[routing] Product tasks → LEAD: Vega=Spark, Nova/Kilo=Mesh, "
        "Delta/Grove=Obsidian, Pixel=sites, Comet=Argus. "
        "Never assign sub-tasks directly to devs; never cross-project without lead approval.\n\n"
        f"You have a new task assigned: '{task_title}' (ID: {task_id}). Use get_task "
        f"to read full details, then work on it autonomously. Follow CLAUDE.md "
        f"workflow — the PRE-DONE gate applies as always (evidence for every "
        f"acceptance criterion; in doubt → review, never 'should work')."
    )


def build_nudge(task_id: str) -> str:
    return (
        f"[FIDDLER nudge] Task #{task_id[:8]} is still assigned to you and has been "
        f"open a while. Either finish it (move to review/done) or leave a hard-blocker "
        f"comment naming the exact ask. Don't let it sit silent."
    )


def build_rescue_prompt(task_id: str, task_title: str, age_min: int) -> str:
    """P0-1 zombie rescue — a stuck in_progress task is re-fed into the session."""
    return (
        f"[FIDDLER rescue] Task '{task_title}' (ID: {task_id}) is assigned to you and "
        f"has been sitting in_progress ~{age_min}m with no updates — your session may "
        f"have restarted mid-work. Call get_task and read the comments/commits to see "
        f"what was already done, then RESUME from there (do not redo finished steps). "
        f"Either finish it (move to review/done) or, if genuinely blocked, leave a "
        f"hard-blocker comment naming the exact ask and move it back to todo. "
        f"Follow CLAUDE.md workflow."
    )


FLUSH_PROMPT = (
    "[FIDDLER] This session's context is about to be CLEARED for an unrelated task. "
    "If you are holding important unwritten facts from this session (decisions, "
    "gotchas, open threads, learnings), persist them NOW with ONE remember() call "
    "(kind:session-checkpoint). Mesh remember() is canonical — do NOT write to mempalace "
    "(retired 2026-07-04; read-only for history). "
    "If nothing needs saving, reply with just: OK"
)
