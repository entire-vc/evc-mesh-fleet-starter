#!/usr/bin/env python3
"""fiddler — feeds Mesh tasks into persistent interactive `claude`-TUI tmux sessions.

EPIC #83a1bc1c (billing-survival to 2026-06-15): Anthropic moves headless `claude -p`
out of the subscription into the metered API pool. Variant B keeps the fleet as
persistent interactive `claude` TUI sessions (covered by the subscription) and
*injects* Mesh tasks into them with this standalone daemon — the fiddler.

Spec: docs/fiddler-spec.md (Phase1-s1). v0.1: s2 base. v0.2: Phase2-b context-recycle. v1.0: Phase4 thread-resume.

HARD INVARIANTS (spec §1):
  1. Standalone CLI. NOT a Mesh agent, no entry in mesh-agents.json, never in the
     dispatcher spawn table.
  2. Does NOT touch the live spawn path (mesh-dispatcher `claude -p` Popen block).
     Prompts live in fiddler_prompt.py (P2-3) — a synced copy of the dispatcher's
     spawn preamble; `fiddler doctor` sha-checks the live dispatcher for drift.
     The dispatcher itself stays untouched until full cutover.
  3. Dispatcher stays as-is = fallback. Fiddler only drives agents in its own
     allowlist (config). In Phase1 those are dedicated test agents the dispatcher does
     not know → no double-work by construction.
  4. One agent = exactly one mechanism (dispatcher OR fiddler, never both).
  5. Completion is detected by Mesh status_category ∈ {review, done, cancelled},
     NEVER by stdout. capture-pane is used ONLY for health-checks.

stdlib only (matches mesh-dispatcher). Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from human_gate import is_human_gated  # audit fix #2: shared canonical human-gate predicate
# #8f0f9ef6: the SECOND freeze signal ("frozen, the ask lives on #X"). Shared with the
# dispatcher and the agent-ops collector so the feeders and the BROKEN classifier
# cannot disagree about what "feedable" means.
from human_gate import dep_freeze_reason, DEP_BLOCKING_TYPES

# Shared prompt templates (P2-3) — single source for everything we paste.
from fiddler_prompt import (build_prompt, build_thin_prompt, build_nudge,
                            build_rescue_prompt, build_continuity_hint,
                            get_thread_id, FLUSH_PROMPT,
                            dispatcher_preamble_drift)

# Shared reauth classification (#038393a0) — the SAME predicate the
# fiddler-reauth-watchdog uses, so the two detectors cannot drift apart.
import fiddler_pane

# ----------------------------------------------------------------------------- #
# Paths & defaults
# ----------------------------------------------------------------------------- #

HOME = Path(os.path.expanduser("~"))
CONFIG_PATH = Path(os.environ.get("FIDDLER_CONFIG", HOME / ".config/fiddler/fiddler.json"))
STATE_DIR = Path(os.environ.get("FIDDLER_STATE_DIR", HOME / ".fiddler"))
LOG_DIR = STATE_DIR / "logs"
LOG_PATH = LOG_DIR / "fiddler.log"
STATE_PATH = STATE_DIR / "state.json"
ROLLBACK_DIR = HOME / ".config" / "fiddler" / "rollback"

# Read-credit sidecar (memory-eval): fiddler prefetches Mesh memories and INJECTS them
# into the feed (the agent never calls recall itself), so those real reads are invisible
# to the transcript-scanning collector — exactly the dispatcher's problem, solved the same
# way. Each injected prefetch appends one line here; collect-memory-ops.py folds it into the
# layer's reads so R:W reflects reality (else the dispatcher→fiddler shift looks like a
# read collapse). Mirror of ~/.openclaw/metrics/dispatcher-memory-reads.jsonl.
FIDDLER_READS_SIDECAR = HOME / ".openclaw" / "metrics" / "fiddler-memory-reads.jsonl"

TMUX_BIN = os.environ.get("TMUX_BIN", "/opt/homebrew/bin/tmux")

DEFAULTS = {
    "mesh_api_url": "https://mesh.example.com",
    "poll_interval_sec": 20,
    "nudge_after_sec": 1200,     # 20 min — soft nudge if still BUSY
    "task_timeout_sec": 2400,    # 40 min — escalate + free the slot
    "debounce_sec": 120,         # don't re-feed (agent, task) within this of completion
    "recently_done_guard_sec": 3600,  # skip tasks completed within this window even if re-queued (reactivation guard)
    "rescue_after_sec": 600,     # 10 min — resume an idle agent's in_progress task with no updates (was 30m; idle-gap = the #1 "sluggishness" lever). The rescue loop only runs for a confirmed-idle lane, so this is "resume the agent's own abandoned work", not interrupting active work.
    "max_session_age_sec": 86400,    # P1-3: force flush+/clear if context older than this (boot snapshot staleness)
    "max_transcript_bytes": 5_000_000,  # P1-3: force flush+/clear when session transcript exceeds this (cache-read burn)
    "alert_min_interval_sec": 1800,  # anti-spam: same (agent,reason) TG alert at most every 30 min
    "alert_outbox": None,        # path to a telegram outbox jsonl; null = log-only (safe default for Phase1)
    "alert_chat_id": None,       # telegram chat id for alerts (e.g. the operator 000000000); null = no TG
    "full_refresh_every": 5,    # P1-1 anti-drift: force full ACP re-paste every N thin feeds (0 = disabled)
    # A self-host Mesh instance may sit behind an HTTP basic-auth gate on its
    # reverse proxy (partner instance: password-walled UI, app-authenticated API).
    # "user:pass" here makes every request also carry the basic credential.
    # null = the default for our own mesh.example.com, no Authorization header.
    "basic_auth": None,
    "agents": [],
}

# Module-global, set once from cfg["basic_auth"] at run() start. Precomputed to
# the full "Basic <base64>" header value, or None. A separate fiddler PROCESS with
# its own config (the partner instance) sets it; our fiddler leaves it None, so
# there is no cross-instance leak — different processes, different globals.
_BASIC_AUTH_HEADER: str | None = None


def _set_basic_auth(cfg: dict) -> None:
    global _BASIC_AUTH_HEADER
    ba = (cfg.get("basic_auth") or "").strip()
    if not ba or ":" not in ba:      # colon-less = treat as disabled, never send an empty password
        _BASIC_AUTH_HEADER = None
        return
    import base64
    _BASIC_AUTH_HEADER = "Basic " + base64.b64encode(ba.encode()).decode()


def _headers(key: str, extra: dict | None = None) -> dict:
    """The headers every Mesh request needs: the agent key the app reads, plus the
    optional reverse-proxy basic-auth. Centralised so no request path can drift out
    of coverage (the Go client learned this same lesson — one applyAuth, not N)."""
    h = {"X-Agent-Key": key}
    if extra:
        h.update(extra)
    if _BASIC_AUTH_HEADER:
        h["Authorization"] = _BASIC_AUTH_HEADER
    return h

PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

# --- feed-skip observability (#b7615572) ------------------------------------- #
# A task that is eligible but not picked used to vanish: candidate-gathering had
# five SILENT `continue`s (in-flight, project/label scope, debounce, foreign
# checkout) and the busy-lane gates returned before gathering ran at all. So a
# HIGH task sitting behind a long MEDIUM produced ZERO log lines — indistinguishable
# from a lost task. Every drop now names its reason, deduped so a permanent skip
# doesn't spam 3 lines/minute forever (the umbrella gate alone wrote ~800k lines).
SKIP_RELOG_SEC = 1800     # re-log an unchanged skip reason at most this often
STARVE_PROBE_SEC = 300    # how often a BUSY lane checks what it's making wait
STARVE_ALERT_SEC = 1800   # a higher-priority task waiting this long behind a busy lane pages
_skip_seen: dict = {}     # (lane_id, task_id) -> (reason, last_logged_wall)


def log_skip(lane_id: str, tid: str, reason: str, now_wall: float | None = None) -> bool:
    """Log 'task not picked, and why' — once per (lane, task, reason), re-armed every
    SKIP_RELOG_SEC. Returns True if it logged. Dedup is on the reason STRING, so a
    task whose skip reason CHANGES (gated → busy-lane → fed) always re-announces."""
    now_wall = time.time() if now_wall is None else now_wall
    prev = _skip_seen.get((lane_id, tid))
    if prev and prev[0] == reason and now_wall - prev[1] < SKIP_RELOG_SEC:
        return False
    _skip_seen[(lane_id, tid)] = (reason, now_wall)
    if len(_skip_seen) > 2000:  # bound: fleet backlog is O(100s)
        for k, _ in sorted(_skip_seen.items(), key=lambda kv: kv[1][1])[:500]:
            _skip_seen.pop(k, None)
    log(lane_id, f"#{tid[:8]} skip feed — {reason}")
    return True


def starvation_report(candidates: list, cur_id: str | None, cur_priority: str | None,
                      waiting: dict, alerted: list, now_wall: float) -> tuple:
    """PURE. What is a BUSY lane making wait, and has anything waited too long?

    A lane that is BUSY (or whose pane reads busy) never re-runs the priority sort —
    by design: fiddler NEVER preempts a working agent (invariant 5, the agent owns its
    turn). The cost is that a HIGH task can wait behind a long-running MEDIUM with no
    evidence anywhere. This does not change the feeding decision; it makes the wait
    VISIBLE, and pages if it gets pathological.

    cur_id=None (lane IDLE but pane busy / no fresh turn-done sentinel) means NOTHING
    can be fed at all — then every eligible candidate is being starved, whatever its
    priority, so cur_priority is treated as worse-than-low.

    Returns (logs, alerts, new_waiting, new_alerted):
      logs   — [(task, waited_min, reason)] to emit via log_skip
      alerts — [(task, waited_min)] that crossed STARVE_ALERT_SEC (once each)
    """
    cur_rank = PRIORITY_RANK.get((cur_priority or "medium").lower(), 2) if cur_id else 99
    logs, fires, new_waiting, new_alerted = [], [], {}, []
    for t in candidates:
        tid = t.get("id")
        if not tid or tid == cur_id:
            continue
        rank = PRIORITY_RANK.get((t.get("priority") or "medium").lower(), 2)
        if rank >= cur_rank:
            continue  # does not outrank the work in flight — normal queueing, not starvation
        first = waiting.get(tid, now_wall)
        new_waiting[tid] = first
        waited_min = int((now_wall - first) / 60)
        if cur_id:
            reason = (f"DEFERRED — lane busy on #{cur_id[:8]} ({cur_priority}); this task is "
                      f"{t.get('priority')} and outranks it, waiting {waited_min}m. fiddler does "
                      f"not preempt a working agent — it will be picked first on lane release.")
        else:
            reason = (f"DEFERRED — lane pane reads busy with no turn-done sentinel, so NOTHING "
                      f"can be fed; this {t.get('priority')} task has waited {waited_min}m.")
        logs.append((t, waited_min, reason))
        if now_wall - first >= STARVE_ALERT_SEC:
            if tid in alerted:
                new_alerted.append(tid)
            else:
                fires.append((t, waited_min))
                new_alerted.append(tid)
    return logs, fires, new_waiting, new_alerted

# Labels that are too broad to indicate topical thread continuity (Phase2-b context-recycle).
_GENERIC_LABELS = frozenset({
    "fleet", "interactive-migration", "bug", "feature", "infra",
    "phase:plan", "phase:execute", "phase:verify", "phase:design",
    "kind:monitor", "kind:session-checkpoint", "no-pavel-triage",
    "context-recycle",
    # meta/process labels — not a topic, so they must NOT imply thread continuity
    "throwaway", "smoke", "probe", "test", "wip", "p0", "p1", "p2",
})

# Agent-Eval golden-decompose fixtures (parent labels agent-eval/golden/eval-harness).
# The decompose-n oracle scores subtask COUNT, not execution — so the generated
# children are eval fixtures, NOT product work. They carry NO label themselves
# (the marker lives on the PARENT), so they slip past every own-label filter and
# get fed into a live session, where agents have twice built + reverted a phantom
# RFC8628 `oauth-device/` server (bob has NO public API; reverts 67f9482, c14c8c8).
# Recurring leak #31f7d853 (06-15, ×2 06-22). mesh-intake-sweep already skips these
# via parent_is_eval_fixture(); the feeder lacked the same guard — this closes it.
EVAL_FIXTURE_LABELS = frozenset({"agent-eval", "golden", "eval-harness"})

# Re-auth / dead-TUI markers moved to scripts/fiddler_pane.py (#038393a0) — the
# SAME module the fiddler-reauth-watchdog imports, so the two detectors cannot
# drift. A bare marker scan is no longer sufficient anyway: see
# fiddler_pane.reauth_scan() for why a visible marker is not proof of a live
# login wall (the TUI keeps one failed turn's error on screen for 15-20 min).
REAUTH_MARKERS = fiddler_pane.AUTH_ERROR_MARKERS + fiddler_pane.LOGIN_WALL_MARKERS
# Entitlement / billing lapse - DISTINCT from reauth: token is valid (loggedIn=true)
# but org subscription/credits are exhausted. Relaunch does NOT help (account-wide,
# needs the operator to RENEW), so it must NOT trigger auto-relaunch (would burn attempts).
ENTITLEMENT_MARKERS = (
    "Credit balance is too low",
    "disabled Claude subscription access",
    "organization has disabled",
)
# Claude Max subscription 5h-window usage limit markers (Phase3-a monitoring).
# Unlike REAUTH (login wall), this is a temporary pause — the session recovers
# automatically when the 5h rolling window resets. Detected by fiddler-exporter.py
# for the fiddler_window_limit_hits_5h metric; not treated as UNHEALTHY here.
WINDOW_LIMIT_MARKERS = (
    "usage limit",
    "You've reached your",
    "You've sent too many",
    "rate limited",
    "limit will reset",
    "too many messages",
    "pause your usage",
)

# Sentinel injected into task comments when a supervised/review task is signaled as
# "ready for the operators close". Checked before posting to ensure idempotency (one signal,
# no daily nag). Also used by review-verify-driver to skip TG repeat.
READY_CLOSE_MARKER = "<!-- fiddler:ready-to-close -->"

# Idle-prompt marker: the claude TUI shows a "❯" prompt box when ready for input.
# It is NOT pinned to the bottom — there are trailing blank rows below it — so we
# scan the whole pane, not just the tail.
IDLE_PROMPT_MARKER = "❯"
# Markers that mean the TUI is actively working / waiting and must NOT be fed.
BUSY_MARKERS = (
    "esc to interrupt",      # claude is generating
    "Do you want to proceed",  # permission prompt (session not in bypass mode)
    "Tool use",              # mid tool-call confirmation
)


def flush_before_clear(session: str, wait_sec: int = 90) -> None:
    """Paste the flush prompt and wait (bounded) for the agent to finish the write.
    Never blocks the queue forever — proceeds to /clear after wait_sec regardless."""
    log("recycle", f"pre-clear memory flush → {session}")
    if not paste_prompt(session, FLUSH_PROMPT):
        log("recycle", f"flush paste failed on {session} — proceeding to /clear")
        return
    deadline = time.monotonic() + wait_sec
    time.sleep(3)  # let the TUI register the paste before health-checking
    while time.monotonic() < deadline:
        health, _ = session_health(session)
        if health == "ok":
            log("recycle", f"{session} flushed (idle) — safe to /clear")
            return
        time.sleep(2)
    log("recycle", f"{session} still busy {wait_sec}s after flush prompt — /clear anyway")


# ----------------------------------------------------------------------------- #
# Logging
# ----------------------------------------------------------------------------- #

def log(scope: str, msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{scope}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------------- #
# Config
# ----------------------------------------------------------------------------- #

def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        log("config", f"missing {CONFIG_PATH} — run `fiddler doctor` for a template")
        return dict(DEFAULTS)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    merged = dict(DEFAULTS)
    merged.update(cfg)
    return merged


def enabled_agents(cfg: dict) -> list:
    return [a for a in cfg.get("agents", []) if a.get("enabled", True)]


# ----------------------------------------------------------------------------- #
# Mesh API
# ----------------------------------------------------------------------------- #

def _api_get(url: str, key: str, timeout: int = 20):
    req = Request(url, headers=_headers(key))
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_todo_tasks(api_url: str, key: str) -> list:
    """One agent's todo tasks (assignee = key holder), eligible for feeding."""
    try:
        url = f"{api_url}/api/v1/agents/me/tasks?status_category=todo&limit=200"
        return _api_get(url, key).get("tasks") or []
    except Exception as e:
        log("poll", f"fetch todo failed: {e}")
        return []


def fetch_task(api_url: str, key: str, task_id: str) -> dict | None:
    """Single task. REST /api/v1/tasks/{id} returns the task object at TOP LEVEL
    (no `.task` wrapper — unlike the MCP get_task tool). Handle both shapes."""
    try:
        data = _api_get(f"{api_url}/api/v1/tasks/{task_id}", key)
        inner = data.get("task")
        return inner if isinstance(inner, dict) else data
    except Exception as e:
        log("poll", f"get_task {task_id[:8]} failed: {e}")
        return None


def parent_is_eval_fixture(api_url: str, key: str, task: dict,
                           cache: dict) -> bool:
    """True if the task's PARENT carries an eval-fixture label — i.e. this is a
    generated child of an Agent-Eval golden decompose task that must never be fed
    into a session (see EVAL_FIXTURE_LABELS). The golden parent itself stays
    feedable (decomposing it IS the eval); only its children are blocked. Cached
    per parent_id so a 4-child fixture costs one parent fetch. Mirrors
    mesh-intake-sweep.parent_is_eval_fixture(). Fail-open: a parent-fetch error
    returns False (feed it) rather than silently starving real work."""
    pid = task.get("parent_task_id")
    if not pid:
        return False
    if pid not in cache:
        parent = fetch_task(api_url, key, pid) or {}
        cache[pid] = bool(EVAL_FIXTURE_LABELS & set(parent.get("labels") or []))
    return cache[pid]


_project_status_cat_cache: dict = {}  # project_id -> {status_id: category}, process-lifetime


def _project_status_map(api_url: str, key: str, project_id: str) -> dict:
    """{status_id: category} for a project, cached process-lifetime (statuses rarely
    change). Needed because the subtasks endpoint returns bare status_id, not category
    — same shape gap mesh-intake-sweep works around per-project."""
    if not project_id:
        return {}
    if project_id in _project_status_cat_cache:
        return _project_status_cat_cache[project_id]
    try:
        statuses = _api_get(f"{api_url}/api/v1/projects/{project_id}/statuses", key)
    except Exception as e:
        log("umbrella-gate", f"project {project_id[:8]} statuses fetch failed: {e}")
        return {}
    cat_map = {s["id"]: s.get("category", "") for s in (statuses or []) if isinstance(s, dict)}
    _project_status_cat_cache[project_id] = cat_map
    return cat_map


def umbrella_all_children_blocked(api_url: str, key: str, task: dict, cache: dict) -> bool:
    """True if `task` has subtasks and every one of them still open (not done/cancelled)
    is itself un-actionable right now — human-gated, `delegation_level=supervised`, or
    parked in backlog. Feeding an umbrella/captain task in that state has no real work
    to do: it just re-triggers a full-tree re-review every cycle (churn #6561d12d —
    captain #0f673b8d fed every ~30min via mesh-intake-sweep's PROMOTE_MARKER comment,
    Warden re-scanning and "re-closing" already-done children each pass, ~$1/pass).
    Cached per task_id for this tick. Fail-open on any fetch error — a transient API
    blip must not silently starve a captain task that has genuinely actionable work."""
    tid = task.get("id")
    if not tid or (task.get("subtask_count") or 0) <= 0:
        return False
    if tid in cache:
        return cache[tid]
    try:
        children = _api_get(f"{api_url}/api/v1/tasks/{tid}/subtasks", key).get("items") or []
    except Exception as e:
        log("umbrella-gate", f"#{tid[:8]} subtasks fetch failed: {e} — fail-open, feed")
        cache[tid] = False
        return False
    status_map = _project_status_map(api_url, key, task.get("project_id", ""))
    open_children = [c for c in children
                     if status_map.get(c.get("status_id"), "") not in ("done", "cancelled")]
    if not open_children:
        cache[tid] = False  # nothing open at all — not this guard's concern
        return False
    blocked = all(
        c.get("delegation_level") == "supervised"
        or bool(c.get("human_gate"))
        or is_human_gated(c)
        or status_map.get(c.get("status_id"), "") == "backlog"
        for c in open_children
    )
    cache[tid] = blocked
    return blocked


def _credit_prefetch_read(agent: str, hits: int) -> None:
    """Append one read-credit line for an injected fiddler prefetch (memory-eval).
    Layer = episodic: the prefetch queries Mesh /api/v1/memories/search — the same
    workspace-memory store recall/remember hit. Best-effort; never raises."""
    if not agent:
        return
    try:
        FIDDLER_READS_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "agent": agent.lower(), "layer": "episodic", "op": "prefetch_search",
               "hits": hits, "via": "fiddler"}
        with FIDDLER_READS_SIDECAR.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def prefetch_memories(api_url: str, key: str, task_title: str, agent: str = "", limit: int = 3) -> str:
    """Phase A3 boot-paste prefetch: top-K Mesh memories for the task topic,
    prepended to the FULL preamble only (fresh window — in-context windows
    already hold their reads). Returns '' on any failure — prefetch is a
    quality bonus, never a blocker. Mirrors the dispatcher's old
    mempalace_prefetch, but from Mesh (the live store). On a successful inject it
    credits one read to the fiddler sidecar (else the read is invisible to the
    transcript collector → R:W understates reality)."""
    try:
        words = [w for w in task_title.split() if len(w) > 3][:5]
        if not words:
            return ""
        from urllib.parse import quote
        q = quote(" ".join(words))
        data = _api_get(f"{api_url}/api/v1/memories/search?q={q}&limit={limit}", key, timeout=10)
        items = data.get("items") or []
        if not items:
            return ""
        out = ["[PREFETCHED MEMORY — relevant prior context, verify before relying on it:]"]
        for it in items:
            body = (it.get("content") or "").strip().replace("\n", " ")
            out.append(f"• ({it.get('key','?')}) {body[:300]}")
        _credit_prefetch_read(agent, len(items))
        return "\n".join(out) + "\n\n"
    except Exception:
        return ""


def _credit_comments_read(agent: str, hits: int) -> None:
    """Read-credit line for an injected task-comments prefetch (memory-eval, layer=task-comments).
    Without this the boot-paste injection of prior comments is invisible to the collector → the
    task-comments R:W understates reality. Best-effort; never raises."""
    if not agent:
        return
    try:
        FIDDLER_READS_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "agent": agent.lower(), "layer": "task-comments", "op": "prefetch_comments",
               "hits": hits, "via": "fiddler"}
        with FIDDLER_READS_SIDECAR.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def is_returned_task(subst: list, agent: str) -> bool:
    """True when the task is coming BACK to the agent rather than arriving fresh: the agent
    already reported on it, and the newest substantive comment is somebody ELSE's answer to
    that report (a review bounce / a surgical correction).

    Author-based, not marker-based on purpose — a bounce is a bounce whether or not the
    reviewer wrote the word DO-NOT-SHIP, and reviewers write in RU and EN."""
    if not agent or not subst:
        return False
    mine = [c for c in subst if (c.get("author_name") or "") == agent]
    if not mine:
        return False  # never worked it → genuinely fresh
    last = subst[-1]
    return (last.get("author_name") or "") != agent


def prefetch_comments(api_url: str, key: str, task_id: str, agent: str = "", limit: int = 6,
                      lane_id: str = "") -> str:
    """The task's RECENT comments, prepended to EVERY fed prompt (any tier).

    Comment context is a property of the TASK, not of the context window — gating it on
    window-freshness (as this did until #cd776e2a) meant a task re-fed into a live session
    arrived with NO comments at all. For a batch lane like spark-gen that is the common case
    (83 of its last 132 feeds were `thin`), so a review bounce reading "fix asset X" was
    consumed as "run the next batch": the correction was invisible, and the same defect
    class recurred for six consecutive batches.

    When the task is RETURNED (see is_returned_task) the block is fronted with a hard banner
    that reframes it as a correction to apply, not new work to start.

    Skips [fiddler] log noise. Credits one task-comments read on inject.
    On fetch failure returns a LOUD placeholder, never a silent '' — a blind feed must be
    visible in the prompt and the log, not indistinguishable from a task with no comments."""
    comments = _fetch_task_comments_strict(api_url, key, task_id, limit=limit * 3)
    if comments is None:
        return ("[TASK COMMENTS — FETCH FAILED. You are being fed BLIND: this task may carry a "
                "review bounce or a surgical correction that did not reach this prompt. Call "
                f"get_task(task_id='{task_id}', include_comments=true) and read the thread to the "
                "end BEFORE doing any work.]\n\n")
    if not comments:
        return ""
    subst = [c for c in comments
             if not (c.get("body") or "").lstrip().startswith("[fiddler]")]
    recent = subst[-limit:]
    if not recent:
        return ""

    out = []
    if is_returned_task(subst, agent):
        who = subst[-1].get("author_name", "?")
        out.append(
            f"⚠️ [RETURNED TASK — NOT A FRESH ASSIGNMENT] You already reported on this task and "
            f"@{who} answered you. The newest comment below is a CORRECTION addressed to you.\n"
            f"Do EXACTLY what it asks, on the SPECIFIC item it names, and verify that item is "
            f"fixed. Do NOT run your routine/batch loop on this task — producing new work instead "
            f"of the named fix is the failure this banner exists to stop. If you think the "
            f"correction is wrong, say so in a comment — never silently substitute other work.\n"
            f"First action: get_task(task_id='{task_id}', include_comments=true) — read the "
            f"newest comment IN FULL (it is truncated below)."
        )
    out.append("[RECENT TASK COMMENTS — prior context on THIS task; READ before acting so you don't "
               "repeat resolved points or redo handled work:]")
    for i, c in enumerate(recent):
        who = c.get("author_name", "?")
        body = (c.get("body") or "").strip().replace("\n", " ")
        # The newest comment is the one that carries the correction — give it room.
        cap = 1200 if i == len(recent) - 1 else 400
        out.append(f"• [{who}] {body[:cap]}")
    _credit_comments_read(agent, len(recent))
    return "\n".join(out) + "\n\n"


def fetch_in_progress_tasks(api_url: str, key: str) -> list:
    """One agent's in_progress tasks WITH full data (for the P0-1 zombie rescue).
    Returns [] on failure."""
    try:
        url = f"{api_url}/api/v1/agents/me/tasks?status_category=in_progress&limit=200"
        return _api_get(url, key).get("tasks") or []
    except Exception as e:
        log("poll", f"fetch in_progress failed: {e}")
        return []


def task_age_sec(task: dict) -> float:
    """Seconds since the task's last update (updated_at ISO8601). 0 on parse failure
    so an unparseable timestamp can never look stale."""
    raw = task.get("updated_at") or ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return 0.0


def _fetch_task_comments_strict(api_url: str, key: str, task_id: str, limit: int = 20,
                                attempts: int = 2):
    """Comments for a task, or None if the fetch FAILED (vs [] = task genuinely has none).

    The distinction matters: a swallowed error that returns [] is indistinguishable from an
    empty thread, which is how a bounced task got fed with no correction in the prompt and
    nobody noticed (#cd776e2a — the 2026-07-12T22:53 feed of #1dc17e6e logged `full+pf`, no
    `+pc`, because the comments GET timed out). Retries once: these are read-timeouts, not
    logic errors."""
    last_err = None
    for _ in range(max(1, attempts)):
        try:
            req = Request(
                f"{api_url}/api/v1/tasks/{task_id}/comments?limit={limit}",
                headers=_headers(key),
            )
            # 8s (not 15s) BECAUSE we now retry and now run on every feed, not just fresh
            # ones: the lane loop is serial, so worst-case blocking per feed must not grow.
            # 2 x 8s caps it at ~16s — the same order as the old single un-retried 15s call.
            with urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            return data.get("comments") or data.get("items") or []
        except Exception as e:
            last_err = e
    log("comments", f"comment fetch {task_id[:8]} FAILED after {attempts} tries: {last_err}")
    return None


def _fetch_task_comments(api_url: str, key: str, task_id: str, limit: int = 20) -> list:
    """Fetch comments for a task; returns [] on error. Used by zombie-rescue gate."""
    return _fetch_task_comments_strict(api_url, key, task_id, limit=limit) or []


def fetch_task_deps(api_url: str, key: str, task_id: str) -> list:
    """A task's formal dependencies, NORMALIZED for human_gate.dep_freeze_reason
    (each row carries the blocker's `blocker_completed_at`). [] on any error.

    ⚠️ Transport note (probed live 2026-08-02, #8f0f9ef6): dependencies are served
    ONLY by the sub-resource `GET /tasks/<uuid>/dependencies`, and only for a FULL
    uuid (a short 8-hex prefix 400s). `GET /tasks/<id>?include_dependencies=true`
    returns 200 with no `dependencies` key at all — the dispatcher's old formal-dep
    check read exactly that field and was therefore dead code from birth.

    Fail-open by construction: every error path yields [], which yields no freeze.
    """
    if not task_id or len(str(task_id)) < 36:
        return []
    try:
        req = Request(f"{api_url}/api/v1/tasks/{task_id}/dependencies",
                      headers=_headers(key))
        with urlopen(req, timeout=12) as r:
            rows = json.loads(r.read())
        if isinstance(rows, dict):
            rows = rows.get("dependencies") or rows.get("items") or []
        out = []
        for dep in rows or []:
            if (dep.get("dependency_type") or "blocks").lower() not in DEP_BLOCKING_TYPES:
                continue
            blocker = dep.get("depends_on_task_id")
            if not blocker:
                continue
            try:
                breq = Request(f"{api_url}/api/v1/tasks/{blocker}",
                               headers=_headers(key))
                with urlopen(breq, timeout=12) as br:
                    bdata = json.loads(br.read())
            except Exception:
                continue  # unreadable blocker never freezes
            btask = bdata.get("task") if isinstance(bdata.get("task"), dict) else bdata
            out.append({
                "dependency_type": dep.get("dependency_type"),
                "depends_on_task_id": blocker,
                "blocker_completed_at": btask.get("completed_at"),
            })
        return out
    except Exception:
        return []


def soft_nudge_gate_reason(api_url: str, key: str, task_id: str) -> str:
    """Reason to withhold a soft-nudge, or '' to send it as usual.

    The soft-nudge is the THIRD fiddler path that pastes into a session by task_id
    (cf. gather_candidates' todo-feed and zombie-rescue, which both already check
    this). Its prompt tells the agent to "finish it or leave a hard-blocker
    comment" — wrong advice for a task frozen by a formal `blocks` dependency or
    genuinely the operator-gated, where there is nothing for the agent to do (#025659c8,
    following #8f0f9ef6). No triage move / comment here, same as zombie-rescue:
    both freeze states are agent-recoverable and self-clear.

    Fail-open by construction: an unreadable task (`fetch_task` returns None, e.g.
    a network blip) still gets nudged — same posture as `dep_freeze_reason`'s own
    callers, a transient error must never silently withhold real work."""
    t = fetch_task(api_url, key, task_id)
    if not t:
        return ""
    if is_human_gated(t):
        return "human-gated"
    return dep_freeze_reason(fetch_task_deps(api_url, key, task_id))


def fetch_active_task_ids(api_url: str, key: str) -> tuple[bool, set]:
    """Set of task ids currently in {todo, in_progress} for this agent.

    Completion (spec invariant 5) is detected by ABSENCE: a BUSY task that is no
    longer in todo/in_progress has moved to review/done/cancelled (or triage =
    parked) → the agent is free. This avoids parsing status_id→category from the
    single-task endpoint (which returns a bare status_id, not a category).

    Returns (ok, ids). ok=False if BOTH category fetches failed — caller must NOT
    treat an empty set as "completed" on a transient network blip.
    """
    ids: set = set()
    any_ok = False
    for cat in ("todo", "in_progress"):
        try:
            url = f"{api_url}/api/v1/agents/me/tasks?status_category={cat}&limit=200"
            data = _api_get(url, key)
            any_ok = True
            for t in data.get("tasks") or []:
                if t.get("id"):
                    ids.add(t["id"])
        except Exception as e:
            log("poll", f"list {cat} failed: {e}")
    return any_ok, ids


_agent_id_cache: dict = {}  # key -> agent UUID (resolved via /agents/me)


def fetch_agent_id(api_url: str, key: str) -> str | None:
    """The calling agent's own UUID (GET /agents/me), cached per key. Used to
    identify the agent's OWN checkout locks for release (never touch others')."""
    if key in _agent_id_cache:
        return _agent_id_cache[key]
    try:
        me = _api_get(f"{api_url}/api/v1/agents/me", key, timeout=15)
        aid = (me.get("agent") or me).get("id") if isinstance(me, dict) else None
        if aid:
            _agent_id_cache[key] = aid
        return aid
    except Exception as e:
        log("poll", f"fetch agent_id failed: {e}")
        return None


def release_checkout(api_url: str, key: str, task_id: str) -> bool:
    """Release THIS agent's checkout lock on a task: DELETE /tasks/{id}/checkout.
    Server-enforced ownership — 204 clears our own lock, 403 'held by another
    agent' for someone else's (so it can never touch a foreign lock); idempotent
    (204 even when no lock is held). Returns True on a clean release/no-op."""
    try:
        req = Request(
            f"{api_url}/api/v1/tasks/{task_id}/checkout",
            headers=_headers(key),
            method="DELETE",
        )
        with urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except HTTPError as e:
        if e.code == 403:
            log("reauth", f"release #{task_id[:8]}: lock held by another agent — left intact")
        else:
            log("reauth", f"release #{task_id[:8]} failed: HTTP {e.code}")
        return False
    except Exception as e:
        log("reauth", f"release #{task_id[:8]} failed: {e}")
        return False


def release_my_orphaned_checkouts(api_url: str, key: str, lane_id: str) -> int:
    """Release every Mesh checkout lock this agent still holds across its
    todo+in_progress tasks. Called after an auto-relaunch-on-reauth: the
    pre-relaunch session left its checkout(s) orphaned, and the feeder skips
    re-feeding any task whose `checked_out_by` is set (else frozen up to the 2h
    TTL). Returns the count released. Idempotent and own-locks-only (compared to
    our own agent_id AND server-enforced 403 for foreign locks)."""
    aid = fetch_agent_id(api_url, key)
    if not aid:
        log(lane_id, "orphaned-checkout release skipped — could not resolve own agent_id")
        return 0
    released = 0
    seen: set = set()
    for cat in ("todo", "in_progress"):
        try:
            data = _api_get(
                f"{api_url}/api/v1/agents/me/tasks?status_category={cat}&limit=200", key)
        except Exception as e:
            log(lane_id, f"orphaned-checkout scan {cat} failed: {e}")
            continue
        for t in data.get("tasks") or []:
            tid = t.get("id")
            co = t.get("checked_out_by") or t.get("checked_out_by_id")
            if not tid or tid in seen or co != aid:
                continue
            seen.add(tid)
            if release_checkout(api_url, key, tid):
                released += 1
                log(lane_id, f"released orphaned checkout #{tid[:8]} — re-feedable next sweep")
    return released


def post_comment(api_url: str, key: str, task_id: str, body: str) -> bool:
    try:
        payload = json.dumps({"body": body}).encode()
        req = Request(
            f"{api_url}/api/v1/tasks/{task_id}/comments",
            data=payload,
            headers=_headers(key, {"Content-Type": "application/json"}),
            method="POST",
        )
        with urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        log("comment", f"post on {task_id[:8]} failed: {e}")
        return False


def post_session_report(api_url: str, key: str, task_id: str,
                        tokens_in: int, tokens_out: int, model: str,
                        cost: float) -> bool:
    """POST per-task spend to Mesh /api/v1/agents/me/sessions/report so it lands
    on the task card (cost-summary endpoint), not just in a comment. Mirrors the
    dispatcher's reaper path — fiddler is the primary driver now, and without
    this its tasks show session_count=0 (the card block is hidden). Best-effort:
    logs and returns False on failure, never raises (must not break the loop)."""
    try:
        payload = json.dumps({
            "tokens_in": int(tokens_in),
            "tokens_out": int(tokens_out),
            "model": model or "",
            "estimated_cost": round(float(cost), 4),
            "task_id": task_id,
        }).encode()
        req = Request(
            f"{api_url}/api/v1/agents/me/sessions/report",
            data=payload,
            headers=_headers(key, {"Content-Type": "application/json"}),
            method="POST",
        )
        with urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        log("cost-report", f"session-report on {task_id[:8]} failed: {e}")
        return False


_triage_status_id_cache: dict = {}  # project_id -> triage status UUID


def _resolve_triage_status_id(api_url: str, key: str, project_id: str) -> str | None:
    """Resolve the triage status UUID for a project (cached).
    REST /move endpoint requires status_id, not status_slug."""
    if project_id in _triage_status_id_cache:
        return _triage_status_id_cache[project_id]
    try:
        req = Request(
            f"{api_url}/api/v1/projects/{project_id}/statuses",
            headers=_headers(key),
        )
        with urlopen(req, timeout=15) as resp:
            statuses = json.loads(resp.read().decode())
        for s in (statuses or []):
            if s.get("slug") == "triage" or s.get("category") == "triage":
                _triage_status_id_cache[project_id] = s["id"]
                return s["id"]
    except Exception as e:
        log("supervised-guard", f"resolve triage status for {project_id[:8]} failed: {e}")
    return None


def return_supervised_to_triage(api_url: str, key: str, task_id: str, project_id: str) -> None:
    """Move a supervised task back to triage (it was incorrectly placed in todo).
    Supervised tasks must stay in triage until a human manually moves them to in_progress."""
    status_id = _resolve_triage_status_id(api_url, key, project_id)
    moved = False
    if status_id:
        try:
            payload = json.dumps({"status_id": status_id}).encode()
            req = Request(
                f"{api_url}/api/v1/tasks/{task_id}/move",
                data=payload,
                headers=_headers(key, {"Content-Type": "application/json"}),
                method="POST",
            )
            with urlopen(req, timeout=15) as resp:
                moved = resp.status in (200, 201)
        except Exception as e:
            log("supervised-guard", f"move #{task_id[:8]} → triage failed: {e}")
    comment = (
        "[fiddler] ⚠️ This task is `delegation_level=supervised` but landed in `todo` — "
        "most likely moved there automatically (a sweep). "
        "Supervised tasks wait for a manual human signal: the operator must move the task to "
        "`in_progress` for the agent to start. "
        + ("Task returned to `triage`." if moved else
           "⚠️ Could not auto-return to triage — check manually.")
    )
    post_comment(api_url, key, task_id, comment)
    log("supervised-guard", f"#{task_id[:8]} supervised in todo → {'triage' if moved else 'comment-only (move failed)'}")


def parse_due_date(raw) -> datetime | None:
    """Mesh `due_date` → aware UTC datetime. None when absent OR unparseable.

    Unparseable deliberately yields None, which makes the caller FEED the task:
    a malformed date must never silently strand real work. Naive timestamps are
    read as UTC, matching how Mesh serialises them."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def rescue_skip_reason(t: dict, *, name: str, inflight: set, rescue_counts: dict,
                       rescue_seen_update: dict, recent: dict, now: float,
                       debounce: int, rescue_after: int, rescue_max: int) -> str | None:
    """Zombie-rescue eligibility: WHY must this in_progress task not be resumed now?

    Returns a reason string, or None when the task is a rescue candidate. Extracted
    from the inline loop in `run_daemon` so the decision can be tested against the
    real code instead of a re-implementation of it. The only mutation is the
    documented progress-aware reset of `rescue_counts`; every side effect (triage
    move, alert, paste) stays in the caller.

    Order is load-bearing and unchanged from the inline version: cheap checks first,
    so a task that will be skipped costs no API call.
    """
    tid = t.get("id")
    if not tid or tid in inflight:
        return "no-id-or-in-flight"
    if is_human_gated(t):
        return "pavel-gated"  # audit fix #1: never zombie-rescue a the operator-gated task
    # Scheduled-task gate (#202e68ee) — the SECOND feed path. This loop does not go
    # through gather_candidates, so the todo-side gate (#c3c906a9, ~line 845) never
    # covered it: a card scheduled for later was still resumed here on an idle lane.
    # The exact analogue in the dispatcher — `stale_redispatcher_loop`, same
    # fetch_in_progress_tasks input, same "resume a stale in_progress card" job — has
    # carried this gate since 2026-05-22, so this is parity on the same population,
    # not a new policy.
    #
    # The objection considered and rejected: on `todo` a future due_date reads as
    # "parked", but on `in_progress` it could equally read as a real deadline on live
    # unfinished work — and rescue exists precisely to resume that (Vega idle 23h on
    # his own in_progress, #d56da225). Measured 2026-07-28 across all 12 projects /
    # 3986 tasks: 73 carry a due_date and ZERO of them sit in `in_progress` (68 done,
    # 4 backlog, 1 todo); all 4 live in_progress tasks are undated. Both readings share
    # that one empty population, so neither harm is live today — but when it does occur,
    # NOT gating pastes into a live TUI lane a card the fleet explicitly scheduled for
    # later (and the progress-aware reset below re-arms the counter every time the agent
    # responds, so RESCUE_MAX does not bound the churn), whereas gating only defers the
    # auto-resume. The caller announces this skip via log_skip so a deferred rescue is
    # visible in fiddler.log rather than silently stranded, and the lane's other
    # detectors (agent-ops idle/BROKEN alerting) still see the open task.
    #
    # Narrow by construction: a PAST due date and an undated task are untouched, and an
    # unparseable one fails OPEN (parse_due_date → None).
    due_raw = t.get("due_date")
    if due_raw:
        due_dt = parse_due_date(due_raw)
        if due_dt and due_dt > datetime.now(timezone.utc):
            return f"scheduled_until_{due_raw}"
    # Progress-aware reset (the #1 "sluggishness" fix): if the task ADVANCED
    # (updated_at moved) since our last resume, the agent IS making progress — this is
    # normal multi-turn continuation, not a stuck task. Reset the escalation counter so
    # (a) a progressing task never false-escalates, and (b) a task that was previously
    # exhausted (rescue_max hit → "human owns it") but has since progressed is no longer
    # abandoned forever (the root of agents idle-for-hours on their own unfinished
    # in_progress work, e.g. Vega #d56da225 idle 23h).
    upd = t.get("updated_at") or ""
    seen = rescue_seen_update.get((name, tid))
    if seen and upd and upd != seen:
        rescue_counts[(name, tid)] = 0
    if rescue_counts.get((name, tid), 0) >= rescue_max:
        return "rescue-exhausted"  # no progress — alerted, a human owns it now
    last_done = recent.get((name, tid))
    if last_done is not None and now - last_done < debounce:
        return "debounce"
    if task_age_sec(t) < rescue_after:
        return "recently-updated"  # someone/something is on it
    return None


def run_zombie_rescue(api: str, key: str, tasks: list, *, name: str, lane_id: str,
                      session: str, st: dict, alerts, inflight: set,
                      rescue_counts: dict, rescue_seen_update: dict, recent: dict,
                      now: float, debounce: int, rescue_after: int,
                      rescue_max: int) -> bool:
    """P0-1 zombie rescue — resume ONE stale in_progress task on an idle lane.

    A task stuck in_progress with no updates (session died mid-task, daemon restart,
    abandoned slot) would otherwise hang forever: the dispatcher no longer watches
    feeder agents, and the fiddler only polls todo. The caller's health=="busy" guard
    already ensures we never rescue while the TUI is actively working.

    Returns True when a rescue prompt was pasted. Lives outside `run_daemon` so the
    whole decision — including the #202e68ee schedule gate — is exercised by tests
    against the real code path rather than a re-implementation of it.
    """
    for t in tasks:
        tid = t.get("id")
        skip = rescue_skip_reason(
            t, name=name, inflight=inflight, rescue_counts=rescue_counts,
            rescue_seen_update=rescue_seen_update, recent=recent, now=now,
            debounce=debounce, rescue_after=rescue_after, rescue_max=rescue_max)
        if skip:
            # Only the scheduled skip is announced: the others fire on every 12s tick
            # for ordinary reasons (debounce, fresh update) and would bury the log.
            # A deferred rescue is the one that must not be silent.
            if tid and skip.startswith("scheduled_until_"):
                log_skip(lane_id, tid, f"{skip} (zombie-rescue)")
            continue
        age = task_age_sec(t)
        # the operator-gate: task awaits a human decision — stop the rescue loop and move to
        # triage so it's visible without churn. Labels/title already cleared by
        # rescue_skip_reason; fetch comments here for the full check.
        if is_human_gated(t, comments=_fetch_task_comments(api, key, tid)):
            log(lane_id, f"zombie #{tid[:8]} the operator-gated → triage, not rescue")
            return_supervised_to_triage(api, key, tid, t.get("project_id", ""))
            post_comment(api, key, tid,
                         "[fiddler] the operator-gate detected on stuck in_progress task "
                         "(❓ Blocking @operator marker or decision label). "
                         "Moving to triage — awaits @operator; auto-rescue suspended.")
            continue
        # Dependency freeze (#8f0f9ef6) — the SECOND fiddler feed path. This loop does
        # not go through gather_candidates, so the todo-side gate never covered it: a
        # card whose blocker is recorded as a formal link (not a `Blocking @operator`
        # marker) was still resumed here on an idle lane. No triage move and no comment,
        # unlike the the operator-gate above — a dependency is agent-recoverable state, the
        # card unfreezes by itself the moment the blocker completes.
        _dep_reason = dep_freeze_reason(fetch_task_deps(api, key, tid))
        if _dep_reason:
            log_skip(lane_id, tid, f"{_dep_reason} (zombie-rescue)")
            continue
        n_try = rescue_counts.get((name, tid), 0) + 1
        rescue_counts[(name, tid)] = n_try
        # Baseline for the next tick's progress check inside rescue_skip_reason.
        rescue_seen_update[(name, tid)] = t.get("updated_at") or ""
        title = t.get("title", "?")
        rescued = False
        if n_try >= rescue_max:
            log(lane_id, f"rescue #{tid[:8]} attempt {n_try}/{rescue_max} — LAST try, alerting")
            alerts.fire(lane_id, "rescue-exhausted",
                        f"#{tid[:8]} '{title}' stuck in_progress ~{int(age/60)}m, "
                        f"{rescue_max} rescues failed — needs a human")
            post_comment(api, key, tid,
                         f"[fiddler] Stuck in_progress ~{int(age/60)}m; {n_try} automated "
                         f"rescues attempted. Escalating to a human — please triage.")
        log(lane_id, f"rescuing stuck in_progress #{tid[:8]} '{title}' "
                     f"(idle {int(age/60)}m, try {n_try}/{rescue_max}) → {session}")
        if paste_prompt(session, build_rescue_prompt(tid, title, int(age / 60))):
            st.update(state="BUSY", task_id=tid, task_title=title,
                      task_priority=t.get("priority"),
                      pasted_at=now, pasted_wall=time.time(),
                      last_feed_wall=time.time(), nudged=False)
            rescued = True
        return rescued  # one rescue per tick per lane
    return False


def gather_candidates(api: str, key: str, tasks: list, *, name: str, lane_id: str,
                      inflight: set, want_project: str | None, want_label: str | None,
                      recent: dict, now: float, debounce: int, cfg: dict,
                      side_effects: bool = True) -> tuple[list, list]:
    """Filter an agent's todo list to the tasks that are actually feedable RIGHT NOW.

    Returns (candidates, drops) — drops is [(task, reason)] for every task that was
    considered and rejected, so the caller can log WHY (#b7615572: five of these
    rejections used to be silent `continue`s, which made a starved task look lost).

    side_effects=False → read-only: no comments posted, no state written. The busy-lane
    starvation probe needs to see the queue without acting on it; only the IDLE feed
    path may emit the supervised / completion-signal ready-to-close comments.
    """
    candidates: list = []
    drops: list = []
    eval_parent_cache: dict = {}   # parent_id -> is-eval-fixture (per-call)
    umbrella_cache: dict = {}      # task_id -> all-open-children-blocked (per-call)
    for t in tasks:
        tid = t.get("id")
        if not tid:
            continue
        if tid in inflight:
            drops.append((t, "already in flight on another lane of this agent"))
            continue
        if want_project and t.get("project_id") != want_project:
            drops.append((t, f"outside this lane's project scope ({str(want_project)[:8]})"))
            continue
        if want_label and want_label not in (t.get("labels") or []):
            drops.append((t, f"missing require_label '{want_label}'"))
            continue
        # Scheduled-task gate (#c3c906a9). A future due_date means the card is
        # intentionally WAITING, not ready: parking work in `todo` with a due
        # date is how the fleet schedules it for later. Without this the feeder
        # re-pastes that card into the lane on every poll until the date lands.
        # The dispatcher has had this gate on both its paths (stale-redispatch
        # since 2026-05-22; pull-on-reap since 2026-07-28, #ba50c1a2 — where the
        # loop re-picked a card 10 min after reap at $22.47 a spawn). The feeder
        # was the last driver without it. Deliberately narrow: a PAST due date
        # and an undated task are untouched, so this cannot mute dated work at
        # large. Placed above the parent/umbrella lookups so a scheduled card
        # costs no API calls to reject.
        due_raw = t.get("due_date")
        if due_raw:
            due_dt = parse_due_date(due_raw)
            if due_dt and due_dt > datetime.now(timezone.utc):
                drops.append((t, f"scheduled_until_{due_raw}"))
                continue
        # Eval-fixture guard (recurring leak #31f7d853, 3rd recurrence):
        # generated children of an Agent-Eval golden decompose fixture
        # carry NO label themselves but their parent does. Never feed them
        # — agents have twice built + reverted a phantom oauth-device/
        # server (bob has no public API). Parent-only check; cached.
        if parent_is_eval_fixture(api, key, t, eval_parent_cache):
            drops.append((t, "eval-fixture child (golden parent)"))
            continue
        # Umbrella-churn guard (#6561d12d): a captain/master task whose whole
        # subtask tree is terminal-or-blocked has nothing actionable right now.
        # Feeding it anyway just re-triggers a full-tree re-review every cycle.
        if umbrella_all_children_blocked(api, key, t, umbrella_cache):
            drops.append((t, "umbrella — all open children blocked/gated/backlog"))
            continue
        last_done = recent.get((name, tid))
        if last_done is not None and now - last_done < debounce:
            drops.append((t, f"debounce — completed {int(now - last_done)}s ago (<{debounce}s)"))
            continue
        # Recently-done guard (incident #56a6d5b2): a done task reactivated by
        # a server event (comment SSE → dispatcher spawn → move_to_in_progress)
        # will appear here as todo/in_progress but with completed_at still set.
        # Skip it — the work is shipped; force a fresh assignment if truly needed.
        completed_at_str = t.get("completed_at")
        if completed_at_str:
            try:
                done_ts = datetime.fromisoformat(
                    completed_at_str.replace("Z", "+00:00")
                ).timestamp()
                recently_done_sec = cfg.get("recently_done_guard_sec", 3600)
                if time.time() - done_ts < recently_done_sec:
                    drops.append((t, f"recently-done guard ({int((time.time()-done_ts)/60)}m ago)"))
                    continue
            except Exception:
                pass
        # respect someone else's active checkout
        co = t.get("checked_out_by") or t.get("checked_out_by_id")
        if co:
            drops.append((t, f"checked out by {str(co)[:8]} — someone else holds the lock"))
            continue
        # completion_signal=True: agent explicitly signaled via PATCH that its
        # work is done. Emit READY_CLOSE_MARKER once so review-verify-driver
        # surfaces the task to the operator regardless of delegation_level.
        if t.get("completion_signal"):
            if side_effects:
                _sc = _fetch_task_comments(api, key, tid)
                if not any(READY_CLOSE_MARKER in (c.get("body") or "") for c in (_sc or [])[-5:]):
                    post_comment(api, key, tid,
                                 "[fiddler] ✅ completion_signal set — "
                                 "@operator, the agent finished its part. Move to done when satisfied. "
                                 f"{READY_CLOSE_MARKER}")
                    log("completion-signal",
                        f"#{tid[:8]} completion_signal=true → ready-to-close emitted")
            drops.append((t, "completion_signal=true — awaiting @operator's close"))
            continue
        # Supervised tasks in todo: intake-sweep no longer promotes these
        # (human_gate.py fix #0bc757b1·S1), so a supervised task in todo was
        # intentionally placed there by the operator. Emit a ready-to-close signal ONCE
        # (idempotent via READY_CLOSE_MARKER), then leave frozen — don't bounce.
        if t.get("delegation_level") == "supervised":
            if side_effects:
                _sc = _fetch_task_comments(api, key, tid)
                if not any(READY_CLOSE_MARKER in (c.get("body") or "") for c in (_sc or [])[-5:]):
                    post_comment(api, key, tid,
                                 "[fiddler] ✅ Supervised task ready for your close — "
                                 "@operator, move to done when satisfied. "
                                 f"{READY_CLOSE_MARKER}")
                    log("supervised-gate",
                        f"#{tid[:8]} supervised in todo — ready-to-close signal emitted")
            drops.append((t, "supervised in todo — awaiting @operator's close"))
            continue
        # audit fix #1 (fiddler half): never feed a the operator-gated task into a
        # session — that re-feed pressure is what makes agents flee to backlog
        # / close to done. Leave it frozen in review/triage for the operator.
        if is_human_gated(t):
            # Cheap (server-flag) path says gated — but if the operator responded
            # AFTER the ❓ Blocking, the freeze must lift and the owner be
            # fed to act on the reply (incident E1 #8594d87d: an approved
            # deploy hung ~a day because a comment never woke the agent).
            _gc = _fetch_task_comments(api, key, tid)
            if is_human_gated(t, comments=_gc):
                drops.append((t, "the operator-gated — left frozen"))
                continue
            if side_effects:
                log("human-gate",
                    f"release #{tid[:8]} — the operator responded after ❓Blocking, feeding owner")
        # Dependency freeze (#8f0f9ef6) — "frozen, the ask lives on #X". The freeze
        # an agent gives up when it correctly refuses to double-ping the operator; a formal
        # `blocks` edge carries it without adding a line to the operators queue.
        # Placed LAST on purpose: it is the only gate here that costs API calls, so
        # only a task that has survived every cheaper check pays for it.
        _dep_reason = dep_freeze_reason(fetch_task_deps(api, key, tid))
        if _dep_reason:
            drops.append((t, _dep_reason))
            continue
        candidates.append(t)
    return candidates, drops


def run_starvation_probe(api: str, key: str, cfg: dict, a: dict, st: dict, lane_id: str,
                         name: str, alerts, todo_cache: dict, inflight: set,
                         recent: dict, now: float) -> None:
    """IO wrapper around starvation_report(): while a lane cannot be fed, what is it
    making wait? Rate-limited to one probe per STARVE_PROBE_SEC per lane, and strictly
    read-only (side_effects=False) — it observes the queue, it never feeds or comments.
    Feeding policy is unchanged: a working agent is never preempted."""
    wall = time.time()
    if wall - (st.get("starve_last_probe") or 0.0) < STARVE_PROBE_SEC:
        return
    st["starve_last_probe"] = wall
    # Whole body guarded: this is OBSERVABILITY. A bug in it must never take the feed
    # loop down with it — a fleet that stops feeding is far worse than a missing log line.
    try:
        if name not in todo_cache:
            todo_cache[name] = fetch_todo_tasks(api, key)
        cands, _drops = gather_candidates(
            api, key, todo_cache[name], name=name, lane_id=lane_id, inflight=inflight,
            want_project=a.get("project_id"), want_label=a.get("require_label"),
            recent=recent, now=now, debounce=cfg.get("debounce_sec", 120), cfg=cfg,
            side_effects=False)
        logs, fires, new_waiting, new_alerted = starvation_report(
            cands, st.get("task_id"), st.get("task_priority"),
            st.get("starve_since") or {}, st.get("starve_alerted") or [], wall)
        st["starve_since"] = new_waiting
        st["starve_alerted"] = new_alerted
        for t, _waited, reason in logs:
            log_skip(lane_id, t["id"], reason, now_wall=wall)
        for t, waited in fires:
            held = st.get("task_id")
            alerts.fire(lane_id, "priority-starvation",
                        f"#{t['id'][:8]} '{str(t.get('title'))[:60]}' ({t.get('priority')}) has waited "
                        f"~{waited}m and cannot be fed: lane is "
                        + (f"busy on #{held[:8]} ({st.get('task_priority')})." if held
                           else "IDLE but its pane reads busy with no turn-done sentinel.")
                        + " fiddler never preempts a working agent — check the lane if this is wrong.")
    except Exception as e:
        log(lane_id, f"starvation probe failed (non-fatal): {e}")


def pick_task(tasks: list) -> dict | None:
    """Highest priority, then oldest created_at."""
    if not tasks:
        return None
    def keyf(t):
        rank = PRIORITY_RANK.get((t.get("priority") or "medium").lower(), 2)
        return (rank, t.get("created_at") or "")
    return sorted(tasks, key=keyf)[0]


def tasks_are_related(prev: dict | None, curr: dict | None) -> tuple[bool, str]:
    """Return (related, reason) if curr should continue the same claude context as prev.

    Heuristics checked in priority order:
      1. Explicit thread_id match (Phase4 — highest authority, set by orchestrators).
      2. Both share the same non-None parent_task_id (subtask siblings in an epic).
      3. One is the direct parent of the other.
      4. Both share ≥1 non-generic label (same topic/project area).
    """
    if not prev or not curr:
        return False, ""
    # 1. Explicit thread_id (Phase4 — highest precedence)
    prev_thread = get_thread_id(prev)
    curr_thread = get_thread_id(curr)
    if prev_thread and curr_thread and prev_thread == curr_thread:
        return True, f"thread:{prev_thread[:8]}"
    prev_id = prev.get("id")
    curr_id = curr.get("id")
    prev_parent = prev.get("parent_task_id")
    curr_parent = curr.get("parent_task_id")
    # 2. Subtask siblings — share the same parent epic/task
    if prev_parent and curr_parent and prev_parent == curr_parent:
        return True, f"parent:{str(prev_parent)[:8]}"
    # 3. Direct parent-child link
    if prev_id and curr_parent and prev_id == curr_parent:
        return True, f"child-of:{str(prev_id)[:8]}"
    if curr_id and prev_parent and curr_id == prev_parent:
        return True, f"parent-of:{str(curr_id)[:8]}"
    # 4. Shared non-generic labels
    prev_labels = {la for la in (prev.get("labels") or []) if la not in _GENERIC_LABELS}
    curr_labels = {la for la in (curr.get("labels") or []) if la not in _GENERIC_LABELS}
    shared = prev_labels & curr_labels
    if shared:
        return True, f"label:{sorted(shared)[0]}"
    return False, ""


# ----------------------------------------------------------------------------- #
# tmux protocol (spec §4 — push_to_tmux from telegram-bridge.py:424-470)
# ----------------------------------------------------------------------------- #

def tmux(*args, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([TMUX_BIN, *args], capture_output=True, timeout=kw.get("timeout", 5))


def session_exists(session: str) -> bool:
    try:
        return tmux("has-session", "-t", session).returncode == 0
    except Exception:
        return False


def capture_pane(session: str) -> str:
    try:
        cp = tmux("capture-pane", "-p", "-t", session)
        return cp.stdout.decode(errors="replace") if cp.returncode == 0 else ""
    except Exception:
        return ""


def session_age_sec(session: str) -> float | None:
    """Seconds since the tmux session was created, or None if unreadable."""
    try:
        r = tmux("display-message", "-p", "-t", session, "#{session_created}")
        if r.returncode != 0:
            return None
        return max(0.0, time.time() - float(r.stdout.decode().strip()))
    except Exception:
        return None


# Auto-relaunch a reauth-stuck session (the operator 2026-06-23). A token corrupted by a
# subscription blip won't self-recover by retrying — needs a fresh claude. But only
# relaunch when the ACCOUNT is healthy (>=N peers working); during an account-wide
# outage relaunching is futile, so hold for the human there.
REAUTH_AUTORELAUNCH = os.environ.get("FIDDLER_REAUTH_AUTORELAUNCH", "1") != "0"
REAUTH_MIN_PEERS = int(os.environ.get("FIDDLER_REAUTH_MIN_PEERS", "2"))
REAUTH_MAX_ATTEMPTS = int(os.environ.get("FIDDLER_REAUTH_MAX_ATTEMPTS", "3"))
REAUTH_COOLDOWN = int(os.environ.get("FIDDLER_REAUTH_COOLDOWN_SEC", "600"))
REAUTH_RESET = int(os.environ.get("FIDDLER_REAUTH_RESET_SEC", "1800"))

# Defer reauth recovery to the fiddler-reauth-watchdog for the lanes it owns (task
# #61955ab2). fiddler's relaunch_session injects the keys.env token but CANNOT clear a
# stale keychain credential, so a relaunched launchd lane comes back on /login and
# RE-BREAKS the lane the watchdog just healed via `launchctl kickstart` (the keychain-
# clean start path). The watchdog's 30-min per-agent cooldown then locks it out and the
# lane thrashes BROKEN for hours. Fix: one recoverer per lane. For any lane the watchdog
# can recover (launchd-managed OR a declared reauth_recovery.spawn_cmd — exactly its
# feeder_agents() ownership set), fiddler backs off and does NOT relaunch. For lanes the
# watchdog cannot touch (no launchd label, no spawn_cmd), fiddler's relaunch ladder
# stays as the only fallback.
REAUTH_DEFER_TO_WATCHDOG = os.environ.get("FIDDLER_REAUTH_DEFER_TO_WATCHDOG", "1") != "0"
# Safety net: if a deferred lane is still reauth-stuck after this long, the watchdog is
# likely down/failing — escalate ONCE so a broken lane never goes silent (a deferral is
# an early `continue`; per #20a9f391 it must still answer "who gets told?"). The watchdog
# polls every 300s and self-alerts the operator on a genuine token expiry, so this is generous.
REAUTH_DEFER_ESCALATE_SEC = int(os.environ.get("FIDDLER_REAUTH_DEFER_ESCALATE_SEC", "1800"))

# Stale-marker ladder (#038393a0). A visible auth marker on a session that still
# shows its idle prompt is remediated with `/clear` — non-destructive, and the
# proven manual fix for the 2026-07-21 kill-loop. Only when the marker COMES BACK
# after this many clear-cycles inside the window is it treated as a real login
# wall (and handed to the watchdog / relaunch ladder as before). One cycle is not
# evidence: a single 401'd turn leaves a marker that /clear removes for good.
REAUTH_STALE_MAX_CLEARS = int(os.environ.get("FIDDLER_REAUTH_STALE_MAX_CLEARS", "3"))
REAUTH_STALE_WINDOW_SEC = int(os.environ.get("FIDDLER_REAUTH_STALE_WINDOW_SEC", "1800"))

# Do not feed a session that was spawned seconds ago. Every 401 in the 2026-07-21
# incident landed on a feed into a ~14-19s-old session (10/10); a feed into the
# same lane minutes later ran clean. The trigger is NOT proven (a synthetic
# 14s-old session + same token did NOT reproduce — see the task's "residual
# unknown"), so this is cheap insurance, not a root-cause fix: it costs one poll
# interval on a lane that was just respawned and nothing otherwise.
MIN_FEED_SESSION_AGE_SEC = int(os.environ.get("FIDDLER_MIN_FEED_SESSION_AGE_SEC", "60"))

_LAUNCHD_LABELS_CACHE: dict = {"t": 0.0, "labels": frozenset()}


def _launchd_labels(ttl: int = 60) -> frozenset:
    """Set of `vc.entire.*` launchd labels from `launchctl list` (cached ttl seconds).

    Test override: FIDDLER_LAUNCHD_LABELS (comma list, even empty) skips launchctl.
    On a probe failure the last good set is kept — a transient launchctl hiccup must
    not silently drop a launchd lane back onto fiddler's re-breaking relaunch path.
    """
    override = os.environ.get("FIDDLER_LAUNCHD_LABELS")
    if override is not None:
        return frozenset(x for x in (l.strip() for l in override.split(",")) if x)
    now = time.time()
    if _LAUNCHD_LABELS_CACHE["labels"] and now - _LAUNCHD_LABELS_CACHE["t"] < ttl:
        return _LAUNCHD_LABELS_CACHE["labels"]
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
        labels = frozenset(re.findall(r"(vc\.entire\.[a-z0-9\-]+)", r.stdout))
    except Exception:
        labels = frozenset()
    if labels:
        _LAUNCHD_LABELS_CACHE["t"] = now
        _LAUNCHD_LABELS_CACHE["labels"] = labels
        return labels
    return _LAUNCHD_LABELS_CACHE["labels"]  # keep last good on failure (may be empty at boot)


def watchdog_owns_reauth(name: str, agent_cfg: dict) -> bool:
    """True if the fiddler-reauth-watchdog can recover this lane's reauth, so fiddler
    must NOT run relaunch_session for it. Mirrors fiddler-reauth-watchdog.py's
    feeder_agents() ownership: launchd-managed (a `vc.entire.<name>` label exists →
    kickstart) OR a declared `reauth_recovery.spawn_cmd` (kill+respawn)."""
    if not REAUTH_DEFER_TO_WATCHDOG:
        return False
    if f"vc.entire.{name.lower()}" in _launchd_labels():
        return True
    if ((agent_cfg.get("reauth_recovery") or {}).get("spawn_cmd") or "").strip():
        return True
    return False

# Auto-restart a dead lane (task d4aa62de). A tmux session that dies takes its lane
# down until a human notices — spark-gen stalled ~28h, Warden ~1h38m mid-task. The
# guards mirror the reauth ladder, plus a live-PEER floor: if most of the host's
# tmux is gone (launchd bounce, reboot, OOM) the failure is host-wide and 16
# concurrent relaunches would make it worse, so we hold for the human instead.
DEAD_AUTORESTART = os.environ.get("FIDDLER_DEAD_AUTORESTART", "1") != "0"
DEAD_MIN_PEERS = int(os.environ.get("FIDDLER_DEAD_MIN_PEERS", "2"))
DEAD_MAX_ATTEMPTS = int(os.environ.get("FIDDLER_DEAD_MAX_ATTEMPTS", "3"))
DEAD_COOLDOWN = int(os.environ.get("FIDDLER_DEAD_COOLDOWN_SEC", "600"))
DEAD_MAX_PER_TICK = int(os.environ.get("FIDDLER_DEAD_MAX_PER_TICK", "1"))

# A lane the feed gate keeps skipping is a lane nobody is watching. The gate defers
# any paste while the pane reads busy and no Stop-hook sentinel has landed since our
# last paste — correct while an agent works, but a pane that will NEVER emit another
# sentinel (crashed to a bare shell, hung TUI, session_health's catch-all "busy: no
# idle prompt") is skipped on every tick forever, in total silence.
#
# 2h, not 1h. The only false positive is a self-started agent grinding one turn without
# ever tripping the Stop hook, and that turn can legitimately run long. A real wedge is
# permanent, so a later detection costs nothing; a spurious mail to the operator costs trust.
WEDGED_BUSY_AFTER = int(os.environ.get("FIDDLER_WEDGED_BUSY_AFTER_SEC", "7200"))
# Re-page cadence while the SAME wedge episode persists (see busy_wedge_due).
WEDGED_BUSY_REPEAT = int(os.environ.get("FIDDLER_WEDGED_BUSY_REPEAT_SEC", "21600"))


def healthy_peer_count(alive_now: dict, agent_state: dict, self_lane_id: str) -> int:
    """Peers that are BOTH tmux-alive this tick AND not-unhealthy. Pure.

    Either source alone defeats the reauth relaunch floor, in opposite directions:
    `agent_state` alone reads its initial IDLE for every lane at daemon boot, so a
    host-wide tmux loss counts as N healthy peers on exactly the tick that matters.
    `alive_now` alone counts sessions that are up but sitting on a login screen, so
    an account-wide expiry looks healthy. The floor wants peers that are up AND working.
    """
    return sum(1 for lid, alive in alive_now.items()
               if lid != self_lane_id and alive
               and agent_state.get(lid, {}).get("state") in ("IDLE", "BUSY"))


def busy_wedge_due(block_since: float, last_alert: float, now: float) -> bool:
    """Has an IDLE-but-busy-paned lane been skipped long enough to alert? Pure.

    `block_since` is the wall ts of the first consecutive skip (0.0 = not blocked).
    `last_alert` is the wall ts of the last page for THIS episode (0.0 = none yet).
    The caller clears both when the gate stops tripping.

    This used to be alert-ONCE per episode, which made a wedge that nobody clears
    indistinguishable from one resolved five minutes later: Comet sat wedged on an
    interactive permission prompt for 16h with 86 feedable tasks starving, having
    paged exactly once (2026-07-21 20:45) and then gone quiet. The alert asks a
    HUMAN to attach — so if the human never does, the condition must keep saying so.
    A sustained work-stopping fault re-pages every WEDGED_BUSY_REPEAT; it never
    goes silent on its own, only on recovery. Same class as #5d66aa3b (a
    delta-only detector greens a sustained outage).
    """
    if not block_since:
        return False
    if not last_alert:
        return (now - block_since) >= WEDGED_BUSY_AFTER
    return (now - last_alert) >= WEDGED_BUSY_REPEAT


# A wedged pane has several very different causes and the operator response differs.
# The one that bit us (Comet, 16h) is a TUI blocked on an interactive confirmation
# dialog: `--permission-mode bypassPermissions` does NOT cover Claude Code's built-in
# dangerous-operation prompts, so the lane waits on a keystroke that no daemon sends.
# Name it in the page — "hung TUI, attach and check" sent nobody looking for a
# one-keystroke answer. Require BOTH a question and a numbered option so ordinary
# transcript text quoting either one alone cannot trip it.
_WEDGE_PROMPT_QUESTIONS = ("Do you want to proceed?", "Do you want to", "Do you trust")
_WEDGE_PROMPT_OPTION = re.compile(r"^\s*(?:❯\s*)?[12]\.\s+\S", re.M)


def wedge_hint(pane: str) -> str:
    """Operator hint for a wedged pane. Pure; '' when the cause is unrecognised."""
    if not pane:
        return ""
    if any(q in pane for q in _WEDGE_PROMPT_QUESTIONS) and _WEDGE_PROMPT_OPTION.search(pane):
        return ("the pane is AWAITING A HUMAN ANSWER to an interactive confirmation "
                "prompt (bypassPermissions does not cover Claude Code's built-in "
                "dangerous-operation dialogs). Nothing will clear this on its own — "
                "answer it in the pane (arrow to the safe option + Enter). ")
    return ""


def session_health(session: str) -> tuple[str, str]:
    """Returns (state, detail).
    state ∈ {ok, dead, reauth, reauth_stale, entitlement, busy}.
      dead         — no such tmux session
      reauth       — the TUI cannot accept input: a login-screen takeover, or an
                     auth marker with no idle prompt (human gate / watchdog)
      reauth_stale — an auth marker is on screen but the idle prompt IS there, so
                     the session is alive and pasteable. The marker is a
                     transcript line of unknown age, NOT proof of a live login
                     wall (#038393a0): the Claude TUI keeps a single failed turn's
                     "Please run /login · API Error: 401" visible for 15-20 min.
                     Caller /clear's it instead of relaunching.
      busy         — TUI not on idle prompt (spinner/working) → defer paste
      ok           — idle prompt visible, safe to paste
    """
    if not session_exists(session):
        return "dead", "no tmux session"
    pane = capture_pane(session)
    for m in ENTITLEMENT_MARKERS:
        if m in pane:
            return "entitlement", f"marker: {m!r}"
    auth_state, auth_detail = fiddler_pane.reauth_scan(pane)
    if auth_state == fiddler_pane.WALL:
        return "reauth", auth_detail
    # Working/waiting? Must not feed — and must not /clear a stale marker out from
    # under a live turn, so this check comes BEFORE reauth_stale.
    for m in BUSY_MARKERS:
        if m in pane:
            return "busy", f"working ({m!r})"
    if auth_state == fiddler_pane.TRANSCRIPT:
        return "reauth_stale", auth_detail
    # Idle if the "❯" prompt box is anywhere in the pane (it sits mid-pane with
    # blank rows below, so a tail-only check misses it).
    if IDLE_PROMPT_MARKER in pane:
        return "ok", "idle prompt"
    return "busy", "no idle prompt"


_WORKTREE_SCRIPT = str(HOME / "bin" / "worktree-session.sh")


def worktree_create(task8: str, repos: list[str]) -> None:
    """Create per-task isolated worktrees (task b695306e, fleet pattern #9).
    Non-fatal: if the script is missing or a repo fails, we log and continue."""
    if not repos or not os.path.isfile(_WORKTREE_SCRIPT):
        return
    try:
        subprocess.run(
            [_WORKTREE_SCRIPT, "create", task8, *repos],
            capture_output=True, timeout=30,
        )
    except Exception as exc:
        pass  # non-fatal: shared repo still works, just not isolated


def worktree_clean(task8: str) -> None:
    """Remove clean (no local commits, no dirty tree) session worktrees after task done."""
    if not os.path.isfile(_WORKTREE_SCRIPT):
        return
    try:
        subprocess.run(
            [_WORKTREE_SCRIPT, "clean", task8],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass  # non-fatal


def paste_prompt(session: str, prompt: str) -> bool:
    """load-buffer (stdin) → paste-buffer → sleep 0.3 → send-keys Enter.
    Retries on tmux failure with backoff (1s,3s,9s)."""
    delays = [1, 3, 9]
    for attempt in range(3):
        try:
            lb = subprocess.run([TMUX_BIN, "load-buffer", "-"],
                                input=prompt.encode(), capture_output=True, timeout=5)
            if lb.returncode != 0:
                raise RuntimeError(f"load-buffer rc={lb.returncode}: {lb.stderr.decode(errors='replace')[:120]}")
            pb = tmux("paste-buffer", "-t", session)
            if pb.returncode != 0:
                raise RuntimeError(f"paste-buffer rc={pb.returncode}: {pb.stderr.decode(errors='replace')[:120]}")
            time.sleep(0.3)
            sk = tmux("send-keys", "-t", session, "Enter")
            if sk.returncode != 0:
                raise RuntimeError(f"send-keys rc={sk.returncode}: {sk.stderr.decode(errors='replace')[:120]}")
            return True
        except Exception as e:
            log("paste", f"attempt {attempt+1}/3 on {session} failed: {e}")
            if attempt < 2:
                time.sleep(delays[attempt])
    return False


def pane_cwd(session: str) -> str:
    """Current working directory of the session's pane ('' on failure)."""
    try:
        cp = tmux("display-message", "-p", "-t", session, "#{pane_current_path}")
        return cp.stdout.decode(errors="replace").strip() if cp.returncode == 0 else ""
    except Exception:
        return ""


def transcript_bytes(session: str) -> int:
    """Size of the newest claude transcript jsonl for this session's workspace.
    P1-3: the transcript is re-read (cache_read) on EVERY turn of a persistent
    session — its size is the per-turn rate-limit burn. 0 if not found."""
    cwd = pane_cwd(session)
    if not cwd:
        return 0
    proj = HOME / ".claude" / "projects" / cwd.replace("/", "-")
    try:
        newest = max(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, default=None)
        return newest.stat().st_size if newest else 0
    except Exception:
        return 0


PRICE_PER_MTOK = {  # (input, output) $/MTok; cache_read = 0.1× input
    "claude-fable": (10.0, 50.0),
    "claude-opus": (5.0, 25.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
}


def transcript_usage_since(session: str, since_wall: float):
    """P2-2 per-task cost: sum usage from the session transcript for assistant
    entries newer than since_wall. Returns (out_tok, cache_read_tok, cost_usd,
    in_tok, model) or None. `in_tok` is the Mesh tokens_in total (input +
    cache_creation + cache_read, matching the dispatcher's reaper) and `model`
    is the last-seen model id — both feed the structured session-report so the
    spend lands on the task card, not just in a comment. Interactive sessions
    emit no jsonl cost-report (the dispatcher's source) — the transcript is the
    only per-task spend signal on the feeder."""
    cwd = pane_cwd(session)
    if not cwd:
        return None
    proj = HOME / ".claude" / "projects" / cwd.replace("/", "-")
    try:
        newest = max(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, default=None)
        if not newest:
            return None
        from datetime import datetime, timezone
        out_t = cr_t = in_t = 0
        cost = 0.0
        model_seen = ""
        seen_ids = set()  # dedup: TUI writes each assistant message 2-6× (same
        # message.id, identical usage) while streaming → ~2.6× over-count if summed.
        for line in newest.open(errors="replace"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get("message") or {}
            u = msg.get("usage")
            if not u:
                continue
            mid = msg.get("id")
            if mid:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            ts = d.get("timestamp") or ""
            try:
                ep = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if ep < since_wall:
                continue
            model = msg.get("model") or ""
            pin, pout = (5.0, 25.0)
            for pref, pr in PRICE_PER_MTOK.items():
                if model.startswith(pref):
                    pin, pout = pr
                    break
            i = u.get("input_tokens", 0) or 0
            o = u.get("output_tokens", 0) or 0
            cr = u.get("cache_read_input_tokens", 0) or 0
            cc = u.get("cache_creation_input_tokens", 0) or 0
            out_t += o
            cr_t += cr
            in_t += i + cr + cc  # Mesh tokens_in = all input variants summed
            if model:
                model_seen = model
            cost += (i * pin + o * pout + cr * pin * 0.1 + cc * pin * 1.25) / 1e6
        return (out_t, cr_t, cost, in_t, model_seen)
    except Exception:
        return None


# ----------------------------------------------------------------------------- #
# P1-5 Label→model routing (cutover fix — dispatcher's model-selection rebuilt)
# ----------------------------------------------------------------------------- #

_PHASE_MODEL: dict[str, str] = {
    "phase:discuss": "opus", "phase:plan": "opus", "phase:debug": "opus",
    "phase:execute": "sonnet", "phase:verify": "sonnet", "phase:ship": "sonnet",
}

_MODEL_SLUGS = ("fable", "opus", "sonnet", "haiku")


def resolve_task_model(labels: list[str], default_model: str = "sonnet") -> str:
    """Determine which Claude model a task should run on, mirroring
    CLAUDE-model-selection.md. Priority (highest first):
      1. model:X label override
      2. kind:visual → opus
      3. phase:* mapping
      4. caller-supplied default
    """
    for lbl in labels:
        if lbl.startswith("model:"):
            return lbl[6:]
    if "kind:visual" in labels:
        return "opus"
    for lbl in labels:
        if lbl in _PHASE_MODEL:
            return _PHASE_MODEL[lbl]
    return default_model


def session_current_model(session: str) -> str:
    """Read the model slug ('sonnet', 'opus', …) the session is currently
    running from its most-recent transcript jsonl. Returns '' if unknown."""
    cwd = pane_cwd(session)
    if not cwd:
        return ""
    proj = HOME / ".claude" / "projects" / cwd.replace("/", "-")
    try:
        newest = max(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                     default=None)
        if not newest:
            return ""
        for line in reversed(list(newest.open(errors="replace"))):
            try:
                model = (json.loads(line).get("message") or {}).get("model") or ""
                if model:
                    for slug in _MODEL_SLUGS:
                        if f"-{slug}" in model:
                            return slug
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _load_rollback(name: str) -> dict:
    """Load the agent's rollback config (workspace, model, env_file)."""
    rb = ROLLBACK_DIR / f"{name}.json"
    try:
        return json.loads(rb.read_text()) if rb.exists() else {}
    except Exception:
        return {}


def resolve_workspace(name: str, session: str, agent_cfg: dict | None = None,
                      allow_pane: bool = True) -> str | None:
    """The directory a relaunched session must start in, or None if undeterminable.

    Order: rollback file → fiddler.json `workspace` → live pane cwd.

    The rollback file is only ever written by agent-switch.py on a dispatcher→feeder
    migration, so agents BORN as feeders (spark-gen, Warden, Test) have none; the
    fiddler.json `workspace` key is the durable declaration for those. pane_cwd is a
    runtime convenience that reads the LIVE session — it returns nothing for exactly
    the case that needs it most, a dead one. Callers that must survive a death
    (`doctor`) pass allow_pane=False to check only the durable sources.

    Never returns $HOME. A lane relaunched there has no CLAUDE.md, no .mcp.json and
    no fetch-batch.sh: it looks alive while quietly doing the wrong thing, which is
    worse than staying dead. Refusing is the safe failure.
    """
    candidates = [
        ("rollback", _load_rollback(name).get("workspace")),
        ("config", (agent_cfg or {}).get("workspace")),
    ]
    if allow_pane:
        candidates.append(("pane-cwd", pane_cwd(session)))

    for src, cand in candidates:
        if not cand:
            continue
        cand = os.path.abspath(os.path.expanduser(str(cand)))
        if cand == str(HOME):
            log(name, f"workspace from {src} is $HOME — refusing (agents never run in $HOME)")
            continue
        if not os.path.isdir(cand):
            log(name, f"workspace from {src} is not a directory: {cand} — skipping")
            continue
        return cand
    return None


def _lane_default_model(cfg: dict, name: str) -> str:
    """The model a lane should come back on after a relaunch it did not choose."""
    return _load_rollback(name).get("model") or cfg.get("default_model", "sonnet")


def dead_restart_decision(attempts: int, last_attempt: float, alive_peers: int,
                          lane_count: int, tick_budget_left: int,
                          now: float | None = None) -> tuple[bool, str]:
    """Should a dead lane be auto-restarted right now? → (go, reason).

    Pure so the caps are testable without a daemon: the reauth ladder's guards were
    only ever exercised in prod. A single-lane config skips the peer floor — there
    are no peers to compare against, so the floor would deadlock it forever.
    """
    now = time.time() if now is None else now
    if not DEAD_AUTORESTART:
        return False, "disabled"
    if tick_budget_left <= 0:
        return False, "tick-budget"
    if lane_count > 1 and alive_peers < DEAD_MIN_PEERS:
        return False, f"peer-floor ({alive_peers} < {DEAD_MIN_PEERS})"
    if attempts >= DEAD_MAX_ATTEMPTS:
        return False, f"attempts-exhausted ({attempts})"
    if (now - last_attempt) < DEAD_COOLDOWN:
        return False, "cooldown"
    return True, "go"


def write_session_context(session: str, task_id: str,
                          thread_id: str | None, project_id: str | None) -> None:
    """Write per-session side-channel so mesh-mcp can auto-populate thread_id/source_task_id.

    File: ~/.fiddler/sessions/<session>/current.json (atomic write via temp+rename).
    Read by mesh-mcp via FIDDLER_STATE_FILE env var set in relaunch_session.
    """
    out_dir = STATE_DIR / "sessions" / session
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = {
        "task_id": task_id or "",
        "thread_id": thread_id or "",
        "project_id": project_id or "",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp = out_dir / "current.json.tmp"
    tmp.write_text(json.dumps(ctx))
    tmp.rename(out_dir / "current.json")



# NOTE (task #863d4d0a, root cause #8eba4210): _oauth_token_env() used to live
# here and its value was injected into every relaunched lane as
# `-e CLAUDE_CODE_OAUTH_TOKEN=...`. That was the ROOT CAUSE of the recurring
# "4-hour" 401 windows, not a cure for them:
#
#   The CLI resolves credentials in `oB()` as
#       if (env.CLAUDE_CODE_OAUTH_TOKEN) return ms();
#   i.e. it returns BEFORE ever reading the Keychain, and with the env var set
#   `ms()` builds {accessToken: <env>, refreshToken: null, expiresAt: null}.
#   A session built that way CANNOT refresh, by construction. After its first
#   401 the CLI silently copies the Keychain accessToken into its own env
#   (telemetry tengu_oauth_401_recovered_from_disk) — accessToken only, still no
#   refreshToken — so it sits on an 8h bomb. When that expires nothing on the
#   host can renew it (no lane can), and the fleet halts until a human logs in.
#   kill+respawn could never fix it: the new process got the same static token.
#
# With no env var, `oB()` falls through to the Keychain and gets a real
# refreshToken + expiresAt, so lanes renew themselves.
#
# The old docstring claimed "launchd-spawned claude can't reliably read the
# keychain". That premise is FALSE in this configuration and was verified so
# before removal: the login keychain is `no-timeout` (never self-locks), and in
# a pane of this very launchd-born tmux server a token-less
# `claude -p` returns OK and `security find-generic-password` reads fine.
# If you are about to re-add this injection, re-read #8eba4210 first.


def resolve_github_env_file(name: str, agent_cfg: dict | None = None) -> str | None:
    """Per-agent GitHub identity env file (~/.config/agents/<slug>-github.env).

    Precedence mirrors resolve_workspace: fiddler.json `env_file` on the live
    agent_cfg, falling back to the rollback snapshot written at the
    dispatcher→feeder migration. Returns None if neither source has one —
    some lanes (Warden, spark-gen, Test) are genuinely identity-less.
    """
    direct = (agent_cfg or {}).get("env_file")
    if direct:
        return direct
    return _load_rollback(name).get("env_file")


def _load_github_env_pairs(path: str) -> dict[str, str]:
    """Resolve a `~/.config/agents/*-github.env` file into a KEY→VALUE dict.

    Every such file is `export KEY=VALUE` lines meant to be `source`d (that IS
    the documented manual workaround: `source ~/.config/agents/<id>-github.env`).
    At least one of them self-references another var in the same file —
    your-github-bot-github.env has `export GH_TOKEN="$GITHUB_TOKEN"` — so this
    MUST go through a real shell rather than a naive KEY=VALUE line-splitter.
    A splitter would take the literal 4-character string `$GITHUB_TOKEN` as
    GH_TOKEN's value; tmux -e does no shell expansion, so that value would
    look resolved (non-empty, no exception) while silently being an invalid
    token — exactly the "looks fixed, still broken" failure mode this whole
    fix exists to close. Caught by live verification (task 09dc852d) after
    the first version of this function shipped with the naive parser.

    Raises on a missing file, an unreadable file, or `source` exiting
    non-zero — the caller decides how loud to be, this function never fails
    silently into an empty/partial identity.
    """
    path = os.path.expanduser(path)
    text = Path(path).read_text()  # raises FileNotFoundError if missing
    keys: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" in line:
            k = line.split("=", 1)[0].strip()
            if k:
                keys.append(k)
    if not keys:
        return {}

    sep = "\x01"
    script = ('source "$GH_ENV_FILE_PATH"; __gh_src_rc=$?; '
              + "; ".join(f'printf "%s{sep}" "${k}"' for k in keys)
              + '; exit $__gh_src_rc')
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=10,
        env={**os.environ, "GH_ENV_FILE_PATH": path})
    if proc.returncode != 0:
        raise RuntimeError(f"`source {path}` exited {proc.returncode}: "
                           f"{proc.stderr.strip()[:300]}")
    values = proc.stdout.split(sep)[:-1]  # trailing chunk after the last sep is ""
    return dict(zip(keys, values))


def relaunch_session(session: str, name: str, model: str,
                     lane_id: str = "relaunch", agent_cfg: dict | None = None) -> bool:
    """Kill and restart a tmux session on a new Claude model.

    ONLY call when the session is idle and the context window is fresh
    (post-/clear or first task) — never on a thread-continue.

    Returns True if the session is healthy and idle within the wait window; False
    if no workspace resolves (we refuse rather than land the lane in $HOME).
    """
    workspace = resolve_workspace(name, session, agent_cfg)
    if not workspace:
        log(lane_id, f"relaunch REFUSED for '{session}': no workspace resolves — "
                     f"no rollback file, no `workspace` key in fiddler.json for agent "
                     f"'{name}', and no live pane to read. Declare one to enable restart.")
        return False

    # GitHub identity (task 09dc852d): resolved + VALIDATED before we touch tmux
    # at all — same as the workspace check above, and for the same reason: a
    # configured-but-unreadable env_file must refuse the relaunch, not kill a
    # live/healthy session and only discover the problem afterward. Without
    # this, the new pane falls through to whatever GH_TOKEN the tmux SERVER's
    # environment happened to capture at boot (currently your-github-bot),
    # silently attributing this agent's commits/PRs to a different persona.
    # -e on new-session sets the var in THIS session's table, which wins over
    # the inherited server-wide default.
    gh_env_file = resolve_github_env_file(name, agent_cfg)
    gh_pairs: dict[str, str] = {}
    if gh_env_file:
        try:
            gh_pairs = _load_github_env_pairs(gh_env_file)
            if not gh_pairs:
                raise ValueError("file parsed to zero KEY=VALUE pairs")
        except Exception as e:
            log(lane_id, f"relaunch REFUSED for '{session}': github identity "
                         f"env_file '{gh_env_file}' configured for agent '{name}' "
                         f"but unreadable/empty ({e}) — refusing rather than "
                         f"launching under an unrelated ambient GitHub identity.")
            return False
    else:
        log(lane_id, f"relaunch {session}: WARN — no github env_file configured "
                     f"for agent '{name}' (no fiddler.json `env_file`, no rollback "
                     f"snapshot). Session will inherit whatever GitHub identity the "
                     f"tmux server captured at boot — verify with `gh auth status` "
                     f"before this lane pushes/opens a PR.")

    log(lane_id, f"relaunch {session} → --model {model} (workspace={workspace})")
    tmux("kill-session", "-t", session)
    time.sleep(1.0)

    state_file = str(STATE_DIR / "sessions" / session / "current.json")
    env_args = ["-e", f"FIDDLER_STATE_FILE={state_file}"]
    # No CLAUDE_CODE_OAUTH_TOKEN here on purpose — see the note above
    # _oauth_token_env()'s former home. Injecting it makes the session
    # refresh-incapable (task #863d4d0a, root cause #8eba4210).
    for k, v in gh_pairs.items():
        env_args += ["-e", f"{k}={v}"]
    if gh_pairs:
        log(lane_id, f"relaunch {session}: sourced GitHub identity from "
                     f"{Path(gh_env_file).name} ({len(gh_pairs)} vars)")

    res = tmux("new-session", "-d", "-s", session, "-n", "main", "-c", workspace,
               *env_args)
    if res.returncode != 0:
        log(lane_id, f"relaunch: new-session failed rc={res.returncode}")
        return False

    claude_cmd = (
        "claude --dangerously-skip-permissions "
        "--permission-mode bypassPermissions "
        f"--model {model}"
    )
    tmux("send-keys", "-t", session, claude_cmd, "Enter")

    # Wait up to 45s for the idle ❯ prompt.
    for _ in range(45):
        time.sleep(1)
        h, detail = session_health(session)
        if h == "ok":
            return True
        if h in ("dead", "reauth", "reauth_stale"):
            log(lane_id, f"relaunch: unexpected state {h} ({detail})")
            return False
    log(lane_id, "relaunch: timed out waiting for idle prompt")
    return False


def sentinel_fresh(session: str, since_wall: float) -> bool:
    """P1-2 Stop-hook sentinel: True if the agent's turn-done sentinel was touched
    after `since_wall`. Deterministic 'agent finished its turn' signal that doesn't
    depend on pane-scraping (which breaks on TUI string changes / code-on-screen).
    Sentinel is written by the Stop hook installed via install-fiddler-hook.py."""
    cwd = pane_cwd(session)
    if not cwd:
        return False
    f = STATE_DIR / "turn-done" / cwd.replace("/", "-")
    try:
        return f.stat().st_mtime > since_wall
    except OSError:
        return False


def clear_context(session: str) -> bool:
    """Send /clear to the claude TUI to recycle context before an unrelated task.

    Waits up to 10s for the idle prompt to reappear. Returns True if the paste
    succeeded regardless of whether idle was confirmed — we never block the next
    task on a slow or stuck /clear.
    """
    log("recycle", f"sending /clear to {session} — unrelated task, fresh context")
    if not paste_prompt(session, "/clear"):
        log("recycle", f"/clear paste failed on {session} — proceeding anyway")
        return False
    for _ in range(20):
        time.sleep(0.5)
        health, _ = session_health(session)
        if health == "ok":
            log("recycle", f"{session} idle after /clear")
            return True
    log("recycle", f"{session} not idle 10s after /clear — proceeding anyway")
    return True


# ----------------------------------------------------------------------------- #
# Alerts (anti-spam, spec §6). Default = log-only (safe for Phase1). TG only if
# alert_outbox + alert_chat_id configured AND reason is a human-gate.
# ----------------------------------------------------------------------------- #

class AlertGate:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._last: dict[tuple, float] = {}

    def fire(self, agent: str, reason: str, text: str) -> None:
        log("alert", f"[{agent}] {reason}: {text}")
        self._escalate(agent, reason, text)
        now = time.monotonic()
        k = (agent, reason)
        last = self._last.get(k)
        interval = self.cfg.get("alert_min_interval_sec", 1800)
        if last is not None and now - last < interval:
            return  # debounced
        self._last[k] = now
        outbox = self.cfg.get("alert_outbox")
        chat_id = self.cfg.get("alert_chat_id")
        if not outbox or not chat_id:
            return  # log-only mode (Phase1 default)
        try:
            rec = {"chat_id": str(chat_id), "text": f"⚠️ fiddler [{agent}] {reason}\n{text}"}
            with open(os.path.expanduser(outbox), "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            log("alert", f"outbox write failed: {e}")

    def _escalate(self, agent: str, reason: str, text: str) -> None:
        """Out-of-band sink for human-gate reasons.

        The TG outbox above is useless for exactly the incidents that matter most:
        a keychain expiry takes the bridge down with the fleet. reauth_escalate
        mails the operator via ops-host+gog instead, sharing nothing with this host's tmux.

        dead-no-workspace is a human gate too: only a human can declare the missing
        `workspace`, and the lane stays down until they do. Warden alerted into a
        log-only void across two daemon boots before a human noticed by accident.

        no-agent-key is the same shape reached through config drift instead of
        runtime health: only a human restores the key, and the lane is dead until
        they do. Both are per-lane, so reauth_escalate dedups them on kind+agent —
        but it also rolls them up: one bad fiddler.json edit trips every lane at
        once, and 16 lanes tripping in one sweep is one root cause, not 16 faults.

        wedged-busy belongs here for the same reason: we deliberately refuse to
        auto-recover a busy-looking pane (it may hold live work), so only a human
        can end it. Routing it to `alert_outbox` alone would be a no-op — outbox and
        chat_id are both null, i.e. log-only, which is the very silence the alert
        exists to break.

        Runs on a daemon thread — an unreachable ops-host would otherwise block the
        poll loop for the ssh timeout on every sweep, and the roll-up collector
        deliberately waits ~15s for the rest of the sweep to register.
        """
        if reason not in ("reauth", "entitlement", "dead-no-workspace",
                          "no-agent-key", "wedged-busy"):
            return
        try:
            from reauth_escalate import escalate
        except Exception as e:
            log("alert", f"escalate import failed: {e}")
            return
        threading.Thread(
            target=lambda: escalate(agent, reason, text),
            name=f"escalate-{reason}", daemon=True,
        ).start()


# ----------------------------------------------------------------------------- #
# SSE wake (P2-1) — same per-agent stream the dispatcher listens to:
#   GET {api}/api/v1/agents/me/events/stream  (X-Agent-Key, text/event-stream)
# We do NOT act on events or track cursors: any event just WAKES the poll loop so
# pickup latency drops from poll_interval (~12-20s) to ~1s. The poll stays the
# source of truth — a dropped SSE connection degrades gracefully to plain polling.
# ----------------------------------------------------------------------------- #

def sse_wake_loop(name: str, key: str, api_url: str,
                  wake: threading.Event, live_keys: dict) -> None:
    url = f"{api_url}/api/v1/agents/me/events/stream"
    while live_keys.get(name) == key:        # exit when agent removed/re-keyed
        try:
            req = Request(url, headers=_headers(key, {"Accept": "text/event-stream"}))
            with urlopen(req, timeout=300) as resp:
                log("sse", f"[{name}] stream connected")
                for raw in resp:
                    if live_keys.get(name) != key:
                        return
                    if raw.startswith(b"data:"):
                        wake.set()           # poll loop wakes and sorts it out
        except Exception as e:
            if live_keys.get(name) != key:
                return
            log("sse", f"[{name}] stream dropped ({type(e).__name__}) — reconnect in 15s")
            time.sleep(15)


def ensure_sse_threads(cfg: dict, api_url: str, wake: threading.Event,
                       live_keys: dict, threads: dict) -> None:
    """Keep one SSE thread per enabled agent; stale threads self-exit via live_keys."""
    current = {a["name"]: a.get("mesh_agent_key", "") for a in enabled_agents(cfg)}
    live_keys.clear()
    live_keys.update(current)
    for name, key in current.items():
        t = threads.get(name)
        if key and (t is None or not t.is_alive()):
            t = threading.Thread(target=sse_wake_loop,
                                 args=(name, key, api_url, wake, live_keys),
                                 daemon=True, name=f"sse-{name}")
            t.start()
            threads[name] = t


# ----------------------------------------------------------------------------- #
# State machine (per agent)
# ----------------------------------------------------------------------------- #
# agent_state[name] = {
#   "state": "IDLE"|"BUSY"|"UNHEALTHY",
#   "task_id": str|None, "task_title": str|None,
#   "pasted_at": float|None (monotonic), "nudged": bool,
#   "health": str, "updated": epoch,
# }
# _recent[(agent, task_id)] = monotonic ts of last completion (debounce)


def persist_state(agent_state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "agents": agent_state,
        }
        tmp = STATE_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
        tmp.replace(STATE_PATH)
    except Exception as e:
        log("state", f"persist failed: {e}")


def expand_lanes(cfg: dict) -> list:
    """P1-4 session pool: an agent entry may declare `tmux_sessions: [..]` (pool,
    for heavy agents like Atlas) or the classic `tmux_session: "name"`. Each
    (agent, session) pair becomes a LANE with its own state; all lanes of one
    agent share the Mesh identity/queue — checkout-by-agent plus the cross-lane
    in-flight set prevent double work."""
    lanes = []
    for a in enabled_agents(cfg):
        sessions = a.get("tmux_sessions") or ([a["tmux_session"]] if a.get("tmux_session") else [])
        for s in sessions:
            lane_id = a["name"] if len(sessions) == 1 else f"{a['name']}[{s}]"
            lanes.append({**a, "tmux_session": s, "lane_id": lane_id})
    return lanes


def _new_lane_state() -> dict:
    return {
        "state": "IDLE", "task_id": None, "task_title": None,
        "pasted_at": None, "nudged": False,
        # Seeded to NOW, not 0.0. The busy-lane feed gate asks "did the agent finish a
        # turn after our last paste?" — with 0.0 *any* turn-done sentinel ever written
        # answered yes, so restarting the daemon pasted a new task straight into a
        # session that was mid-batch (the "don't restart mid-batch" caution). This lane
        # has been pasted nothing yet: only a sentinel newer than this moment can vouch
        # that a busy-looking pane is actually free.
        "pasted_wall": time.time(),
        # NOT seeded, unlike pasted_wall above — and that is the entire point.
        # pasted_wall answers "may I paste into this lane?", so seeding it to NOW
        # is correct for the busy-lane gate but makes it a LIE about feeding: this
        # process never restores state.json (run() builds agent_state fresh), so
        # after every daemon restart all 15 lanes claim to have been fed at the
        # restart instant. agent-ops read pasted_wall as feed-liveness and would
        # have muted BROKEN fleet-wide for 2h after each restart (#a4a1a389).
        # This field is set ONLY where a paste actually happens, so None is
        # honestly "this lane has not been fed since the daemon started".
        "last_feed_wall": None,
        "health": "?", "updated": int(time.time()),
        "unhealthy_reason": None,  # which reason KIND put this lane UNHEALTHY (alert dedup)
        "dead_attempts": 0,       # auto-restarts spent on the current death
        "dead_last": 0.0,         # wall ts of the last auto-restart attempt
        "dead_block_logged": None,  # last logged restart-block reason (log dedup)
        "task_priority": None,    # priority of the in-flight task (starvation probe)
        "starve_last_probe": 0.0, # wall ts of the last busy-lane starvation probe
        "starve_since": {},       # task_id -> wall ts it first outranked the in-flight work
        "starve_alerted": [],     # task_ids already paged for (alert-once)
        "busy_block_since": 0.0,  # wall ts of the first consecutive busy-pane feed skip
        "busy_wedge_alerted": False,  # whether the wedged-busy gate has paged this episode
        "busy_wedge_last_alert": 0.0,  # wall ts of the last wedged-busy page (repeat cadence)
        "last_task": None,        # metadata of last fed task; drives context-recycle (Phase2-b)
        "fresh": True,            # P1-1: True = context window had no full preamble yet
        "last_clear_wall": time.time(),  # P1-3: when this context window started
        "thin_count": 0,          # P1-1 anti-drift: how many thin feeds since last full preamble
    }


def maybe_soft_nudge(api_url: str, key: str, session: str, st: dict, lane_id: str,
                     nudge_after: int) -> bool:
    """Send the idle soft-nudge, unless the task is gated (#025659c8). Cheap checks
    (age > nudge_after, not already nudged, health == "ok") are the CALLER's job —
    this only runs the paid gate check and the paste. Returns True iff a nudge was
    actually sent."""
    tid = st["task_id"]
    reason = soft_nudge_gate_reason(api_url, key, tid)
    if reason:
        log_skip(lane_id, tid, f"{reason} (soft-nudge)")
        return False
    log(lane_id, f"task #{tid[:8]} idle >{int(nudge_after/60)}m → soft nudge")
    if paste_prompt(session, build_nudge(tid)):
        st["nudged"] = True
        return True
    return False


def run_daemon(cfg: dict) -> None:
    api = cfg["mesh_api_url"]
    poll = cfg["poll_interval_sec"]
    nudge_after = cfg["nudge_after_sec"]
    task_timeout = cfg["task_timeout_sec"]
    debounce = cfg["debounce_sec"]
    rescue_after = cfg.get("rescue_after_sec", 1800)
    max_session_age = cfg.get("max_session_age_sec", 86400)
    max_transcript = cfg.get("max_transcript_bytes", 5_000_000)
    RESCUE_MAX = 3
    rescue_counts: dict[tuple, int] = {}  # (agent, task_id) -> consecutive no-progress rescue attempts
    rescue_seen_update: dict[tuple, str] = {}  # (agent, task_id) -> updated_at at last rescue (progress detection)
    alerts = AlertGate(cfg)

    agent_state: dict[str, dict] = {}     # lane_id -> lane state
    recent: dict[tuple, float] = {}       # (agent_name, task_id) -> completion ts
    for lane in expand_lanes(cfg):
        agent_state[lane["lane_id"]] = _new_lane_state()

    # SSE wake (P2-1)
    wake = threading.Event()
    sse_live_keys: dict[str, str] = {}
    sse_threads: dict[str, threading.Thread] = {}
    ensure_sse_threads(cfg, api, wake, sse_live_keys, sse_threads)

    log("run", f"fiddler started — {len(agent_state)} lane(s): {', '.join(agent_state) or '(none)'}")
    if not agent_state:
        log("run", "no enabled agents in config — idle loop")

    while True:
        loop_start = time.monotonic()
        # Hot-reload config each loop so `agent-switch` (which writes fiddler.json)
        # is picked up within one poll cycle — no daemon restart needed. Keep the
        # old cfg on a malformed read so a half-written file never kills the loop.
        try:
            new_cfg = load_config()
            if new_cfg.get("agents"):
                cfg = new_cfg
                _set_basic_auth(cfg)   # a config edit can add/rotate the gate credential
                # These two were captured once before the loop, so a hot-reload
                # silently did NOT pick them up — raising task_timeout for a
                # deploy-heavy lane had no effect until a daemon restart. Re-read
                # them here so the hot-reload the surrounding comment promises is
                # actually true for every tunable it touches.
                nudge_after  = cfg["nudge_after_sec"]
                task_timeout = cfg["task_timeout_sec"]
        except Exception as e:
            log("config", f"hot-reload failed, keeping previous: {e}")
        ensure_sse_threads(cfg, api, wake, sse_live_keys, sse_threads)

        # Per-tick caches: ONE Mesh fetch per AGENT per category per tick, however
        # many lanes the agent has (pool agents would otherwise multiply API load).
        todo_cache: dict[str, list] = {}
        active_cache: dict[str, tuple] = {}

        cur_lanes = expand_lanes(cfg)
        cur_ids = {ln["lane_id"] for ln in cur_lanes}
        for stale in [k for k in agent_state if k not in cur_ids]:
            del agent_state[stale]  # lane removed from config (hot-reload)

        # Liveness pre-pass. The dead-lane restart floor must not count peers from
        # agent_state: at boot every lane still reads its initial IDLE, so a
        # host-wide tmux loss would look like 15 healthy peers and stampede 16
        # relaunches. Ask tmux directly, once per tick, before anyone acts.
        alive_now = {ln["lane_id"]: session_exists(ln["tmux_session"]) for ln in cur_lanes}
        dead_restarts = 0  # per-tick budget; a relaunch blocks up to 45s

        for a in cur_lanes:
            name = a["name"]
            lane_id = a["lane_id"]
            session = a["tmux_session"]
            key = a.get("mesh_agent_key", "")
            st = agent_state.setdefault(lane_id, _new_lane_state())
            # Cross-lane in-flight (P1-4): tasks already being worked on another
            # lane of THIS agent must not be fed twice. Computed up here (not just
            # before the feed) because the BUSY-lane starvation probe needs it too —
            # a stale value from the previous lane would mis-report what is waiting.
            inflight = {s2["task_id"] for k2, s2 in agent_state.items()
                        if k2.split("[")[0] == name and s2["state"] == "BUSY" and s2["task_id"]}
            if not key:
                # Config drift is a fault like any other. This branch used to `continue`
                # before reaching the health gates below, so a dropped or mistyped
                # `mesh_agent_key` took the lane down forever in total silence — no
                # alert, no escalation, only `doctor` printing key✗MISSING to nobody.
                # fiddler.json is hot-reloaded, so a bad edit lands within one poll.
                if st.get("unhealthy_reason") != "no-agent-key":
                    alerts.fire(lane_id, "no-agent-key",
                                f"no `mesh_agent_key` for '{name}' in fiddler.json — the lane "
                                f"cannot authenticate to Mesh and will never be fed. Only a "
                                f"human can restore the key; holding.")
                st["state"] = "UNHEALTHY"; st["health"] = "no mesh_agent_key in config"
                st["unhealthy_reason"] = "no-agent-key"
                st["updated"] = int(time.time())
                continue

            health, detail = session_health(session)
            st["health"] = f"{health}: {detail}"
            st["updated"] = int(time.time())

            # Health gates that block all work. Alert-once is keyed on the reason KIND,
            # not on the shared UNHEALTHY bucket: a lane that goes dead (tmux collapse)
            # and comes back on a login screen must still escalate the reauth. Keying on
            # `state != UNHEALTHY` swallowed exactly that transition — the shape of the
            # 2026-07-10 incident, where sessions died before showing /login.
            if health == "dead":
                # Resolve BEFORE relaunching: a lane with no workspace must stay dead
                # and loud, never come back up in $HOME looking healthy.
                ws = resolve_workspace(name, session, a)
                reason = "dead-session" if ws else "dead-no-workspace"
                if st.get("unhealthy_reason") != reason:
                    if ws:
                        alerts.fire(lane_id, "dead-session",
                                    f"tmux session '{session}' is gone — auto-restart enabled "
                                    f"(workspace={ws}); the peer/attempt/cooldown guards decide "
                                    f"whether it fires.")
                    else:
                        alerts.fire(lane_id, "dead-no-workspace",
                                    f"tmux session '{session}' is dead AND no workspace resolves "
                                    f"(no rollback file, no `workspace` key in fiddler.json for "
                                    f"'{name}'). REFUSING to auto-restart — a lane launched in "
                                    f"$HOME has no CLAUDE.md/.mcp.json and would silently do the "
                                    f"wrong thing. Declare `workspace` to enable restart.")
                st["state"] = "UNHEALTHY"; st["unhealthy_reason"] = reason
                if not ws:
                    continue

                alive_peers = sum(1 for lid, ok in alive_now.items() if lid != lane_id and ok)
                _att = st.get("dead_attempts", 0)
                go, why = dead_restart_decision(
                    _att, st.get("dead_last", 0.0), alive_peers, len(cur_lanes),
                    DEAD_MAX_PER_TICK - dead_restarts,
                )
                if not go:
                    # Log each distinct block reason once: a lane can sit dead for hours
                    # and this loop runs every poll_interval.
                    if st.get("dead_block_logged") != why:
                        log(lane_id, f"dead: not restarting — {why}")
                        st["dead_block_logged"] = why
                    continue
                st["dead_block_logged"] = None

                st["dead_attempts"] = _att + 1
                st["dead_last"] = time.time()
                dead_restarts += 1
                log(lane_id, f"dead session + {alive_peers} live peers -> auto-restart "
                             f"(attempt {_att + 1}/{DEAD_MAX_ATTEMPTS}) in {ws}")
                try:
                    _cur = STATE_DIR / "sessions" / session / "current.json"
                    if _cur.exists():
                        _cur.rename(_cur.with_suffix(".json.dead-bak"))
                except Exception:
                    pass
                _model = _lane_default_model(cfg, name)
                if relaunch_session(session, name, _model, lane_id, a):
                    # A session that died mid-task left its Mesh checkout orphaned;
                    # the feed loop skips any checked-out task, so the work would
                    # freeze until the 2h TTL. Same reasoning as the reauth path. #662d9721
                    try:
                        n = release_my_orphaned_checkouts(api, key, lane_id)
                        if st.get("task_id"):
                            log(lane_id, f"was on #{st['task_id'][:8]} when it died — "
                                         f"{n} orphaned checkout(s) released")
                    except Exception as e:
                        log(lane_id, f"orphaned-checkout release errored (non-fatal): {e}")
                    log(lane_id, "dead-session auto-restart ok -> IDLE")
                    st["state"] = "IDLE"; st["task_id"] = None; st["pasted_at"] = None
                    st["nudged"] = False; st["fresh"] = True
                    st["last_task"] = None  # context lost with the session
                    st["last_clear_wall"] = time.time(); st["thin_count"] = 0
                    st["unhealthy_reason"] = None
                else:
                    log(lane_id, "dead-session auto-restart failed — staying UNHEALTHY, "
                                 "retry after cooldown")
                continue
            if health == "entitlement":
                if st.get("unhealthy_reason") != "entitlement":
                    alerts.fire(lane_id, "entitlement", f"session '{session}' shows a SUBSCRIPTION/CREDIT lapse - needs the operator to RENEW (auto-relaunch will not help; holding). {detail}")
                st["state"] = "UNHEALTHY"; st["unhealthy_reason"] = "entitlement"
                continue
            if health == "reauth_stale":
                # An auth marker is on screen but the TUI is at its idle prompt, so
                # the session is ALIVE and pasteable. The marker is a transcript
                # line of unknown age — the Claude TUI keeps one failed turn's
                # "Please run /login · API Error: 401" visible for 15-20 min, which
                # is exactly what made this lane UNHEALTHY (blocking every feed) and
                # fed the watchdog's 30-min kill-loop. #038393a0
                #
                # Remediate with the cheap, proven, NON-destructive step: /clear
                # drops the transcript (and the marker) without killing the session.
                # Only if the marker keeps COMING BACK across clear-cycles is this a
                # real login wall — fall through to the reauth ladder then.
                now_s = time.time()
                if now_s - st.get("stale_clear_first", 0.0) > REAUTH_STALE_WINDOW_SEC:
                    st["stale_clear_first"] = now_s
                    st["stale_clears"] = 0
                _n = st.get("stale_clears", 0)
                if _n < REAUTH_STALE_MAX_CLEARS:
                    st["stale_clears"] = _n + 1
                    log(lane_id, f"stale reauth marker on a live idle session — /clear "
                                 f"(cycle {_n + 1}/{REAUTH_STALE_MAX_CLEARS}), NOT relaunching. {detail}")
                    clear_context(session)
                    # The turn that produced the marker is dead — the TUI is back at
                    # its idle prompt with nothing running. Whatever task the lane
                    # held is abandoned, so release its Mesh checkout or the feed
                    # loop will skip that task until the 2h TTL. Same reasoning as
                    # the reauth path. #662d9721
                    try:
                        _held = st.get("task_id")
                        if _held:
                            n = release_my_orphaned_checkouts(api, key, lane_id)
                            log(lane_id, f"was on #{_held[:8]} at the stale marker — "
                                         f"{n} orphaned checkout(s) released; task re-feeds")
                    except Exception as e:
                        log(lane_id, f"orphaned-checkout release errored (non-fatal): {e}")
                    # Context is gone with the transcript: next feed must be a full
                    # preamble, and the thread cannot be continued.
                    st["fresh"] = True; st["last_clear_wall"] = now_s
                    st["thin_count"] = 0; st["last_task"] = None
                    st["state"] = "IDLE"; st["task_id"] = None; st["pasted_at"] = None
                    st["nudged"] = False; st["unhealthy_reason"] = None
                    continue
                # Marker survived REAUTH_STALE_MAX_CLEARS clear-cycles inside the
                # window → it is regenerated by every turn, i.e. a genuine auth
                # failure. Hand it to the normal reauth ladder below.
                log(lane_id, f"reauth marker returned after {_n} /clear cycles in "
                             f"{REAUTH_STALE_WINDOW_SEC // 60}m — treating as a REAL login wall")
                health = "reauth"
                detail = f"{detail}; survived {_n} /clear cycles"
                st["health"] = f"{health}: {detail}"
            if health == "reauth":
                st["state"] = "UNHEALTHY"; st["unhealthy_reason"] = "reauth"
                # DEFER to the fiddler-reauth-watchdog for the lanes it owns (#61955ab2).
                # Our relaunch_session re-injects the token but can't clear a stale
                # keychain credential, so it comes back on /login and RE-BREAKS the lane
                # the watchdog just healed (launchctl kickstart), whose 30-min cooldown
                # then locks it out → hours of BROKEN thrash. One recoverer, the working
                # one. NOT relaunching and NOT escalating here — the watchdog owns both
                # recovery and (on genuine token expiry) the the operator alert.
                if watchdog_owns_reauth(name, a):
                    now_w = time.time()
                    if not st.get("reauth_deferred"):
                        st["reauth_deferred"] = True
                        st["reauth_deferred_since"] = now_w
                        log(lane_id, "reauth → deferring recovery to fiddler-reauth-watchdog "
                                     "(launchd/spawn owns it); NOT relaunching (stale-keychain safe)")
                        # Free our orphaned Mesh checkout(s) now so the held task re-feeds
                        # once the watchdog heals the lane, instead of freezing to the 2h
                        # TTL (same reasoning as the relaunch path). #662d9721
                        try:
                            _held = st.get("task_id")
                            n = release_my_orphaned_checkouts(api, key, lane_id)
                            if _held:
                                log(lane_id, f"was on #{_held[:8]} at reauth — "
                                             f"{n} orphaned checkout(s) released")
                        except Exception as e:
                            log(lane_id, f"orphaned-checkout release errored (non-fatal): {e}")
                    elif (now_w - st.get("reauth_deferred_since", now_w) >= REAUTH_DEFER_ESCALATE_SEC
                          and not st.get("reauth_alerted")):
                        alerts.fire(lane_id, "reauth",
                                    f"session '{session}' has been reauth-stuck "
                                    f"≥{REAUTH_DEFER_ESCALATE_SEC // 60}m and the fiddler-reauth-watchdog "
                                    f"has NOT recovered it — check the watchdog "
                                    f"(vc.entire.fiddler-reauth-watchdog). {detail}")
                        st["reauth_alerted"] = True
                    continue
                # --- Watchdog can't recover this lane (no launchd label, no spawn_cmd):
                #     fiddler's relaunch_session ladder is the only recoverer. ---
                healthy_peers = healthy_peer_count(alive_now, agent_state, lane_id)
                _att = st.get("reauth_attempts", 0)
                _cooled = (time.time() - st.get("reauth_last", 0)) >= REAUTH_COOLDOWN
                # ACT FIRST, ALERT AFTER (the operator 2026-07-14). The old code fired the human
                # escalation on the FIRST reauth tick — i.e. BEFORE the auto-relaunch it was
                # about to attempt. On 2026-07-14 that emailed the operator "go run /login" at
                # 09:29:07 and the auto-relaunch healed the lane at 09:29:09, two seconds
                # later; the fleet never needed him. Escalate only when auto-heal genuinely
                # CANNOT run (disabled / no healthy peers / attempts exhausted) — not while
                # it is merely waiting out the inter-attempt cooldown. `reauth_alerted` is
                # the dedup (reset on heal, so a later genuine failure alerts again).
                _autoheal_possible = (REAUTH_AUTORELAUNCH
                                      and healthy_peers >= REAUTH_MIN_PEERS
                                      and _att < REAUTH_MAX_ATTEMPTS)
                if not _autoheal_possible and not st.get("reauth_alerted"):
                    alerts.fire(lane_id, "reauth", f"session '{session}' shows a login/expired screen — needs human subscription re-login. {detail}")
                    st["reauth_alerted"] = True
                if _autoheal_possible and _cooled:
                    st["reauth_attempts"] = _att + 1
                    st["reauth_last"] = time.time()
                    log(lane_id, f"reauth + account healthy ({healthy_peers} peers) -> "
                                 f"auto-relaunch (attempt {_att + 1}/{REAUTH_MAX_ATTEMPTS})")
                    try:
                        _cur = STATE_DIR / "sessions" / session / "current.json"
                        if _cur.exists():
                            _cur.rename(_cur.with_suffix(".json.reauth-bak"))
                    except Exception:
                        pass
                    _model = _lane_default_model(cfg, name)
                    if relaunch_session(session, name, _model, lane_id, a):
                        _held = st.get("task_id")
                        # The pre-relaunch session left its Mesh checkout(s)
                        # orphaned; the feeder skips re-feeding any checked_out
                        # task, freezing it up to the 2h TTL. Free our own
                        # lock(s) now so the next sweep re-feeds immediately
                        # (and the fresh session can re-checkout). #662d9721
                        try:
                            n = release_my_orphaned_checkouts(api, key, lane_id)
                            if _held:
                                log(lane_id, f"was on #{_held[:8]} at reauth — "
                                             f"{n} orphaned checkout(s) released")
                        except Exception as e:
                            log(lane_id, f"orphaned-checkout release errored (non-fatal): {e}")
                        log(lane_id, "auto-relaunch ok -> IDLE")
                        st["state"] = "IDLE"; st["task_id"] = None; st["pasted_at"] = None
                        st["nudged"] = False; st["fresh"] = True
                        st["unhealthy_reason"] = None  # healed → next failure alerts again
                        st["reauth_alerted"] = False   # re-arm human escalation
                    else:
                        log(lane_id, "auto-relaunch failed — staying UNHEALTHY, retry after cooldown")
                elif _att >= REAUTH_MAX_ATTEMPTS:
                    log(lane_id, f"reauth persists after {_att} auto-relaunches — left for human")
                continue
            # recovered from unhealthy?
            if st["state"] == "UNHEALTHY" and health in ("ok", "busy"):
                log(lane_id, f"session recovered ({health}) → IDLE")
                st["state"] = "IDLE"; st["task_id"] = None; st["pasted_at"] = None
                st["nudged"] = False; st["last_task"] = None  # context lost during downtime
                st["fresh"] = True; st["last_clear_wall"] = time.time()
                st["reauth_attempts"] = 0  # genuine recovery resets auto-relaunch budget
                # clear reauth-deferral bookkeeping so the next reauth re-arms defer +
                # its safety-net escalation (#61955ab2)
                st["reauth_deferred"] = False; st["reauth_deferred_since"] = 0.0
                st["reauth_alerted"] = False
                st["dead_attempts"] = 0; st["dead_block_logged"] = None
                st["unhealthy_reason"] = None
                st["busy_block_since"] = 0.0; st["busy_wedge_alerted"] = False
                st["stale_clears"] = 0; st["stale_clear_first"] = 0.0  # #038393a0

            now = time.monotonic()

            if st["state"] == "BUSY":
                if name not in active_cache:
                    active_cache[name] = fetch_active_task_ids(api, key)
                ok, active = active_cache[name]
                if ok and st["task_id"] not in active:
                    log(lane_id, f"task #{st['task_id'][:8]} left todo/in_progress (completed) → IDLE")
                    # A completed task is proof the lane authenticates fine — any
                    # stale-marker clear-cycles it burned were false alarms. #038393a0
                    st["stale_clears"] = 0; st["stale_clear_first"] = 0.0
                    # P2-2: per-task observability — duration + notional cost from
                    # the transcript delta (no jsonl cost-report in interactive mode).
                    try:
                        dur_m = int((now - (st["pasted_at"] or now)) / 60)
                        u = transcript_usage_since(session, st.get("pasted_wall") or 0)
                        if u:
                            out_t, cr_t, cost, in_t, model = u
                            post_comment(api, key, st["task_id"],
                                         f"[fiddler] ✅ completed in ~{dur_m}m · session={session} · "
                                         f"~${cost:.2f} notional (out {out_t/1000:.1f}k tok, "
                                         f"cache_rd {cr_t/1_000_000:.1f}M)")
                            # P2-2b: also emit a structured session-report so the
                            # spend lands on the task card (cost-summary endpoint),
                            # not just in this comment. Without it fiddler tasks
                            # never reach agent_sessions → card block stays hidden.
                            post_session_report(api, key, st["task_id"],
                                                in_t, out_t, model, cost)
                    except Exception as e:
                        log(lane_id, f"cost-comment failed (non-fatal): {e}")
                    recent[(name, st["task_id"])] = now
                    # Clean up session worktrees if no local commits (task b695306e).
                    worktree_clean(st["task_id"][:8])
                    st.update(state="IDLE", task_id=None, task_title=None, pasted_at=None, nudged=False)
                elif not ok:
                    # transient fetch failure — keep waiting, don't drop the slot
                    pass
                else:
                    age = now - (st["pasted_at"] or now)
                    if age > task_timeout:
                        log(lane_id, f"task #{st['task_id'][:8]} timeout ({int(age)}s) → escalate + free slot")
                        post_comment(api, key, st["task_id"],
                                     f"[fiddler] Task has been open ~{int(age/60)}m in the interactive session "
                                     f"without reaching review/done. Freeing the session slot. If you're blocked, "
                                     f"leave a hard-blocker comment naming the exact ask.")
                        alerts.fire(lane_id, "task-timeout", f"#{st['task_id'][:8]} '{st['task_title']}' open ~{int(age/60)}m — slot freed")
                        # NOTE: do NOT move the task — that's the agent's right (invariant 5).
                        st.update(state="IDLE", task_id=None, task_title=None, pasted_at=None, nudged=False)
                    elif age > nudge_after and not st["nudged"]:
                        if health == "ok":
                            maybe_soft_nudge(api, key, session, st, lane_id, nudge_after)
                # BUSY agents never get a new paste — but a higher-priority task piling up
                # behind this one must not do so silently (#b7615572).
                run_starvation_probe(api, key, cfg, a, st, lane_id, name, alerts,
                                     todo_cache, inflight, recent, now)
                continue

            # st == IDLE → look for a task to feed
            if st["state"] != "IDLE":
                continue
            # Don't feed a session that was spawned seconds ago (#038393a0). Every
            # 401 in the 2026-07-21 kill-loop landed on a feed into a ~14-19s-old
            # session (10/10 respawn cycles); the same lane fed minutes later ran
            # clean. Costs one poll interval on a just-respawned lane.
            if MIN_FEED_SESSION_AGE_SEC > 0:
                _sage = session_age_sec(session)
                if _sage is not None and _sage < MIN_FEED_SESSION_AGE_SEC:
                    if not st.get("young_logged"):
                        log(lane_id, f"session is {int(_sage)}s old — holding first feed until "
                                     f"{MIN_FEED_SESSION_AGE_SEC}s (post-spawn 401 guard)")
                        st["young_logged"] = True
                    continue
            st["young_logged"] = False
            if health == "busy" and not sentinel_fresh(session, st.get("pasted_wall") or 0):
                # TUI mid-work (agent self-started, or stale busy text on the pane).
                # P1-2: the Stop-hook sentinel overrides — if the agent finished a
                # turn AFTER our last paste, the pane text is stale and the lane is
                # actually free (pane-scrape false-busy, e.g. code containing
                # 'esc to interrupt'-like strings).
                #
                # But a pane that will never emit another sentinel is skipped here on
                # every tick, forever, and no health gate above covers it: the lane is
                # IDLE, not UNHEALTHY. Silent-stuck beats silent-corruption, so we do
                # not force a paste or relaunch on top of a pane that may hold live
                # work — but it must not be silent. Alert and hold for the human.
                _now_wall = time.time()
                if not st.get("busy_block_since"):
                    st["busy_block_since"] = _now_wall
                else:
                    # Migrate state written before busy_wedge_last_alert existed: an
                    # already-latched episode carries no ts, so date its page to when
                    # the old alert-once path would have fired.
                    _last = st.get("busy_wedge_last_alert") or (
                        (st["busy_block_since"] + WEDGED_BUSY_AFTER)
                        if st.get("busy_wedge_alerted") else 0.0)
                    if busy_wedge_due(st["busy_block_since"], _last, _now_wall):
                        _mins = int((_now_wall - st["busy_block_since"]) / 60)
                        _again = " (STILL wedged — re-paging)" if _last else ""
                        alerts.fire(lane_id, "wedged-busy",
                                    f"lane is IDLE but tmux session '{session}' has read busy for "
                                    f"~{_mins}m with no turn-done sentinel{_again} — it has been "
                                    f"skipped by the feed gate on every poll since. "
                                    f"{wedge_hint(capture_pane(session))}"
                                    f"Otherwise likely a crashed-to-shell pane, a hung TUI, or a "
                                    f"stale busy scrape. NOT auto-recovering: the pane may hold "
                                    f"live work. Attach and check: `tmux attach -t {session}`.")
                        st["busy_wedge_alerted"] = True
                        st["busy_wedge_last_alert"] = _now_wall
                # Nothing can be fed into this lane at all — so EVERY eligible task is
                # being starved, not just a lower-ranked one. Say which (#b7615572).
                run_starvation_probe(api, key, cfg, a, st, lane_id, name, alerts,
                                     todo_cache, inflight, recent, now)
                continue
            # Gate passed → the lane is feedable; any wedge has resolved.
            st["busy_block_since"] = 0.0
            st["busy_wedge_alerted"] = False
            st["busy_wedge_last_alert"] = 0.0
            st["starve_since"] = {}
            st["starve_alerted"] = []

            # P0-1 zombie rescue — BEFORE new todo work. A task stuck in_progress with
            # no updates (session died mid-task, daemon restart, abandoned slot) would
            # otherwise hang forever: the dispatcher no longer watches feeder agents,
            # and the fiddler only polls todo. The health=="busy" guard above already
            # ensures we never rescue while the TUI is actively working.
            rescued = run_zombie_rescue(
                api, key, fetch_in_progress_tasks(api, key),
                name=name, lane_id=lane_id, session=session, st=st, alerts=alerts,
                inflight=inflight, rescue_counts=rescue_counts,
                rescue_seen_update=rescue_seen_update, recent=recent, now=now,
                debounce=debounce, rescue_after=rescue_after, rescue_max=RESCUE_MAX)
            if rescued:
                continue

            if name not in todo_cache:
                todo_cache[name] = fetch_todo_tasks(api, key)
            tasks = todo_cache[name]
            # Scoping (spec §8): optional per-agent narrowing so an agent's session
            # is only fed a bounded queue (and the Phase1 test never grabs real backlog).
            want_project = a.get("project_id")
            want_label = a.get("require_label")
            # Candidate-gathering (#b7615572): every rejection now names a reason
            # instead of vanishing into a silent `continue`.
            candidates, drops = gather_candidates(
                api, key, tasks, name=name, lane_id=lane_id, inflight=inflight,
                want_project=want_project, want_label=want_label,
                recent=recent, now=now, debounce=debounce, cfg=cfg, side_effects=True)
            for _t, _reason in drops:
                log_skip(lane_id, _t["id"], _reason)
            task = pick_task(candidates)
            if not task:
                continue
            # Runner-ups: a task that LOSES the priority sort is deferred, not lost —
            # say so, with what beat it. (Ranking was never the bug in #b7615572; the
            # silence around it was.)
            for _t in candidates:
                if _t.get("id") != task["id"]:
                    log_skip(lane_id, _t["id"],
                             f"lower rank than #{task['id'][:8]} "
                             f"({task.get('priority')}, created {str(task.get('created_at'))[:19]}) "
                             f"— stays queued, picked on a later cycle")
            _skip_seen.pop((lane_id, task["id"]), None)  # fed → forget its skip history

            tid = task["id"]
            title = task.get("title", "?")
            next_task_meta = {
                "id": tid,
                "title": title,
                "parent_task_id": task.get("parent_task_id"),
                "labels": task.get("labels") or [],
                "thread_id": get_thread_id(task),
            }
            # P1-3 forced recycle: a too-old or too-fat context window gets flushed
            # + cleared even for RELATED tasks. Age = boot snapshot (MEMORY.md,
            # CLAUDE.md, canonical state) staleness; size = the transcript re-read
            # on every turn (rate-limit burn on the subscription account).
            ctx_age = time.time() - st.get("last_clear_wall", time.time())
            tb = transcript_bytes(session)
            force_recycle = (ctx_age > max_session_age) or (tb > max_transcript)
            if force_recycle and st.get("last_task"):
                log(lane_id, f"forced recycle: ctx_age={int(ctx_age/3600)}h "
                             f"transcript={tb//1_000_000}MB → flush + /clear")

            # Context-recycle (Phase2-b) + thread-resume (Phase4):
            # - Related tasks: keep context, inject continuity hint into prompt.
            # - Unrelated tasks (or forced recycle): flush memory, /clear, fresh window.
            prev_task = st.get("last_task")
            continuity_hint = ""
            related, reason = tasks_are_related(prev_task, next_task_meta)
            if prev_task:
                if related and not force_recycle:
                    log(lane_id, f"#{tid[:8]} continues thread ({reason}) — keeping context")
                    prev_id = prev_task.get("id") or ""
                    prev_title = prev_task.get("title") or "?"
                    continuity_hint = build_continuity_hint(prev_id, prev_title, reason)
                else:
                    if not force_recycle:
                        log(lane_id, f"#{tid[:8]} unrelated to previous — clearing context")
                    flush_before_clear(session)  # P0-2: persist durable memory first
                    clear_context(session)
                    st["fresh"] = True
                    st["last_clear_wall"] = time.time()
                    st["thin_count"] = 0

            # P1-5 Model routing: on a fresh context window (boot / post-/clear),
            # check whether the session's model matches the task's label-driven
            # model. Relaunch when they differ. Thread-continues are intentionally
            # excluded — the live context must not be disrupted mid-thread.
            if st.get("fresh", True):
                rb_cfg = _load_rollback(name)
                lane_default = rb_cfg.get("model", cfg.get("default_model", "sonnet"))
                needed = resolve_task_model(
                    next_task_meta.get("labels") or [], lane_default
                )
                cur = session_current_model(session)
                if cur and cur != needed:
                    log(lane_id,
                        f"model route: #{tid[:8]} labels={next_task_meta.get('labels')} "
                        f"{cur}→{needed}")
                    if relaunch_session(session, name, needed, lane_id, a):
                        log(lane_id, f"model route: relaunch ok → {needed}")
                        st["fresh"] = True
                        st["thin_count"] = 0
                    else:
                        log(lane_id, f"model route: relaunch failed, proceeding on {cur}")
                elif cur:
                    log(lane_id,
                        f"model route: #{tid[:8]} → {needed} (already on {cur})")

            # P1-1 two-tier paste: full ACP preamble only into a FRESH context
            # window (boot / post-clear / post-recovery); thin prompt otherwise —
            # the wake-up reads are already in the live context.
            #
            # Anti-drift (P1-1): even within a live window, context compaction can
            # wash out routing rules on long threads. Force a full re-paste (without
            # clearing context) every full_refresh_every thin feeds so routing and
            # protocol anchors are periodically re-established.
            full_refresh_every = cfg.get("full_refresh_every", 5)
            thin_count = st.get("thin_count", 0)
            force_refresh = (
                not st.get("fresh", True)
                and full_refresh_every > 0
                and thin_count > 0
                and thin_count % full_refresh_every == 0
            )
            # #cd776e2a: comments ride EVERY tier. They belong to the TASK, not to the
            # context window — a task re-fed into a live session used to arrive with no
            # comments at all, so a review bounce ("fix asset X") was consumed as "run the
            # next batch" and the named defect stayed live in prod. A RETURNED task also
            # loses the continuity hint: "you are continuing this thread" is precisely the
            # wrong framing for "you got bounced, apply the correction".
            pc = prefetch_comments(api, key, tid, name, lane_id=lane_id)
            returned = pc.startswith("⚠️ [RETURNED TASK")
            if returned and continuity_hint:
                continuity_hint = ""
            if st.get("fresh", True):
                # A3: fresh window → prepend top-3 relevant memories (read-quality from turn
                # one, like the dispatcher's old prefetch).
                pf = prefetch_memories(api, key, title, name)
                prompt = pf + pc + build_prompt(tid, title, continuity_hint)
                tier = "full" + ("+pf" if pf else "") + ("+pc" if pc else "")
            elif force_refresh:
                # Protocol refresh — no context clear; re-paste full preamble to counteract
                # compaction-induced drift.
                prompt = pc + build_prompt(tid, title, continuity_hint)
                tier = "full-refresh" + ("+pc" if pc else "")
            else:
                prompt = pc + build_thin_prompt(tid, title, continuity_hint)
                tier = "thin" + ("+pc" if pc else "")
            if returned:
                tier += "+BOUNCE"
            # Full preamble pasted (fresh window or protocol refresh) → the thin streak
            # restarts. Keyed on the flags, not on the tier STRING: the string carries
            # +pf/+pc/+BOUNCE suffixes, so a `tier in (...)` match silently missed every
            # enriched full paste and let thin_count drift upward.
            is_full_paste = st.get("fresh", True) or force_refresh
            log(lane_id, f"feeding task #{tid[:8]} '{title}' → {session} [{tier}]")
            # Per-task worktree isolation (task b695306e): create isolated worktrees
            # before pasting so the agent works in a fresh, uncontested working tree.
            worktree_create(tid[:8], a.get("repos") or [])
            if paste_prompt(session, prompt):
                new_thin_count = 0 if is_full_paste else thin_count + 1
                st.update(state="BUSY", task_id=tid, task_title=title,
                          task_priority=task.get("priority"),
                          pasted_at=now, pasted_wall=time.time(),
                          last_feed_wall=time.time(), nudged=False,
                          last_task=next_task_meta, fresh=False,
                          thin_count=new_thin_count)
                try:
                    write_session_context(session, tid,
                                          next_task_meta.get("thread_id"),
                                          task.get("project_id"))
                except Exception as exc:
                    log(lane_id, f"write_session_context failed (non-fatal): {exc}")
            else:
                alerts.fire(lane_id, "paste-failed", f"could not paste #{tid[:8]} into '{session}' after retries")
                st["state"] = "UNHEALTHY"

        persist_state(agent_state)
        # SSE-aware sleep (P2-1): wake immediately on any Mesh event for our
        # agents; otherwise jittered poll interval as before.
        elapsed = time.monotonic() - loop_start
        sleep_for = max(1.0, poll * (1 + random.uniform(-0.2, 0.2)) - elapsed)
        if wake.wait(timeout=sleep_for):
            wake.clear()
            time.sleep(1.0)  # let Mesh commit the event before we poll


# ----------------------------------------------------------------------------- #
# CLI sub-commands
# ----------------------------------------------------------------------------- #

def cmd_status(cfg: dict) -> int:
    if not STATE_PATH.is_file():
        print("fiddler: no state file yet (daemon not running?)")
        return 1
    snap = json.loads(STATE_PATH.read_text())
    print(f"fiddler state @ {snap.get('updated')}")
    for name, st in snap.get("agents", {}).items():
        tid = (st.get("task_id") or "")[:8]
        line = f"  {name:<10} {st.get('state'):<10} {('#'+tid) if tid else '-':<10} {st.get('health','')}"
        print(line)
    return 0


def _mcp_stale_map() -> dict:
    """{workspace: worst stale record} from mcp_presence_watchdog --stale --json.

    An MCP stdio server is spawned once at session init and Claude Code never
    respawns it, so a lane can run code committed days ago (task b2617187). Probe
    failure yields None (rendered `mcp:?`), NEVER an empty "everything is fresh"
    map — a broken probe must not read as a green check.
    """
    wd = Path.home() / "bin/mcp_presence_watchdog.py"
    if not wd.is_file():
        return None
    for py in ("/opt/homebrew/bin/python3", sys.executable, "python3"):
        try:
            out = subprocess.run([py, str(wd), "--stale", "--json"],
                                 capture_output=True, text=True, timeout=45)
            if out.returncode != 0:
                continue
            recs = json.loads(out.stdout or "[]")
        except Exception:
            continue
        worst = {}
        for r in recs:
            ws = (r.get("workspace") or "").rstrip("/")
            if not ws:
                continue
            if ws not in worst or r.get("behind_sec", 0) > worst[ws].get("behind_sec", 0):
                worst[ws] = r
        return worst
    return None


def cmd_doctor(cfg: dict) -> int:
    agents = enabled_agents(cfg)
    if not CONFIG_PATH.is_file():
        print(f"No config at {CONFIG_PATH}. Template:\n")
        tmpl = {
            "mesh_api_url": "https://mesh.example.com",
            "poll_interval_sec": 20,
            "nudge_after_sec": 1200,
            "task_timeout_sec": 2400,
            "debounce_sec": 120,
            "alert_outbox": None,
            "alert_chat_id": None,
            "agents": [{
                "name": "test1", "tmux_session": "agent-test1",
                "workspace": "/Users/fleet/ClaudeCowork/test1",
                "mesh_agent_key": "tr_agent_...", "project_id": None, "enabled": True,
            }],
        }
        print(json.dumps(tmpl, indent=2))
        return 1
    print(f"config: {CONFIG_PATH}  ({len(agents)} enabled agent(s))")
    print(f"tmux:   {TMUX_BIN}")
    rc = 0
    unresolvable = []
    stale_map = _mcp_stale_map()
    for lane in expand_lanes(cfg):
        session = lane["tmux_session"]
        health, detail = session_health(session)
        flag = {"ok": "✓", "busy": "~", "reauth": "✗", "dead": "✗"}.get(health, "?")
        if health in ("dead", "reauth"):
            rc = 1
        keyf = "key✓" if lane.get("mesh_agent_key") else "key✗MISSING"
        tb = transcript_bytes(session)
        # Durable sources only: pane_cwd would mask the problem while the session
        # happens to be alive, and vanish the moment it dies — which is precisely
        # when the workspace is needed to bring the lane back.
        ws = resolve_workspace(lane["name"], session, lane, allow_pane=False)
        if ws:
            wsf = "ws✓"
        else:
            wsf = "ws✗UNRESOLVABLE"
            unresolvable.append(lane["lane_id"])
            rc = 1
        if stale_map is None:
            mcpf = "mcp:?"
        else:
            st = stale_map.get((ws or "").rstrip("/")) if ws else None
            if st:
                mcpf = (f"mcp:STALE({os.path.basename(st['script'])} newer by "
                        f"{st['behind_sec'] / 3600.0:.1f}h)")
                rc = max(rc, 1)
            else:
                mcpf = "mcp:fresh"
        print(f"  {flag} {lane['lane_id']:<14} {session:<16} {health:<7} {keyf}  {wsf:<16} "
              f"{mcpf:<20} transcript={tb//1_000_000}MB  {detail}")
    if unresolvable:
        print(f"\n  ✗ {len(unresolvable)} lane(s) cannot be auto-restarted: "
              f"{', '.join(unresolvable)}")
        print("    No rollback file and no `workspace` key in fiddler.json. If the tmux")
        print("    session dies, fiddler will REFUSE to restart it (never falls back to")
        print(f"    $HOME) and the lane stays down. Fix: add \"workspace\": \"/abs/path\" to")
        print("    the agent's entry in fiddler.json (hot-reloaded, no daemon restart).")
    # P2-3 drift check: warn when the live dispatcher preamble diverged from the
    # synced copy in fiddler_prompt.py (hand-sync required until full cutover).
    drift = dispatcher_preamble_drift()
    if drift:
        print(f"  ⚠ PROMPT DRIFT: {drift}")
        rc = max(rc, 1)
    else:
        print("  ✓ prompt module in sync with dispatcher preamble")
    return rc


def cmd_paste(cfg: dict, agent: str, task_id: str) -> int:
    a = next((x for x in cfg.get("agents", []) if x["name"] == agent), None)
    if not a:
        print(f"unknown agent '{agent}' (configured: {[x['name'] for x in cfg.get('agents', [])]})")
        return 1
    session = a["tmux_session"]
    key = a.get("mesh_agent_key", "")
    health, detail = session_health(session)
    if health in ("dead", "reauth"):
        print(f"session '{session}' not feedable: {health} ({detail})")
        return 1
    task = fetch_task(cfg["mesh_api_url"], key, task_id) if key else None
    title = (task or {}).get("title", "?")
    prompt = build_prompt(task_id, title)
    if paste_prompt(session, prompt):
        print(f"fed #{task_id[:8]} '{title}' → {session} (as {agent})")
        return 0
    print("paste failed")
    return 1


def cmd_relaunch(cfg: dict, agent: str, force: bool = False) -> int:
    """Restart an idle lane's tmux session so it reloads fresh MCP stdio servers /
    model (Claude Code never respawns a killed stdio MCP, so committed MCP code only
    goes live on relaunch — task b2617187). REFUSES a BUSY lane (relaunch rips the
    in-flight task) unless --force. Safe on idle: relaunch_session is idle-only."""
    al = agent.lower()
    def _match(x):
        return (x.get("name","").lower() == al
                or x.get("tmux_session","").lower() == al
                or al in [s.lower() for s in (x.get("tmux_sessions") or [])]
                or os.path.basename((x.get("workspace") or "").rstrip("/")).lower() == al)
    a = next((x for x in cfg.get("agents", []) if _match(x)), None)
    if not a:
        print(f"unknown agent '{agent}' (configured: {[x['name'] for x in cfg.get('agents', [])]})")
        return 1
    agent = a["name"]  # canonicalize for state.json lookup below
    session = a["tmux_session"]
    if STATE_PATH.is_file() and not force:
        try:
            st = (json.loads(STATE_PATH.read_text()).get("agents", {}).get(agent) or {})
            if st.get("state") == "BUSY":
                print(f"REFUSED: {agent} is BUSY (task #{(st.get('task_id') or '')[:8]}). "
                      f"Relaunch would rip the task — wait for idle or pass --force.")
                return 2
        except Exception:
            pass
    model = _lane_default_model(cfg, agent)
    ok = relaunch_session(session, agent, model, "relaunch-cli", a)
    print(f"relaunch {agent} ({session}) → {'OK (idle+healthy)' if ok else 'FAILED — check fiddler log'}")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="fiddler", description="Feed Mesh tasks into interactive claude tmux sessions.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="daemon loop (launchd)")
    sub.add_parser("status", help="print per-agent state")
    sub.add_parser("doctor", help="health-check all configured sessions")
    pp = sub.add_parser("paste", help="one-shot manual feed")
    pp.add_argument("agent")
    pp.add_argument("task_id")
    rl = sub.add_parser("relaunch", help="restart an idle lane's tmux session (fresh MCP/model); refuses if BUSY")
    rl.add_argument("agent")
    rl.add_argument("--force", action="store_true", help="relaunch even if BUSY (rips the task)")
    args = p.parse_args()

    cfg = load_config()
    _set_basic_auth(cfg)   # honor a gated instance for every subcommand, not just run
    if args.cmd == "run":
        try:
            run_daemon(cfg)
        except KeyboardInterrupt:
            log("run", "stopped (SIGINT)")
        return 0
    elif args.cmd == "status":
        return cmd_status(cfg)
    elif args.cmd == "doctor":
        return cmd_doctor(cfg)
    elif args.cmd == "paste":
        return cmd_paste(cfg, args.agent, args.task_id)
    elif args.cmd == "relaunch":
        return cmd_relaunch(cfg, args.agent, args.force)
    else:
        p.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
