#!/usr/bin/env python3
"""mesh-dispatcher: Multi-agent SSE listener for Mesh → Claude Code."""

import json
import re
import subprocess
import threading
import urllib.request
import urllib.error
import time
import os
import sys
import html as _html
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# --- Centralized MCP registry loader (task 75de4532) ------------------------
# Regenerates each agent's <workspace>/.mcp.json from ~/.config/mcp-registry/
# registry.yaml before every spawn. Import is GUARDED and the loader is
# fail-safe: if the module (or PyYAML) is unavailable, or the registry is
# malformed, the dispatcher runs exactly as before on the static .mcp.json
# files. regenerate_safe() never raises and only writes when the rendered
# config differs from on-disk — and only if the loader.enabled flag is set.
try:
    sys.path.insert(0, str(Path.home() / "bin"))
    import mcp_registry as _mcp_registry
except Exception as _mcp_import_err:  # noqa: BLE001
    _mcp_registry = None

# P0 #2 audit fix 2026-06-15: shared canonical human-gate labels (both spellings).
try:
    sys.path.insert(0, str(Path.home() / "ClaudeCowork/bob/scripts"))
    from human_gate import HUMAN_GATE_LABELS as _HUMAN_GATE_LABELS_DISPATCH
    from human_gate import gate_status as _gate_status
    from human_gate import is_human_gated as _is_human_gated
    from human_gate import gate_reason as _gate_reason
    from human_gate import is_automated_comment as _is_automated_comment
    from human_gate import masked_low as _gate_masked_low
    from human_gate import dep_freeze_reason
    from human_gate import DEP_FREEZE_ENABLED, DEP_BLOCKING_TYPES as _DEP_BLOCKING_TYPES
    # #7da3577d: these two sets used to be defined literally ~4400 lines below, making them
    # the second and third copies of the same membership. The PREDICATES stay here (they
    # drive two different things — see their definitions); only the membership moved.
    from human_gate import PASSIVE_WAIT_LABELS as _PASSIVE_WAIT_LABELS
    from human_gate import HUMAN_VERIFY_LABELS as _HUMAN_VERIFY_LABELS
except Exception:
    _HUMAN_GATE_LABELS_DISPATCH = frozenset({
        "blocked:pavel", "blocking:pavel", "decision", "needs:pavel",
        "kind:decision", "kind:human-verify", "human-verify", "needs:human",
    })
    def _gate_status(task, comments=None):  # type: ignore  # defensive fallback
        return ""
    # Fail-open fallbacks: if the shared predicate can't be imported, the feed gate
    # below degrades to "never gate" — a broken import must never freeze the fleet.
    def _is_human_gated(task, comments=None):  # type: ignore
        return False
    def _gate_reason(task, comments=None):  # type: ignore
        return ""
    # NOT fail-open (#84ab54fd): falling back to "no comment is automated" restores the
    # phantom-escalation bug, and one of the comments this must exclude is THIS FILE's own
    # re-verify ping — a self-sustaining loop that guarantees a the operator escalation. Carry a
    # literal copy of the driver markers so a broken import degrades to correct, not noisy.
    _AUTO_MARKERS_FALLBACK = (
        "🤖 auto:", "[fiddler]", "переведена в triage", "moving to triage",
        "**no-stall driver", "**pr driver", "**triage-drain (auto)", "**авто-перепроверка",
    )
    def _is_automated_comment(body):  # type: ignore
        b = (body or "").lower()
        return any(m in b for m in _AUTO_MARKERS_FALLBACK)
    # NOT fail-open either, for the same reason as the markers just above (#ba5a4f10):
    # degrading to "mask nothing" restores the defect this exists to close — an agent's
    # investigation prose quoting the marker in backticks re-arms the review sweep and
    # books a slot in the operators 🔴 digest. Length-preserving, like the canonical copy.
    _CODE_FENCE_FALLBACK = re.compile(r"```.*?```", re.DOTALL)
    _INLINE_CODE_FALLBACK = re.compile(r"`[^`\n]*`")
    def _gate_masked_low(body):  # type: ignore
        s = (body or "").lower()
        s = _CODE_FENCE_FALLBACK.sub(lambda m: " " * (m.end() - m.start()), s)
        return _INLINE_CODE_FALLBACK.sub(lambda m: " " * (m.end() - m.start()), s)
    # Fail-open, like the human-gate fallbacks above: a broken import must never
    # freeze the fleet, so the dependency gate degrades to "never freeze".
    DEP_FREEZE_ENABLED = False  # type: ignore
    _DEP_BLOCKING_TYPES = frozenset({"blocks"})
    def dep_freeze_reason(deps):  # type: ignore
        return ""
    # NOT fail-open, same reasoning as the markers and the mask above (#7da3577d). Degrading
    # to an empty set makes `_is_passive_wait`/`_is_human_verify` answer False for every card,
    # which does not merely disable a nicety: the stale-respawn park stops firing and every
    # passive/human-verify card reaches count==3 and auto-triages TO PAVEL with a "needs your
    # decision" comment — precisely the two leaks the operators 2026-06-03 and 2026-06-06 rules
    # exist to close. Carry the membership literally so a broken import degrades to correct.
    # `scripts/test_hgate_labelset_import_7da3577d.py` section D reads these two literals out
    # of this `except` branch by AST and fails if they drift from human_gate.py — an unused
    # fallback is exactly the copy that rots unnoticed.
    _PASSIVE_WAIT_LABELS = frozenset({
        "phase:verify", "kind:verify", "kind:monitor",
        "kind:passive", "no-pavel-triage", "awaiting-window",
    })
    _HUMAN_VERIFY_LABELS = frozenset({
        "kind:human-verify", "kind:human", "human-verify",
        "needs:human", "needs:macbook", "host:macbook",
    })

# --- Second the operator channel: gate-scope population for the review sweep (#f49ad8ca) ---
# The review sweep is the only channel that REPEATS an ask until it is answered
# (CLAUDE-orbit.md), and it looked exclusively at `status_category=review`, on the
# agent keys in mesh-agents.json — which is ONE enabled agent. So a the operator-gate on
# any other agent's card, or on a card in todo/triage/in_progress/backlog, could
# not reach the repeating channel at any age. #b0a81e17 (money-critical,
# evc-billing, urgent) sat 4 days and never surfaced here once.
#
# The population + predicate are IMPORTED from ~/bin/pavel-digest.py, not
# re-derived: `fetch_gate_scope_tasks()` already scans every project across the
# four non-review statuses and applies the canonical `human_gate.py` predicate
# UNION the digest-only soft ask-regex tier. Re-implementing it here is precisely
# the two-engines-two-verdicts divergence the 2026-06-15 audit (fix #2) paid for
# once already, and the acceptance criterion of this task forbids it.
#
# Why importing the DIGEST is right and copying its regex up into human_gate.py is
# not: human_gate.py is the fleet FREEZE predicate — a false positive there strands
# live work. The soft tier is deliberately looser because *surfacing* costs one
# extra line. This import consumes it on the surfacing side only; the freeze/feed
# call sites (`_human_gate_blocks_feed`, `_has_human_gate_signal`) are untouched.
#
# FAIL-SOFT, and the direction is deliberate: if the import breaks, the sweep
# degrades to exactly its pre-#f49ad8ca behaviour (review-only) rather than
# crashing the daemon thread that also drives escalation. A silent degrade is the
# thing to avoid, so the failure is logged loudly on first use, not swallowed.
_PD_GATE_SCOPE = None
_PD_IMPORT_ERR = ""
try:
    from importlib.machinery import SourceFileLoader as _SFL
    import importlib.util as _ilu
    _pd_path = str(Path.home() / "bin" / "pavel-digest.py")
    _pd_loader = _SFL("pavel_digest_for_dispatcher", _pd_path)
    _pd_spec = _ilu.spec_from_loader("pavel_digest_for_dispatcher", _pd_loader)
    _pd_mod = _ilu.module_from_spec(_pd_spec)
    _pd_loader.exec_module(_pd_mod)
    _PD_GATE_SCOPE = _pd_mod.fetch_gate_scope_tasks
except Exception as _pd_err:  # noqa: BLE001
    _PD_IMPORT_ERR = f"{type(_pd_err).__name__}: {_pd_err}"

# Kill-switch (house convention): REVIEW_SWEEP_GATE_SCOPE=0 restores the exact
# pre-#f49ad8ca review-only sweep without an edit or a rollback.
GATE_SCOPE_SWEEP_ENABLED = os.environ.get("REVIEW_SWEEP_GATE_SCOPE", "1") != "0"
# Per-message cap for the out-of-review section. Whatever it drops is named by id,
# never merely counted (#2836cd00: a rank cut with no roster makes an arbitrary
# card permanently unreachable, and the count alone reads as "that is all there is").
GATE_SCOPE_MAX = int(os.environ.get("REVIEW_SWEEP_GATE_SCOPE_MAX", "10"))
# Of those slots, at most this many may go to `backlog`-status cards. `backlog`
# means "not now", and left to compete on age alone parked cards take every slot.
GATE_SCOPE_BACKLOG_MAX = int(os.environ.get("REVIEW_SWEEP_GATE_SCOPE_BACKLOG_MAX", "3"))
# --- #effd0fbb: rotation of the tail ---------------------------------------------
# The ranked cut above is DETERMINISTIC and ageing moves every card equally, so the
# relative order of two non-urgent cards never changes. Measured over the 29 cycles
# in `~/logs/mesh-dispatcher.log` (2026-07-30T17:45 → 2026-08-01T13:38): 49 of the
# first cycle's 52 dropped cards were STILL dropped in the last, and only 5 ids ever
# left the dropped list at all — one of them (`b0a81e17`) by the urgent float, not by
# ageing. A card outside the top-10 rises only when somebody ahead of it LEAVES the
# scope, i.e. when the operator answers; until he does, the queue does not move at all.
#
# Fix: a SECOND, explicitly-labelled block of detail lines, filled least-recently-
# shown first, on top of the ranked cut — NOT a reshuffle of the ranked cut itself.
# That choice is the whole design and it is deliberate:
#   * the ranked top is what #2836cd00 / #73a96478 / #7a489d2f built and measured
#     (urgent money-critical #b0a81e17 must stay in front of the operator EVERY send). Any
#     scheme that rotates the top demotes it to 1-send-in-K by construction;
#   * therefore rotation is additive: nothing that is shown today stops being shown.
# Raising GATE_SCOPE_MAX is not an alternative and not a substitute — it changes how
# many cards starve, never whether they starve. Cap and rotation are orthogonal.
GATE_SCOPE_ROTATE_MAX = int(os.environ.get("REVIEW_SWEEP_GATE_SCOPE_ROTATE_MAX", "10"))
# Telegram rejects a message body over 4096 chars outright (HTTP 400). `_post_tg_nag`
# only queues into the outbox, so the limit is met by the BRIDGE — i.e. after this
# process has already logged «digest sent» and advanced its state. An over-long digest
# is therefore delivered to nobody while every local signal reports success. Budget
# below the hard limit: HTML entities (`&gt;`, escaped titles) make the string Telegram
# counts differ from the one measured here.
TG_MSG_SAFE_CHARS = int(os.environ.get("TG_MSG_SAFE_CHARS", "3600"))

# Feed-gate kill-switch: DISPATCHER_HUMAN_GATE=0 restores the old always-dispatch
# behaviour without an edit/rollback.
HUMAN_GATE_FEED_ENABLED = os.environ.get("DISPATCHER_HUMAN_GATE", "1") == "1"

# Lane-identity routing gate (task 98a1db69). A lane must not be handed a card
# that belongs to another agent AS IF it were its own. Kill-switch:
# DISPATCHER_ROUTING_GATE=0 restores the pre-fix always-dispatch-as-assignment
# behaviour without an edit/rollback.
ROUTING_GATE_ENABLED = os.environ.get("DISPATCHER_ROUTING_GATE", "1") == "1"

CONFIG = Path.home() / "bin" / "mesh-agents.json"
LOG_DIR = Path.home() / "logs"
LOG_DIR.mkdir(exist_ok=True)

CLAUDE_BIN = "/opt/homebrew/bin/claude"
# Runtime-aware spawn (task ae7efdd0): the dispatcher branches its spawn command
# by the per-agent `runtime` config key. `claude_code` (default) → `claude -p`;
# `comet` → the Nous Comet Agent runtime (DeepSeek mechanic tier, e.g. Lumen).
# The comet binary is a bash wrapper that unsets PYTHONPATH/PYTHONHOME and execs
# the venv comet — it lives in ~/.local/bin (NOT on the dispatcher's PATH), so
# we reference it by absolute path. Profile is selected with `-p <profile>`
# (NOT `--profile` — proven by the `lumen` alias = `comet -p lumen`).
HERMES_BIN = os.path.expanduser("~/.local/bin/comet")
ENV = {
    **os.environ,
    "PATH": "/opt/homebrew/bin:/opt/homebrew/opt/node@22/bin:/usr/local/bin:/usr/bin:/bin",
}

# --- Anti commit-clobber alert plumbing (task 30663152) ---
# the operators Telegram chat; Orbit's bridge (vc.entire.orbit-telegram, KeepAlive)
# is the always-on coordinator channel that drains this outbox to him.
PAVEL_CHAT_ID = "000000000"
RIKER_OUTBOX = "/Users/fleet/ClaudeCowork/bob/telegram-outbox.jsonl"
TG_REPLY = "/Users/fleet/bin/tg-reply"


def log(agent: str, msg: str):
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{ts} [{agent}] {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "mesh-dispatcher.log", "a") as f:
        f.write(line + "\n")


# --- SSE cursor (task 81f5cec1) -----------------------------------------------
# Per-agent last-event-id cursor survives dispatcher restarts. Written atomically
# via temp+rename; chmod 600 on dir. Backward-compat: only written if server
# actually sent an `id:` field (old server builds skip this entirely).

_CURSOR_DIR = Path("/tmp/mesh-cursors")
_CURSOR_DIR.mkdir(mode=0o700, exist_ok=True)


def _cursor_path(agent_slug: str) -> Path:
    return _CURSOR_DIR / f"{agent_slug}.txt"


def _read_cursor(agent_slug: str) -> str | None:
    try:
        v = _cursor_path(agent_slug).read_text().strip()
        return v or None
    except FileNotFoundError:
        return None


def _write_cursor(agent_slug: str, event_id: str) -> None:
    p = _cursor_path(agent_slug)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(event_id)
    tmp.replace(p)


def _delete_cursor(agent_slug: str) -> None:
    _cursor_path(agent_slug).unlink(missing_ok=True)


def _load_env_file(path: str, _seen: set = None) -> dict:
    """Parse a shell-style `export KEY=VALUE` env file into a dict.

    Supports double-quoted values and `$VAR` expansion against the already-parsed map.
    `source <file>` / `. <file>` lines are FOLLOWED (path relative to the sourcing
    file's directory if not absolute; ~ expanded), so a thin per-agent wrapper can
    reference single-source secret files — credential rotation then touches only the
    secret file (task 3b272632, lead-access model). Lines starting with `#` and blank
    lines are skipped. Returns {} on read failure.
    """
    import re
    result: dict[str, str] = {}
    if _seen is None:
        _seen = set()
    rp = os.path.realpath(os.path.expanduser(path))
    if rp in _seen:          # guard against source-cycles
        return result
    _seen.add(rp)
    base_dir = os.path.dirname(rp)
    try:
        with open(rp, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # follow `source FILE` / `. FILE` directives
                m_src = re.match(r'^(?:source|\.)\s+(.+)$', line)
                if m_src:
                    inc = m_src.group(1).strip().strip('"\'')
                    inc = os.path.expanduser(inc)
                    if not os.path.isabs(inc):
                        inc = os.path.join(base_dir, inc)
                    result.update(_load_env_file(inc, _seen))
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
                if not m:
                    continue
                k, v = m.group(1), m.group(2).strip()
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                # expand $VAR / ${VAR} against already-parsed
                v = re.sub(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?',
                           lambda mm: result.get(mm.group(1), os.environ.get(mm.group(1), "")), v)
                result[k] = v
    except OSError:
        pass
    return result


def _resolve_claude_env_file(agent_cfg: dict) -> str | None:
    """Map per-agent `account` config key to a Claude API credential env file.

    Resolution order (first match wins):
    1. claude_env_file: <path>         — explicit override
    2. ~/.config/agents/{account}-claude.env — shell KEY=VALUE format
    3. ~/.config/agents/claude-{account}-oauth.token — raw token file
       (single-line raw value treated as ANTHROPIC_API_KEY; Orbit's
       provisioning convention for the MacBook-Pro key, B3.P1)

    absent / no match → None (system ANTHROPIC_API_KEY used as-is)

    B3.P1: MacBook-Pro key → Atlas/Orbit (opus orchestrators); Mac-Mini key →
    all other agents. Secret provisioned by Orbit at ~/.config/agents/ (600).
    """
    direct = agent_cfg.get("claude_env_file")
    if direct:
        return os.path.expanduser(direct)
    account = agent_cfg.get("account")
    if not account:
        return None
    env_path = os.path.expanduser(f"~/.config/agents/{account}-claude.env")
    if os.path.exists(env_path):
        return env_path
    # Raw token file fallback (Orbit convention: claude-{account}-oauth.token)
    token_path = os.path.expanduser(f"~/.config/agents/claude-{account}-oauth.token")
    if os.path.exists(token_path):
        return token_path
    # File not yet provisioned — return conventional path so WARN is logged at spawn
    return env_path


# --- Dedup / concurrency lock (task 31bb7aad) ------------------------------
# Repeated SSE task.assigned/task.created for the SAME (agent, task_id) must
# NOT spawn parallel `claude` instances: they share cwd=workspace and clobber
# each other's files (observed: task 30663152 spawned 6+ parallel Garfields).
#
# Two layers of guarantee:
#   1. debounce  — a burst of repeats within DEBOUNCE_SEC of the last claim
#                  for that pair never re-spawns (covers SSE reconnect replay
#                  storms that arrive before a child pid is even registered).
#   2. liveness  — while a spawned child for the pair is still running we skip
#                  duplicates regardless of debounce (covers the whole multi-
#                  minute agent session). When the child exits the reaper
#                  clears the slot, so a later *sequential* re-assignment
#                  (after completion) spawns fresh as normal.
#
# State is thread-safe (one listener thread per agent) AND mirrored to a
# lockfile so it survives a launchd reload while child claudes are still
# alive (a redelivered event post-reload would otherwise duplicate them).
#
# Watchdog coordination (task 9fca65aa): LIVE_FILE is the shared registry of
# live (agent,task_id)->pid. Watchdog may read it; after it kills a stuck
# session it may drop that entry or simply let reap_dead() collect it —
# pid-dead detection is idempotent and safe to call concurrently.
_DISPATCH_LOCK = threading.Lock()
_LIVE: dict = {}          # (agent, task_id) -> slot dict (see claim_dispatch)
_RECENT: dict = {}        # (agent, task_id) -> monotonic ts of last claim
DEBOUNCE_SEC = 120        # repeat assigned/updated within this -> no re-spawn
COALESCE_WINDOW_SEC = int(os.environ.get("MESH_DISPATCH_COALESCE_SEC", "45"))  # B3.3: hold N-sec after session reap
RESERVE_TTL = 600         # a claimed-but-never-spawned slot older than this
                          # is stale (spawn crashed) -> reclaimable
LIVE_FILE = Path.home() / ".cache" / "mesh-dispatcher" / "live.json"

# --- Flap circuit breaker honor (task d52d7b0f, Watchdog gap 2) ------------
# mesh_watchdog.monitor_flap writes CACHE_DIR/breaker-<agent>-<task_id>.flag
# (JSON with `tripped_at` = wall-clock epoch) when it sees > FLAP_MAX_PER_HOUR
# launches of the same (agent, task_id) pair. Without honoring the flag here
# the alert "dispatcher не будет бесконечно рестартить" lies — dispatcher
# would keep spawning. Flag auto-clears after BREAKER_TTL_SEC so a one-off
# trip doesn't pin a task forever; within the window only manual unlink
# (or the operator resolving the underlying issue) clears it.
BREAKER_DIR = LIVE_FILE.parent
BREAKER_TTL_SEC = int(os.environ.get("MESH_DISPATCH_BREAKER_TTL_SEC", 24 * 3600))

# --- Stale in_progress re-dispatch (task 24e33cf2) -------------------------
# Headless `claude -p` sessions sometimes die (network blip, OOM, internal
# crash, conversation auto-compact stall) WITHOUT closing their task. The
# task sits in_progress with no comments / no updates and nobody re-fires
# task.assigned, so it rots silently. This loop periodically scans every
# agent's in_progress backlog and re-dispatches anything stale, going
# through the same claim_dispatch gate so a session that's actually still
# alive doesn't get duplicated (dedup 31bb7aad already covers that).
STALE_THRESHOLD_SEC = 30 * 60          # B5.3: 30min (was 4h) — fast silent-death recovery
TODO_STALE_THRESHOLD_SEC = 30 * 60     # 30 min for todo tasks (faster re-dispatch on missed assign)
STALE_CHECK_INTERVAL_SEC = 300         # one scan every 5 min (was 10m)
STALE_RESPAWN_COOLDOWN_SEC = 2 * 3600  # B5.3: 2h cooldown (was 4h); first fire at 30min, next at 2.5h
# --- auto-triage latch lifetime (#aca99f88) --------------------------------
# The count==3 latch (`_TRIAGED_AUTO`) is persisted; the ONLY code that used to
# discard it lived inside the `_STALE_LAST` prune loop, and `_STALE_LAST` is
# memory-only. After any restart the latch came back from disk with no matching
# `_STALE_LAST` key, so the prune iterated past it forever: an immortal latch.
# Two independent lifetimes now bound it — a status-driven release (the exact
# promise both the circuit-breaker comment and fetch_open_tasks' docstring
# already make: "a human moving the task back to todo/in_progress re-arms
# dispatch") and this absolute wall-clock backstop. Wall-clock, NOT monotonic:
# `_STALE_LAST` stores `time.monotonic()`, which resets to ~0 on reboot, so
# simply persisting that clock would make every restored age meaningless.
LATCH_TTL_SEC = 14 * 86400             # a latch older than this is dropped, whatever else
# A release re-arms the escalation ladder, so a card whose park MOVE keeps
# failing could ping-pong release→3 respawns→park-fail→release. Bound it: after
# this many releases the latch is permanent and the card is reported instead.
LATCH_MAX_RELEASES = 2
# --- shared respawn ladder (#3788c8f0) -------------------------------------
# `_STALE_COUNTS` is the per-card respawn ladder that escalates to
# auto-triage/park at RESPAWN_LADDER_MAX. It had exactly ONE writer —
# `stale_redispatcher_loop` — while FIVE other call sites could re-dispatch the
# same card. `_STALE_LAST` (the cooldown stamp) had the same single writer,
# which is why `_pull_next_task_for_agent` READ a cooldown it never wrote and
# therefore never throttled itself; its docstring's claim to honour "all the
# same caps & cooldowns as the stale-redispatch loop" covered the CAPS, not the
# LADDER.
#
# Measured 7d to 2026-08-03 (`~/logs/mesh-dispatcher-stdout.log`): 471 spawns :
# 245 distinct task_ids = 226 surplus (48%). By path: pull-on-reap 86 and
# @-mention 63 were completely unbounded (66% of surplus), against 67 for the
# one path that DID carry the ladder — despite that path firing on a 5-minute
# timer. Surplus sessions were 48.6% of sessions but only 30.4% of session-time
# (median 4.5 min vs 17.7): the arrive→recall→read-thread→yield signature.
#
# Do NOT "fix" a cap-hit row by raising MAX_PER_AGENT_SPAWNS. A cap-defer costs
# $0 (it returns strictly before subprocess.Popen), so raising the cap turns the
# row green by ADMITTING every surplus spawn — strictly more expensive.
RESPAWN_LADDER_MAX = 3
# --- progress reprieve (#19d9a1d1) -----------------------------------------
# The ladder counts RESPAWNS. Reaching its top says "we spawned this card three
# times", which is a fact about the dispatcher, not about the card: the measured
# split above is 64% `pull-on-reap` vs 36% `stale-redispatch`, so most of what
# drives a card to the give-up rung is the dispatcher's own reap-and-refill.
# Measured over 344 parks in ~/logs/mesh-dispatcher.log (2026-06-10 → 08-04):
# median `age` at the park is 0.5h and 88.9% are under 1.0h — `age` at the park
# is just STALE_THRESHOLD_SEC read back, because the loop fires the instant
# `age >= threshold` and escalates in the same pass. Raising the threshold would
# move that number and change nothing about WHICH cards get parked. 137 of those
# 344 (39.8%) had their `age` drop between two ladder fires, i.e. something moved
# `updated_at` mid-ladder — a checkout, a move, a live session.
#
# So the give-up test needs a second term that is about the CARD: has the
# executor said anything since we last spawned it. A card whose assignee
# commented after the last spawn is working, whatever the respawn count says,
# and gets its ladder reset instead of a park — but only PROGRESS_REPRIEVE_MAX
# times, so a card that comments without finishing still reaches the parking
# lane. Without that bound this is not a fix, it is a disabled watchdog.
PROGRESS_REPRIEVE_MAX = int(os.environ.get("PROGRESS_REPRIEVE_MAX", "3"))
# Race-aborted tasks (concurrent agents touching same repo → momentary
# detached HEAD → ABORT) deserve much faster retry than 4h. They are NOT
# real stale: the agent crashed for an infrastructure-transient reason.
REPO_UNSAFE_RETRY_SEC = 600            # 10 min — retry repo-unsafe aborts
# Consecutive unrecoverable reposync aborts on the SAME task before paging the operator
# in Telegram (task bfa8e55a). With BEHIND+DIRTY now auto-recovering, reaching the
# abort path means a genuine unrecoverable state; the first few are logged +
# commented on the task (Orbit-visible), the operator is escalated only past this count.
REPO_UNSAFE_ALERT_AFTER = int(os.environ.get("REPO_UNSAFE_ALERT_AFTER", "3"))
MAX_CONCURRENT_SPAWNS = int(os.environ.get("MAX_CONCURRENT_SPAWNS", "8"))
MAX_PER_AGENT_SPAWNS = int(os.environ.get("MAX_PER_AGENT_SPAWNS", "3"))
SPAWN_JITTER_SEC = int(os.environ.get("SPAWN_JITTER_SEC", "30"))
EMPTY_LOG_CRASH_GRACE_SEC = 60         # if a session reaps <Ns AND log==0 -> crashed
COST_REPORT_ENABLED = os.environ.get("MESH_COST_REPORT", "1") != "0"
COST_REPORT_TIMEOUT_SEC = 5            # POST timeout — don't block reaper
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
# Anthropic pricing per 1M tokens (USD), updated 2026-05-22.
# Sources: anthropic.com/pricing. Used for estimated_cost; Mesh stores the value
# we send, no server-side recompute.
MODEL_PRICING = {
    # (input, output, cache_read, cache_write) per 1M tokens
    "opus":   (15.0, 75.0, 1.50, 18.75),
    "sonnet": (3.0,  15.0, 0.30, 3.75),
    "haiku":  (1.0,  5.0,  0.10, 1.25),
}
CRASH_RETRY_BACKOFF_SEC = [120, 300, 900, 3600, 14400]  # 2m, 5m, 15m, 1h, 4h
CRASH_MAX_RETRIES = 5                  # after 5 crashes → escalate to triage, stop retrying
RATE_LIMIT_CLUSTER_WINDOW = 120        # crashes within this window across agents → API issue
RATE_LIMIT_CLUSTER_THRESHOLD = 3       # ≥N empty-log crashes in window → trigger auto-pause
RATE_LIMIT_KEYWORDS = (
    "Server is temporarily limiting",
    "rate limit",
    "rate_limit_exceeded",
    "Too Many Requests",
    "429 Too Many",
    "usage limit",
)
# Auth expiry, unlike a rate-limit, never clears on its own: retrying just burns
# another spawn. Between 07-08 and 07-10 this loop cost 1354 dead Orbit sessions —
# the log is non-empty ("Not logged in · Please run /login") and carries no
# rate-limit keyword, so it fell through both existing crash guards and every
# reaped session was immediately re-spawned. Only a human /login clears it.
#
# Keep these SPECIFIC. A false positive here pauses the whole fleet, so the bar is
# "only the Claude CLI's own auth-death banner". Measured against 6125 real session
# logs (1454 auth-dead / 4671 healthy), these two match 1454/1454 with 0 false
# positives. Rejected on that evidence — do not add them back:
#   "Invalid authentication" — 0 true / 21 false. It is an unrelated upstream 401
#                              ("API Error: 401 Invalid authentication credentials").
#   "Not logged in"          — 1 false, and redundant: "Please run /login" covers
#                              the identical 1061 logs with none.
# (fiddler's REAUTH_MARKERS can afford the loose variants — it scrapes a tmux pane,
#  not a session log that agents print arbitrary API errors into.)
#
# The keywords alone are NOT sufficient (#3146391b, 2026-07-30). They are a
# substring scan, and the fleet WRITES ABOUT auth incidents in its own session
# logs — a finished report that quotes `Please run /login` inside a sentence
# matched exactly like a session that died on it. Measured on the live corpus
# (~/logs/*.log, 6656 logs): 5 healthy reports matched, every one of them a
# completed Orbit session report about a *past* auth incident. A false positive
# here sets PAUSE_ALL and halts the whole fleet, so the substring scan is now
# only a cheap pre-filter; the decision below is structural.
#
# The discriminator is the LINE SHAPE the CLI itself emits. A real auth-death is
# a process event: the CLI writes one line and exits in ~0.1s, and the banner is
# the ENTIRE line (33 / 72 chars). The 5 quoting lines run 235-535 chars with the
# keyword buried mid-sentence, never at line start. So anchor on the emitted line
# (^...$), not on a substring floating anywhere in the file.
#
# Same class as canon-phantom-blocking-marker-in-driver-comments: a gate that
# greps for a token its own fleet prints is a latch, not a gate.
#
# NOTE the colon in "authenticate:" is load-bearing — the benign upstream 401
# ("Failed to authenticate. API Error: 401 ...") is ALSO a sole-line log, so
# "one line" alone cannot reject it. It fails on `authenticate.` vs
# `authenticate:`. Do not relax that to \W.
#
# --- auth-gate constants: test_dispatcher_auth_gate.py execs this whole block ---
AUTH_FAIL_KEYWORDS = (
    "Please run /login",
    "OAuth session expired",
)
AUTH_FAIL_LINE_RE = re.compile(
    r"^(?:"
    r"not logged in\b.*?\bplease run /login\b"
    r"|failed to authenticate:\s*oauth session expired\b"
    r").*$",
    re.IGNORECASE,
)
# Defence in depth: a dying session prints nothing else. A report is never this
# short, so even a report that quoted a banner on a line of its own is rejected.
AUTH_FAIL_MAX_LINES = 5
_AUTH_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# --- end auth-gate constants ---
PAUSE_DIR = Path.home() / ".cache" / "mesh-dispatcher"
PAUSE_GLOBAL_FILE = PAUSE_DIR / "PAUSE_ALL"

# Defensive allowlist (the operator rule 2026-05-26): these agents are managed
# OUTSIDE mesh-dispatcher (own LaunchAgents, separate runtime, isolated
# mempalace) and MUST NEVER be touched by /stop, /pause, PAUSE_ALL,
# rate-limit auto-pause, or any future /restart-all-agents command.
#
# - Aux1 (vc.entire.aux1, vc.entire.aux1.garmin-sync,
#   vc.entire.aux1.daily-summary) — the operators family health coach,
#   own Telegram bot, separate mempalace
# - Aux2 / aux2_bot (vc.entire.aux2) — the operators private Telegram
#   project, own mempalace, distinct lifecycle
#
# These names are not present in mesh-agents.json — but this list is
# the authoritative source for "do not touch" checks elsewhere.
EXEMPT_AGENTS = {"Aux1", "Aux2", "Aux2", "aux2_bot"}
TG_NAG_CHAT_ID = os.environ.get("TG_NAG_CHAT_ID", "000000000")  # the operator/Orbit default
TG_NAG_OUTBOX = os.environ.get("TG_NAG_OUTBOX",
    "/Users/fleet/ClaudeCowork/bob/telegram-outbox.jsonl")
_STALE_LOCK = threading.Lock()
_STALE_LAST: dict = {}                 # task_id -> monotonic ts of last re-dispatch
_STALE_COUNTS: dict = {}               # task_id -> int — stale-redispatch fire count
_STALE_NAGGED: set = set()             # task_id — nag comment already posted
_TG_NAGGED: set = set()               # task_id — TG nag already sent (1x per task)
_TRIAGED_AUTO: set = set()             # task_id — auto-moved to triage at count==3
_LATCH_TS: dict = {}                   # task_id -> ISO-8601 UTC wall-clock of when the latch was set
_LATCH_RELEASES: dict = {}             # task_id -> int — how often the latch was released again
_BUDGET_TOKENS: dict = {}              # token -> (task_id, prev _STALE_LAST) — in-flight ladder attempts
_BUDGET_SEQ: int = 0                   # monotonic token serial (guarded by _STALE_LOCK)
# --- progress predicate (#19d9a1d1) ---------------------------------------
# `_STALE_LAST` is `time.monotonic()`, which is meaningless across a restart and
# cannot be compared to a comment's `created_at`. The progress check needs the
# same event in WALL clock, so it gets its own stamp written at the same choke
# point (`_respawn_budget`) — that way every one of the six re-entry paths that
# can spawn a card is recorded, not just the stale loop.
_LAST_SPAWN_WALL: dict = {}            # task_id -> epoch seconds of the last ladder-consumed spawn
# token -> (prev_wall, wrote_mono, wrote_wall). Both halves matter: `prev_*` is
# what to put back, `wrote_*` is what this token itself stamped, and a refund
# may only restore while the live value is STILL what this token wrote. See
# `_respawn_budget_refund`.
_BUDGET_STAMPS: dict = {}
_PROGRESS_REPRIEVES: dict = {}         # task_id -> int — how often progress spared this card from a park
_PARK_NOTIFY_WOKEN: set = set()        # task_id — park-notice self-wake already granted
_COALESCE_COMPLETED: dict = {}         # B3.3: (agent, task_id) -> monotonic ts of last session reap
# Status categories that must NEVER trigger an agent respawn. Tasks awaiting
# human review or in terminal states are filtered out at fetch time AND at
# the per-task decision point so a status change between fetch and dispatch
# doesn't slip through.
_SKIP_CATEGORIES = frozenset({"review", "done", "cancelled", "backlog"})
_REPO_UNSAFE_LOCK = threading.Lock()
_REPO_UNSAFE: dict = {}                # task_id -> monotonic ts of last repo-unsafe ABORT
_REPO_UNSAFE_COUNT: dict = {}          # task_id -> consecutive unrecoverable abort count (task bfa8e55a)
_CRASH_RETRY: dict = {}                # task_id -> monotonic ts of last empty-log crash
_CRASH_COUNT: dict = {}                # task_id -> int — empty-log crash count (for backoff)
_CRASH_HISTORY: list = []              # [(ts, agent, task_id, reason)] — rolling window for cluster detect
_RATE_LIMIT_PAUSED: bool = False       # set True once auto-pause triggered (1× per session)
_AUTH_FAIL_PAUSED: bool = False        # set True once auth-expiry auto-pause triggered (1× per session)
_REAP_AGENTS_TO_PULL: set = set()      # agents whose slots freed (process in reaper after lock)
_AGENTS_BY_NAME: dict = {}             # name -> agent_cfg, populated in main()
_API_URL: str = ""                     # populated in main()
                                       # cleared on successful spawn

# --- review_arbiter (shared cross-engine review state) ---------------------
# Lives in the bob scripts dir; this dispatcher is a real file in ~/bin (not a
# symlink) so the module isn't co-located → explicit path. Guarded: a missing
# module must NEVER break the dispatcher. Audit 2026-06-15, epic #b9df70ce —
# P2 #8 (cross-engine TG-nag dedup) uses it in _post_tg_nag below.
try:
    sys.path.insert(0, os.path.expanduser("~/ClaudeCowork/bob/scripts"))
    import review_arbiter
except Exception:
    review_arbiter = None

# --- P2 #7: persist stale-circuit counters across restart ------------------
# _STALE_COUNTS / _TRIAGED_AUTO / _STALE_NAGGED / _TG_NAGGED live only in memory,
# so a dispatcher restart resets every task's stale count to 0 → a long-stuck
# task re-accumulates respawns from scratch and re-nags on count==2/3. Back them
# with a small JSON file: loaded once at boot, saved once per stale-scan pass.
_COUNTERS_FILE = Path(os.path.expanduser("~/.fiddler/state/dispatcher_counters.json"))


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _latch_add(tid: str) -> None:
    """Set the count==3 latch AND stamp when it was set. Caller holds _STALE_LOCK.

    Every latch must carry a wall-clock stamp or LATCH_TTL_SEC cannot expire it —
    an unstamped latch is exactly the immortal kind this fix exists to remove.
    """
    _TRIAGED_AUTO.add(tid)
    _LATCH_TS.setdefault(tid, _utcnow_iso())


def _latch_age_sec(tid: str, now: datetime) -> float | None:
    ts = _parse_iso_utc(_LATCH_TS.get(tid))
    return None if ts is None else (now - ts).total_seconds()


def _prune_expired_latches() -> int:
    """Absolute backstop: drop any latch older than LATCH_TTL_SEC.

    Independent of `_STALE_LAST` (memory-only, monotonic) so it keeps working
    across restarts — the property whose absence made the latch immortal.
    """
    # naive-UTC: the whole module's clock convention (`_parse_iso_utc` strips
    # tzinfo). Mixing aware and naive here raises at runtime, not at import.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with _STALE_LOCK:
        expired = [t for t in list(_TRIAGED_AUTO)
                   if (_latch_age_sec(t, now) or 0) > LATCH_TTL_SEC]
        for t in expired:
            _TRIAGED_AUTO.discard(t)
            _LATCH_TS.pop(t, None)
            _LATCH_RELEASES.pop(t, None)
    if expired:
        log("stale-redispatch",
            f"latch TTL: dropped {len(expired)} latch(es) older than "
            f"{LATCH_TTL_SEC // 86400}d: {', '.join(t[:8] for t in expired[:10])}")
    return len(expired)


def _respawn_budget(agent: str, task_id: str, path: str,
                    enforce: bool = True) -> tuple:
    """Consult + consume ONE attempt from a card's shared respawn ladder.

    Returns `(allowed, count, token)`. `token` is None whenever nothing was
    consumed; pass it to `dispatch_claude(budget_token=...)` so an attempt that
    never reaches `subprocess.Popen()` is handed back (see the wrapper).

    Who calls this, and how (#3788c8f0):

    | call site                      | line  | mode          | rationale                                    |
    |--------------------------------|-------|---------------|----------------------------------------------|
    | `_pull_next_task_for_agent`    | ~3860 | enforce       | re-entry; was 86 of 226 surplus              |
    | `task.mentioned` wake          | ~4950 | enforce       | re-entry; with task.commented, 63 of surplus |
    | `task.commented` @-wake        | ~4990 | enforce       | re-entry (same ladder as task.mentioned)     |
    | SSE 410-recovery               | ~4795 | enforce       | re-entry: re-dispatches the WHOLE open backlog|
    | `stale_redispatcher_loop`      | ~4610 | enforce=False | owns the escalation ACTION — see below       |
    | SSE `task.assigned`            | ~4914 | consult-only  | first entry, not re-entry — see below        |

    `enforce=False` for the stale loop is deliberate, not an exemption. That
    loop is the only path that can actually RUN the ladder's consequences (nag
    comment, human-gate check, passive-wait park, auto-triage, TG ping), and it
    already has its own richer gate — the `tid in _TRIAGED_AUTO` circuit breaker
    — between the increment and the dispatch. If it refused here it would stop
    counting and the card would never escalate at all. So it always consumes;
    the other paths consume AND are refused at the ceiling, then wait for this
    loop (≤5 min) to escalate.

    SSE `task.assigned` consults without consuming. 144 first-spawns against 7
    surplus says the per-assignment model is healthy, and charging a genuine new
    assignment to the ladder would park cards for being worked normally. It
    still CONSULTS so that a park-emitted `task.assigned` — `move_task`
    re-stamps the assignee, which is how parking a card used to re-arm its own
    spawn (#ba50c1a2) — cannot walk past a latch the ladder has already set.

    Consuming stamps `_STALE_LAST` as well as `_STALE_COUNTS`. Both matter:
    the stamp is what `_pull_next_task_for_agent` reads for its 30-min cooldown
    (previously always absent, so the cooldown was dead), and it is the key the
    opportunistic prune in the stale loop iterates — a count written without a
    stamp could never be pruned and would be the immortal-latch defect
    (#aca99f88) rebuilt on the counter instead of the latch.
    """
    global _BUDGET_SEQ
    # `log()` writes to disk, so it is kept strictly OUTSIDE the lock: three
    # threads (reaper, stale loop, per-agent SSE listeners) contend for
    # _STALE_LOCK, and it is not reentrant.
    refuse = None
    token = None
    with _STALE_LOCK:
        count = _STALE_COUNTS.get(task_id, 0)
        if enforce:
            if task_id in _TRIAGED_AUTO:
                refuse = "auto-triaged (parked for human)"
            elif count >= RESPAWN_LADDER_MAX:
                refuse = f"ladder exhausted {count}/{RESPAWN_LADDER_MAX}"
        if not refuse:
            count += 1
            _STALE_COUNTS[task_id] = count
            _BUDGET_SEQ += 1
            token = f"{path}:{task_id}:{_BUDGET_SEQ}"
            # Remember what we overwrote so a refund restores it exactly,
            # rather than clearing a stamp a concurrent path is relying on.
            _BUDGET_TOKENS[token] = (task_id, _STALE_LAST.get(task_id))
            _prev_wall = _LAST_SPAWN_WALL.get(task_id)
            _wrote_mono = time.monotonic()
            # Wall-clock twin of the line below (#19d9a1d1). Written here rather
            # than at the call sites so all six re-entry paths stamp it.
            _wrote_wall = time.time()
            _STALE_LAST[task_id] = _wrote_mono
            _LAST_SPAWN_WALL[task_id] = _wrote_wall
            _BUDGET_STAMPS[token] = (_prev_wall, _wrote_mono, _wrote_wall)
    if refuse:
        log(agent, f"respawn-budget: REFUSE {path} {task_id[:8]} — {refuse}")
        return (False, count, None)
    log(agent, f"respawn-budget: {path} {task_id[:8]} "
               f"attempt {count}/{RESPAWN_LADDER_MAX}")
    return (True, count, token)


def _respawn_budget_consult(agent: str, task_id: str, path: str) -> bool:
    """Ceiling check with no consumption. See `_respawn_budget`'s table."""
    with _STALE_LOCK:
        latched = task_id in _TRIAGED_AUTO
        count = _STALE_COUNTS.get(task_id, 0)
    if latched or count >= RESPAWN_LADDER_MAX:
        log(agent, f"respawn-budget: REFUSE {path} {task_id[:8]} — "
                   + ("auto-triaged (parked for human)" if latched
                      else f"ladder exhausted {count}/{RESPAWN_LADDER_MAX}"))
        return False
    return True


def _respawn_budget_refund(token: str, reason: str) -> bool:
    """Hand back exactly the attempt `token` names — never "one attempt".

    The old inline version decremented `_STALE_COUNTS[tid]` unconditionally in
    the cap-defer branches. With a single writer that was safe. With six paths
    on three threads it is not: the reaper deferring attempt B would refund the
    increment of the stale loop's attempt A, which is mid-flight and about to
    really spawn — the card then respawns forever without reaching the ceiling
    (the `#8b5f818a` equality-test defect, arrived at from the other side).
    Popping the token makes a refund idempotent and attributable.

    The B3·dq intent is preserved exactly: a spawn the dispatcher REFUSED (cap,
    pause, unknown runtime) costs $0 and must not count toward give-up, so it is
    refunded; a spawn that ran and produced nothing is not.
    """
    with _STALE_LOCK:
        tid = _respawn_budget_refund_locked(token)
    if tid is None:
        return False
    log("respawn-budget", f"refund {token.split(':')[0]} {tid[:8]} — {reason}")
    return True


def _respawn_budget_refund_locked(token: str) -> "str | None":
    """Body of `_respawn_budget_refund`. CALLER MUST HOLD `_STALE_LOCK`.

    Split out (#ee63bb07) so the progress-reprieve tail can run check → refund →
    reset → restamp under ONE hold instead of four. `_STALE_LOCK` is not
    reentrant, so a caller that already holds it cannot call the public wrapper.
    This is the single source of the refund logic — the wrapper is a wrapper, not
    a second copy; two copies of a compare-and-swap this subtle would drift, and
    the drift is exactly the four-in-a-row defect family this file already has.

    Returns the task id whose attempt was handed back, or None if the token was
    unknown/empty (nothing to log). Logging is the wrapper's job because `log()`
    writes to disk and must stay OUTSIDE the lock.
    """
    if not token:
        return None
    entry = _BUDGET_TOKENS.pop(token, None)
    if entry is None:
        return None
    tid, prev_last = entry
    cur = _STALE_COUNTS.get(tid, 0)
    if cur > 0:
        _STALE_COUNTS[tid] = cur - 1
    # A refunded attempt never reached Popen, so no spawn happened and both
    # stamps must go back — otherwise the progress window starts at a spawn
    # that does not exist and every comment older than it reads as "nothing
    # since the last spawn" (#19d9a1d1).
    #
    # But the restore is COMPARE-AND-SWAP, not unconditional. The token
    # already carried the value to restore; what it was missing is the value
    # it WROTE. Without that check a late refund silently rolls back a real,
    # newer spawn recorded by one of the other five paths on another thread:
    # this file's own docstring lists six budget consumers across three
    # threads, and the progress-reprieve branch holds its token across an
    # HTTP round-trip (`_executor_progress_since`), which is a wide window
    # to be clobbering a sibling's stamp in. Same defect family as the
    # unconditional `_STALE_COUNTS` decrement this function was written to
    # replace — arrived at one dict over. Caught by the verifier 2026-08-04.
    #
    # If the live value is no longer what this token wrote, somebody else
    # owns the stamp now and the correct action is to leave it alone.
    stamps = _BUDGET_STAMPS.pop(token, None)
    prev_wall, wrote_mono, wrote_wall = stamps or (None, None, None)
    if wrote_mono is not None and _STALE_LAST.get(tid) == wrote_mono:
        if prev_last is None:
            _STALE_LAST.pop(tid, None)
        else:
            _STALE_LAST[tid] = prev_last
    if wrote_wall is not None and _LAST_SPAWN_WALL.get(tid) == wrote_wall:
        if prev_wall is None:
            _LAST_SPAWN_WALL.pop(tid, None)
        else:
            _LAST_SPAWN_WALL[tid] = prev_wall
    return tid


def _respawn_budget_settle(token: str) -> None:
    """The attempt reached `subprocess.Popen()`. Its increment stands."""
    if not token:
        return
    with _STALE_LOCK:
        _BUDGET_TOKENS.pop(token, None)
        _BUDGET_STAMPS.pop(token, None)


def _budget_token_owns_stamps(token: str) -> bool:
    """Is `_STALE_LAST[tid]` still the value THIS token wrote? (#19d9a1d1)

    The compare half of `_respawn_budget_refund`'s compare-and-swap, exposed so
    a caller can ask the question BEFORE the refund pops the record. Callers
    that follow a refund with a reset need it: `_respawn_budget_reset` clears
    `_STALE_LAST` unconditionally by design (a genuine status change should
    forget the cooldown), so a caller that has just been told "a sibling owns
    this stamp now" would otherwise throw that answer away one line later and
    clobber the sibling anyway — the CAS closed at the refund and reopened at
    the reset, in the same branch.

    ⚠️ The answer is only true FOR AS LONG AS THE LOCK IS HELD. Asking through
    this wrapper and then acting on the answer after it has been released is a
    TOCTOU — which is what the reprieve tail used to do (#ee63bb07). Callers that
    act on the answer must hold the lock across both, i.e. call the `_locked`
    core. This wrapper is for callers that only want to observe (tests, probes).
    """
    with _STALE_LOCK:
        return _budget_token_owns_stamps_locked(token)


def _budget_token_owns_stamps_locked(token: str) -> bool:
    """Body of `_budget_token_owns_stamps`. CALLER MUST HOLD `_STALE_LOCK`."""
    if not token:
        return False
    entry = _BUDGET_TOKENS.get(token)
    stamps = _BUDGET_STAMPS.get(token)
    if not entry or not stamps:
        return False
    tid = entry[0]
    wrote_mono = stamps[1]
    return wrote_mono is not None and _STALE_LAST.get(tid) == wrote_mono


def _respawn_budget_reset(task_id: str, reason: str,
                          preserve_stale_last: bool = False) -> bool:
    """Clear a card's ladder on a genuine status change.

    Required by the ladder's own shape: `RESPAWN_LADDER_MAX` is a threshold on a
    counter that otherwise only ever grows, so without a reset a card that was
    legitimately closed and reopened (or moved between statuses by a human)
    would arrive with its ladder already exhausted and be refused every re-entry
    silently. Any in-flight tokens for the card are dropped too, so a refund
    landing after the reset cannot push the fresh counter negative or resurrect
    a stale `_STALE_LAST`.

    `preserve_stale_last` exists for exactly one caller shape: a path that has
    already established (via `_budget_token_owns_stamps`) that the cooldown
    stamp now belongs to a CONCURRENT attempt, not to it. Dropping the cooldown
    is right when the card genuinely changed status; it is wrong when the only
    thing that happened is that this thread decided not to spawn while another
    thread did. Default stays False so the status-change caller is unaffected.

    ⚠️ Deciding `preserve_stale_last` in one lock acquisition and applying it in
    another is a TOCTOU (#ee63bb07). A caller that does both must hold the lock
    across both — see `_respawn_budget_reprieve`.
    """
    with _STALE_LOCK:
        had = _respawn_budget_reset_locked(task_id, preserve_stale_last)
    if had:
        log("respawn-budget", f"reset {task_id[:8]} — {reason}")
    return had


def _respawn_budget_reset_locked(task_id: str,
                                 preserve_stale_last: bool = False) -> bool:
    """Body of `_respawn_budget_reset`. CALLER MUST HOLD `_STALE_LOCK`.

    Split out (#ee63bb07) for the same reason as the refund core: the reprieve
    tail must decide `preserve_stale_last` and apply it without ever dropping the
    lock in between. Single source of the reset logic; the public function above
    is a wrapper that only adds the (disk-writing, must-be-unlocked) log line.
    """
    had = (task_id in _STALE_COUNTS or task_id in _STALE_LAST)
    _STALE_COUNTS.pop(task_id, None)
    if not preserve_stale_last:
        _STALE_LAST.pop(task_id, None)
    _STALE_NAGGED.discard(task_id)
    _TG_NAGGED.discard(task_id)
    for tok in [t for t, (tid, _) in _BUDGET_TOKENS.items() if tid == task_id]:
        _BUDGET_TOKENS.pop(tok, None)
        _BUDGET_STAMPS.pop(tok, None)
    # `_LAST_SPAWN_WALL` and `_PROGRESS_REPRIEVES` deliberately survive a
    # reset. The first is a record of when we last spawned — still true after
    # the ladder is cleared. The second is the BOUND on the progress reprieve,
    # and the reprieve's own action is a reset: clearing it here would make
    # the reprieve self-renewing and the give-up rung unreachable (#19d9a1d1).
    return had


def _respawn_budget_reprieve(token: str, task_id: str, reprieves_next: int,
                             mono: float, why: str) -> bool:
    """The progress-reprieve tail, as ONE critical section (#ee63bb07).

    `bump reprieve → who owns the stamp? → refund → reset → restamp` is a single
    decision, and it used to be four separate acquisitions of a non-reentrant
    `_STALE_LOCK`. `_budget_token_owns_stamps` answers about a value another
    thread may change the instant the lock is released; the reset then acts on
    the stale answer and pops `_STALE_LAST` anyway, and the restamp writes THIS
    scan's older `mono` over a sibling's newer, real one. Net effect: the
    sibling's cooldown is shortened and the card becomes respawn-eligible early —
    exactly the surplus respawn #3788c8f0/#19d9a1d1 exist to remove.

    #19d9a1d1 shrank this window from "spans an HTTP round-trip" to "a few
    consecutive Python statements", which is why the round-trip-scale race
    simulation in `concurrency_check` cannot reproduce it. Small is not zero: the
    GIL is released every `sys.setswitchinterval()` (5 ms by default) regardless
    of I/O, and this loop runs against two other lock-taking threads.

    Returns whether the cooldown stamp was still ours (i.e. whether we restamped).

    NOTE the network call is deliberately NOT in here. `_executor_progress_since`
    stays at the call site, OUTSIDE the lock: it is a 15 s-timeout HTTP round
    trip, and serialising the reaper and the SSE listeners behind it would be
    curing a millisecond race with a fifteen-second stall.
    """
    with _STALE_LOCK:
        _PROGRESS_REPRIEVES[task_id] = reprieves_next
        owns = _budget_token_owns_stamps_locked(token)
        refunded_tid = _respawn_budget_refund_locked(token)
        had = _respawn_budget_reset_locked(task_id,
                                           preserve_stale_last=not owns)
        if owns:
            # The ladder reset drops the cooldown stamp, so put ours back —
            # without it the next 5-minute scan re-runs this whole check
            # immediately and a live card is re-read every scan forever. Only
            # when the stamp is still OURS: if a concurrent path spawned this
            # card while we were on the network, its stamp is both newer and
            # true, and it already provides the cooldown.
            _STALE_LAST[task_id] = mono
    # `log()` writes to disk — strictly outside the lock, like everywhere else
    # in this module.
    if refunded_tid:
        log("respawn-budget",
            f"refund {token.split(':')[0]} {refunded_tid[:8]} — "
            "progress reprieve — no spawn")
    if had:
        log("respawn-budget", f"reset {task_id[:8]} — progress observed — {why}")
    return owns


def _release_latch_if_feedable(tid: str, cat: str, name: str) -> bool:
    """Clear the count==3 latch on a card that is BACK in a feedable status.

    The latch's whole job is "stop respawning this card while it sits parked".
    A parked card is already unreachable by status alone — `triage`, `backlog`,
    `review`, `done` are none of them fetched. So the latch is redundant with
    the status for every card whose park MOVE landed, and actively harmful for
    every card whose park did not land or that someone later moved back: those
    sit in `todo`, get fetched every scan, and are dropped by the circuit
    breaker forever. Measured 2026-08-02: 79 latches, of which 76 sat on cards
    in non-feedable statuses (inert) and exactly 3 sat on open `todo` cards
    that had been silently unfeedable for 4-6 days.

    Releasing resets the stale count too. Without that the ladder cannot re-arm
    (`nag_count == 3` is an equality test), so the card would respawn forever
    with no escalation — the opposite defect, #8b5f818a.

    Returns True if a latch was released.
    """
    if cat not in ("todo", "in_progress"):
        return False
    with _STALE_LOCK:
        if tid not in _TRIAGED_AUTO:
            return False
        released = _LATCH_RELEASES.get(tid, 0)
        if released >= LATCH_MAX_RELEASES:
            capped = True
        else:
            capped = False
            _TRIAGED_AUTO.discard(tid)
            _LATCH_TS.pop(tid, None)
            _LATCH_RELEASES[tid] = released + 1
            _STALE_COUNTS.pop(tid, None)
            _STALE_NAGGED.discard(tid)
            _TG_NAGGED.discard(tid)
            # A human / the triage-drain healer put this card back in a feedable
            # lane: that is a NEW episode, so the progress-reprieve budget starts
            # over. This is the only place it is cleared — see
            # `_respawn_budget_reset` for why the reprieve must not clear it
            # itself (#19d9a1d1).
            _PROGRESS_REPRIEVES.pop(tid, None)
    if capped:
        log("stale-redispatch",
            f"latch KEPT {name}/{tid[:8]} — already released "
            f"{released}× (max {LATCH_MAX_RELEASES}); its park keeps failing, "
            "not re-arming again")
        return False
    log("stale-redispatch",
        f"latch released {name}/{tid[:8]} — card is back in '{cat}', "
        f"re-arming dispatch (release #{released + 1})")
    return True


def _load_counters() -> None:
    try:
        data = json.loads(_COUNTERS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return
    try:
        with _STALE_LOCK:
            _STALE_COUNTS.update({k: int(v)
                                  for k, v in (data.get("stale_counts") or {}).items()})
            _TRIAGED_AUTO.update(data.get("triaged_auto") or [])
            _STALE_NAGGED.update(data.get("stale_nagged") or [])
            _TG_NAGGED.update(data.get("tg_nagged") or [])
            _LATCH_TS.update(data.get("triaged_auto_ts") or {})
            _LATCH_RELEASES.update({k: int(v) for k, v in
                                    (data.get("latch_releases") or {}).items()})
            # #19d9a1d1: both must survive a restart. Without the spawn stamps a
            # dispatcher reload mid-ladder leaves every in-flight card with "no
            # recorded last spawn", and the progress check then has no window to
            # ask about — it fails closed and parks, which is the bug it exists
            # to fix. Without the reprieve counts the bound resets on every
            # reload and a chatty card could dodge the parking lane forever.
            _LAST_SPAWN_WALL.update({k: float(v) for k, v in
                                     (data.get("last_spawn_wall") or {}).items()})
            _PROGRESS_REPRIEVES.update({k: int(v) for k, v in
                                        (data.get("progress_reprieves") or {}).items()})
            # Migration: latches written before this field existed carry no stamp.
            # They get "now", not epoch — an un-aged latch must not be flushed
            # wholesale on first boot. The status-driven release below is what
            # frees the live ones; the TTL only guarantees none lives forever.
            _migrated = 0
            for t in _TRIAGED_AUTO:
                if t not in _LATCH_TS:
                    _LATCH_TS[t] = _utcnow_iso()
                    _migrated += 1
        log("main", f"counters restored: {len(_STALE_COUNTS)} stale-count, "
                    f"{len(_TRIAGED_AUTO)} triaged"
                    + (f" ({_migrated} latch stamps migrated)" if _migrated else ""))
        _prune_expired_latches()
    except Exception as e:
        log("main", f"counters restore failed: {e}")


def _save_counters() -> None:
    try:
        with _STALE_LOCK:
            data = {
                "stale_counts": dict(_STALE_COUNTS),
                # Kept a plain sorted list: this is the field every external
                # probe reads ("is card X latched?"). The stamps ride alongside.
                "triaged_auto": sorted(_TRIAGED_AUTO),
                "triaged_auto_ts": {k: v for k, v in _LATCH_TS.items()
                                    if k in _TRIAGED_AUTO},
                "latch_releases": {k: v for k, v in _LATCH_RELEASES.items()
                                   if v},
                "stale_nagged": sorted(_STALE_NAGGED),
                "tg_nagged": sorted(_TG_NAGGED),
                "last_spawn_wall": dict(_LAST_SPAWN_WALL),
                "progress_reprieves": {k: v for k, v in
                                       _PROGRESS_REPRIEVES.items() if v},
            }
        _COUNTERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _COUNTERS_FILE.with_name(_COUNTERS_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, _COUNTERS_FILE)
    except Exception as e:
        log("stale-redispatch", f"counters save failed: {e}")

# --- Review-sweep (review-backlog visibility) ------------------------------
# `review` is excluded from respawn (correct — it awaits a human). But nothing
# surfaced it either: done-work rotted unclosed (no review->done closer) and
# the operator-gated asks died in Mesh comments nobody read. This sweep periodically
# scans review tasks across all agents, buckets them (awaiting-human-decision
# vs awaiting-verify+close) and sends the operator ONE consolidated, dedup'd TG digest.
# Cost-neutral by design: pure API reads + a TG message — NO agent respawns,
# NO auto-close. (Orbit 2026-05-25, after Atlas flagged 30+ tasks stuck in review.)
REVIEW_SWEEP_ENABLED = os.environ.get("REVIEW_SWEEP_ENABLED", "1") != "0"
REVIEW_SWEEP_INTERVAL_SEC = int(os.environ.get("REVIEW_SWEEP_INTERVAL_SEC", str(6 * 3600)))
REVIEW_STALE_SEC = int(os.environ.get("REVIEW_STALE_SEC", str(24 * 3600)))
REVIEW_DIGEST_REMIND_SEC = int(os.environ.get("REVIEW_DIGEST_REMIND_SEC", str(24 * 3600)))
REVIEW_SWEEP_MAX_COMMENT_FETCHES = int(os.environ.get("REVIEW_SWEEP_MAX_COMMENT_FETCHES", "80"))
REVIEW_DIGEST_MAX_PER_BUCKET = int(os.environ.get("REVIEW_DIGEST_MAX_PER_BUCKET", "10"))
REVIEW_SWEEP_AUTOCLOSE = os.environ.get("REVIEW_SWEEP_AUTOCLOSE", "1") != "0"
DISPATCHER_MUTATIONS = os.environ.get("DISPATCHER_MUTATIONS", "live").lower()  # 'report'=dry-run, no POST /move
_REVIEW_DIGEST_SIG = ""                # signature of last digest sent (dedup)
_REVIEW_DIGEST_LAST_SENT = 0.0         # monotonic ts of last digest send
# G1-s3: graph-recall memory context injection for captain/parent tasks on spawn.
# SHIP-DARK: default OFF (DISPATCHER_RECALL_GRAPH=1 to enable) until the
# recall_graph endpoint is smoke-verified live on prod. When OFF the spawn path
# is byte-identical to before. Async-safe: hard timeout + flat-recall fallback +
# skip-on-failure so a slow/missing endpoint never blocks or delays a spawn.
RECALL_GRAPH_ENABLED = os.environ.get("DISPATCHER_RECALL_GRAPH", "0") == "1"
RECALL_GRAPH_HOPS = int(os.environ.get("DISPATCHER_RECALL_GRAPH_HOPS", "2"))
RECALL_GRAPH_TIMEOUT = float(os.environ.get("DISPATCHER_RECALL_GRAPH_TIMEOUT", "3"))
RECALL_GRAPH_MAX_ITEMS = int(os.environ.get("DISPATCHER_RECALL_GRAPH_MAX_ITEMS", "5"))
# Memory-eval A3-followup (task e8344446): DETERMINISTIC mempalace READ on spawn.
# Root cause: prompt-instructed wake-up read (A3) is skipped by the LLM →
# mempalace transcript R:W stuck at 0.054 (target 0.3). recall_graph (above) is
# captain-only + EPISODIC (evc-mesh), not mempalace, and never moves the mempalace
# layer. This block makes the dispatcher itself read the agent's OWN mempalace wing
# (latest sessions handoff + task-topic hits) read-only from chroma.sqlite3, inject
# it as a 🏛️ block, AND record a per-agent read to the sidecar that collect-memory-
# ops.py credits to the mempalace layer (closing its documented "headless reads
# outside transcripts" coverage gap — these are REAL reads, just not model tool_use).
# ON by default (local read, no network, proven); opt-out label `no-mempalace-prefetch`.
MEMPALACE_PREFETCH_ENABLED = os.environ.get("DISPATCHER_MEMPALACE_PREFETCH", "0") == "1"
MEMPALACE_CHROMA_DB = Path(os.environ.get(
    "DISPATCHER_MEMPALACE_DB",
    str(Path.home() / ".mempalace" / "your-org" / "chroma.sqlite3")))
MEMPALACE_PREFETCH_TIMEOUT = float(os.environ.get("DISPATCHER_MEMPALACE_TIMEOUT", "3"))
MEMPALACE_PREFETCH_SESSIONS = int(os.environ.get("DISPATCHER_MEMPALACE_SESSIONS", "2"))
MEMPALACE_PREFETCH_TOPIC = int(os.environ.get("DISPATCHER_MEMPALACE_TOPIC", "3"))
# Sidecar of dispatcher-side memory reads — consumed by collect-memory-ops.py (D1).
MEMORY_READS_SIDECAR = Path(os.environ.get(
    "DISPATCHER_MEMORY_READS_SIDECAR",
    str(Path.home() / ".openclaw" / "metrics" / "dispatcher-memory-reads.jsonl")))
# Markers in a review task's recent comments that mean "waiting on a human GO".
# Only a DELIBERATE §0b blocking marker counts as "awaiting the operator". Loose
# mentions ("@operator async", "the operator decision НЕ нужен", "FYI @operator") are NOT
# asks — they were the #1 source of false 🔴 entries (the operator 2026-06-03).
_PAVEL_ASK_MARKERS = (
    "blocking @operator", "❓ blocking", "блокирующий @operator", "blocking pavel",
)
# If any of these appear in the SAME comment, the ask is negated/withdrawn.
_PAVEL_ASK_NEGATORS = (
    "не нужен", "ничего не нужно", "ничего не надо",
    "не требует", "не срочно", "не дёргаю", "не дергаю",
    "не блокир", "unblock", "blocking снят", "decision не нужен",
    "решение не нужно", "от тебя ничего",
    "лично от pavel ничего", "fyi @operator", "fyi:",
)


def _comment_items(resp) -> list:
    """Canonical unwrap for GET /api/v1/tasks/{id}/comments.

    The endpoint paginates as {"items": [...], "total_count": N, ...} — there is NO
    "comments" key. Reading .get("comments") silently yielded [] on EVERY call, which
    made every comment-driven gate here dead code (identical bug fixed in
    pr-task-driver 2026-07-12, bob@a4c3ff5 / bob@6ae23f1; fiddler, mesh-intake-sweep,
    review-verify-driver and triage-drain already unwrap items-first).

    Key PRESENCE, not truthiness: a present-but-empty "items" must NOT fall through to
    a legacy key (that fallthrough was bob@6ae23f1).
    """
    if isinstance(resp, list):
        return resp
    if not isinstance(resp, dict):
        return []
    for k in ("items", "comments", "data"):
        if k in resp:
            v = resp[k]
            return v if isinstance(v, list) else []
    return []


def _fetch_task_full(agent_key: str, task_id: str, api_url: str):
    """GET one task dict. None on any error (callers fail-open)."""
    try:
        req = Request(f"{api_url}/api/v1/tasks/{task_id}",
                      headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _fetch_task_comments(agent_key: str, task_id: str, api_url: str, limit: int = 50) -> list:
    """GET a task's comments, correctly unwrapped. [] on any error."""
    try:
        req = Request(f"{api_url}/api/v1/tasks/{task_id}/comments?limit={limit}",
                      headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=10) as r:
            return _comment_items(json.loads(r.read()))
    except Exception:
        return []


def _human_gate_blocks_feed(agent_name: str, agent_key: str, task_id: str,
                            api_url: str, mention_context: dict = None) -> bool:
    """Dispatcher half of audit fix #1 — enforce the human_gate.py FREEZE RULE on the
    feed path ("never re-fed", which until now only fiddler honoured).

    fiddler has skipped the operator-gated tasks when feeding since 2026-06-15; its own comment
    calls that "audit fix #1 (fiddler half)" — the dispatcher half was never written. So
    ANY path that lands a gated task in `todo` (a stale re-feed, an intake-sweep promote,
    a re-assign) made the dispatcher spawn the owner immediately, burning a session on a
    task that cannot move without the operator. Task cd53e9aa looped this way 5×.

    NOT gated — mention/comment wakes (mention_context set): the operator answering IS the
    un-freeze signal and must always reach the agent (incident E1 #8594d87d, where an
    approved deploy hung ~a day because a comment never woke the owner).

    Fail-open everywhere: any API error, or a task we can't fetch, DISPATCHES. A network
    blip must never freeze the fleet.
    """
    if not HUMAN_GATE_FEED_ENABLED or mention_context or not task_id:
        return False
    t = _fetch_task_full(agent_key, task_id, api_url)
    if not t:
        return False  # fail-open
    if not _is_human_gated(t):  # cheap path — no comment fetch
        return False
    # Cheap signal says gated → re-check WITH comments, because the operator may have replied
    # AFTER the block, which RELEASES the freeze and must re-feed the owner.
    cs = _fetch_task_comments(agent_key, task_id, api_url)
    if cs and not _is_human_gated(t, comments=cs):
        log(agent_name,
            f"human-gate RELEASED #{task_id[:8]} — the operator replied after the block → feeding")
        return False
    log(agent_name,
        f"human-gate skip feed #{task_id[:8]} — {_gate_reason(t, cs)} — frozen for the operator")
    return True


def _has_human_gate_signal(api_url: str, agent_key: str, t: dict) -> bool:
    """True if the task carries an explicit human-gate signal that warrants @operator.

    Gate signals (checked in order):
    0. Server `human_gate` flag (PR #258) — stamped true by enforceBlockingTriage
       on any ❓Blocking @operator comment. Cheapest check, no API call. Freeze rule:
       human_gate=true always wins (P3 #9, 2026-06-16).
    1. Label 'blocked:pavel' — explicitly set by an agent flagging a the operator decision.
    2. Recent comment with a _PAVEL_ASK_MARKERS phrase not negated by _PAVEL_ASK_NEGATORS.

    Returns False by default → escalation routes to creator, not @operator.
    Fail-open on API errors: no gate detected means don't ping @operator on network issues.
    (B3·dq fix 2026-06-08; human_gate flag added P3 #9 2026-06-16)
    """
    if t.get("human_gate"):
        return True
    labels = {str(l).lower() for l in (t.get("labels") or [])}
    if labels & _HUMAN_GATE_LABELS_DISPATCH:
        return True
    task_id = t.get("id")
    if not task_id:
        return False
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/comments?limit=5"
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        comments = _comment_items(data)
        for c in comments:
            # the operators own reply is not an ask TO the operator, and a fleet driver quoting the
            # marker as instructions is not an ask at all (#84ab54fd — same phantom that
            # hit the review sweep; here it would freeze a task nobody ever un-freezes).
            if (c.get("author_type") or c.get("author_kind") or "").lower() == "user":
                continue
            body_low = (c.get("body") or "").lower()
            if _is_automated_comment(body_low):
                continue
            # Marker AND negators read from ONE code-masked view (#ba5a4f10). Reading the
            # marker from the raw body is #ce053513 in the mirror direction: prose that
            # QUOTES the marker while explaining the gate arms the gate it documents.
            # Authorship detection above stays on the RAW body on purpose — a driver's
            # self-declaring prefix is not a code span, and masking it would let a driver
            # that backticks its own prefix slip past the #84ab54fd filter.
            masked = _gate_masked_low(body_low)
            if any(m in masked for m in _PAVEL_ASK_MARKERS):
                if not any(n in masked for n in _PAVEL_ASK_NEGATORS):
                    return True
    except Exception:
        pass  # fail-open: error → don't @operator by default
    return False


# --- Escalation re-verify gate (task 7472e600, Orbit 2026-06-04) -----------
# A `❓Blocking @operator` marker can go STALE: the blocker self-heals (often
# silently, with NO follow-up comment) but the marker stays the latest "ask",
# so the sweep escalates a PHANTOM blocker to the operator. Incident (TR×Mesh): a
# "backend autodeploy broken, blocking @operator" cleared itself 9 min after the
# comment, yet the operator saw the escalation only 2 days later → 2 days lost on a
# non-existent blocker.
#
# Fix: a marker-based ask does NOT go straight to the operator. On first sighting the
# OWNING AGENT is pinged ONCE to re-verify (re-curl / re-check live). The ask
# is then HELD for ESCALATION_REVERIFY_SEC. Only if it SURVIVES that window —
# still present, not withdrawn, task still in review — does it escalate. A
# self-healed blocker drops out (agent removes the marker / closes the task /
# the task leaves review) and never reaches the operator. Cost: at most ONE agent
# re-check per genuine stale pavel-ask (rare), vs. days of the operators time on
# phantoms. Tasks assigned directly TO the operator (assignee_type==user) bypass the
# gate — he owns them, there is no agent to re-curl and no phantom risk.
ESCALATION_REVERIFY_ENABLED = os.environ.get("ESCALATION_REVERIFY_ENABLED", "1") != "0"
# N hours to wait after the agent re-verify ping before a surviving ask reaches
# the operator. Default 4h: long enough for an awoken agent to re-curl and withdraw a
# self-healed blocker, short enough that a genuine blocker still surfaces same-day.
ESCALATION_REVERIFY_SEC = int(os.environ.get("ESCALATION_REVERIFY_SEC", str(4 * 3600)))
# Persistent gate state: {task_id: {first_seen, reverify_requested_at, escalated_at}}
ESCALATION_REVERIFY_FILE = LIVE_FILE.parent / "escalation-reverify.json"
# "Surface a resolved human-gate (ready-to-close) ONCE, never daily-nag it" — the operator
# 2026-06-26 (2 Willow billing tasks nagged «Ждут твоего решения» 5d though only his
# manual close remained). {task_id: first-surfaced ISO ts}; pruned when the task
# leaves review or is re-blocked. Distinct from the reverify gate state above.
REVIEW_READY_SEEN_FILE = LIVE_FILE.parent / "review-ready-close-seen.json"
# #effd0fbb: {task_id: ISO ts of the last digest that ACTUALLY WENT OUT carrying a
# detail line for it}. Drives the rotation of the out-of-review tail.
#
# Written at SEND time, never at selection time. The distinction is the whole
# correctness of the mechanism: this sweep skips its own message most cycles
# («digest unchanged, within remind window — skip» fired on all three cycles of
# 2026-08-01), and a stamp written at selection time would let a card be "rotated
# through" by a digest the operator never received — the card would then go to the back of
# the queue having been shown to nobody. Same failure class as an alert-on-change
# probe consuming its own state transition in a muted dry run.
GATE_SCOPE_SHOWN_FILE = LIVE_FILE.parent / "gate-scope-shown.json"


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _persist_live():
    """Mirror _LIVE to LIVE_FILE (best-effort). Caller MUST hold _DISPATCH_LOCK."""
    try:
        LIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        snap = [
            {"agent": a, "task_id": t, "pid": v.get("pid"),
             "started_at": v.get("started_at"),
             "reserved": v.get("reserved", False), "log": v.get("log")}
            for (a, t), v in _LIVE.items()
        ]
        tmp = LIVE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, indent=2))
        tmp.replace(LIVE_FILE)
    except Exception as e:
        log("dispatcher", f"persist live registry failed: {e}")


def _load_live_file():
    """Recover the registry after a (re)start: keep only entries whose pid is
    still alive — a child that outlived a launchd reload must still block a
    duplicate spawn. Stale / reserved-only entries are dropped."""
    try:
        if not LIVE_FILE.exists():
            return
        for e in json.loads(LIVE_FILE.read_text()):
            pid = e.get("pid")
            if pid and _pid_alive(pid):
                _LIVE[(e["agent"], e["task_id"])] = {
                    "pid": pid, "proc": None, "mono": time.monotonic(),
                    "started_at": e.get("started_at"), "reserved": False,
                    "log": e.get("log"),
                    # Mark as recovered from lockfile — when this session reaps,
                    # skip crash-detection (its duration measurement is wrong,
                    # log was written by pre-restart process). Added 2026-05-24.
                    "recovered": True}
        if _LIVE:
            log("dispatcher",
                f"recovered {len(_LIVE)} live agent session(s) from lockfile")
        _persist_live()
    except Exception as e:
        log("dispatcher", f"load live registry failed: {e}")


def _live_active_count() -> int:
    """Count claude processes currently alive (counts toward MAX_CONCURRENT_SPAWNS)."""
    n = 0
    with _DISPATCH_LOCK:
        for v in _LIVE.values():
            pid = v.get("pid")
            if pid and _pid_alive(pid):
                n += 1
    return n


def _live_per_agent_count(agent: str) -> int:
    """Count alive claudes for a specific agent (counts toward MAX_PER_AGENT_SPAWNS).

    Fair-queueing: prevents one agent from hogging all global slots while
    other agents starve. Added 2026-05-22 after Atlas cascade locked Kilo out.
    """
    n = 0
    with _DISPATCH_LOCK:
        for (a, _), v in _LIVE.items():
            if a != agent:
                continue
            pid = v.get("pid")
            if pid and _pid_alive(pid):
                n += 1
    return n


def _post_tg_nag(chat_id: str, body: str, parse_mode: str = None) -> None:
    """Append a Telegram outbox entry. Best-effort, never raises.

    parse_mode (e.g. "HTML") lets callers emit compact <a href>#id</a> task
    links — the bridge honors it (same path tg-reply/tg-mesh-linkify use).
    """
    # P2 #8: cross-engine TG-nag dedup. Content-keyed (chat+body) so only an
    # IDENTICAL message sent by another engine within the window is collapsed —
    # distinct nags carry different task ids/counts and pass through. Covers the
    # review-verify-driver too: it nags via this same function (md._post_tg_nag).
    if review_arbiter is not None:
        try:
            if not review_arbiter.nag_once((chat_id, body)):
                log("stale-redispatch", "tg nag deduped (cross-engine, identical body)")
                return
        except Exception:
            pass
    try:
        obj = {"chat_id": chat_id, "text": body}
        if parse_mode:
            obj["parse_mode"] = parse_mode
        line = json.dumps(obj, ensure_ascii=False)
        with open(TG_NAG_OUTBOX, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        log("stale-redispatch", f"tg nag failed: {e}")


def _workspace_to_jsonl_slug(workspace_path: str) -> str:
    """Claude Code stores sessions at ~/.claude/projects/<slug>/<session-id>.jsonl
    where slug = absolute path with '/' replaced by '-'."""
    return workspace_path.replace("/", "-")


def _snapshot_jsonls(workspace_path: str) -> set:
    """Snapshot existing jsonl filenames in workspace's slug dir at this moment.
    Used to identify the freshly-spawned claude session's jsonl by set diff
    against this pre-spawn snapshot. Empty set on errors (degrades to legacy)."""
    slug = _workspace_to_jsonl_slug(workspace_path)
    slug_dir = CLAUDE_PROJECTS_DIR / slug
    if not slug_dir.is_dir():
        return set()
    try:
        return {f.name for f in slug_dir.glob("*.jsonl")}
    except OSError:
        return set()


def _find_session_jsonl(workspace_path: str, spawn_ts: float, pre_set=None):
    """Find the jsonl for a freshly-spawned claude session.

    Primary (pre_set provided): pick jsonl whose name is NOT in pre_set
    (created after spawn) — deterministic, immune to concurrent active
    sessions writing their own jsonls.

    Fallback (pre_set None — old call site / lockfile-recovered slot): mtime-only
    legacy logic. Vulnerable to misattribution.

    Fixed 2026-05-25: legacy mtime-only mis-attributed a 3-day Telegram session
    accumulating 274M tokens to a 3-min Reddit-watch task ($894 false reading).
    """
    slug = _workspace_to_jsonl_slug(workspace_path)
    slug_dir = CLAUDE_PROJECTS_DIR / slug
    if not slug_dir.is_dir():
        return None
    if pre_set is not None:
        cand = []
        try:
            for f in slug_dir.glob("*.jsonl"):
                if f.name in pre_set:
                    continue
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if m >= spawn_ts - 10:
                    cand.append((m, f))
        except OSError:
            return None
        if cand:
            cand.sort(key=lambda x: x[0])
            return cand[0][1]
    # Fallback
    best = None
    best_mtime = 0
    try:
        for f in slug_dir.glob("*.jsonl"):
            try:
                m = f.stat().st_mtime
                if m >= spawn_ts - 10 and m > best_mtime:
                    best = f
                    best_mtime = m
            except OSError:
                continue
    except OSError:
        return None
    return best


def _summarize_session_jsonl(jsonl_path) -> dict:
    """Parse jsonl, sum tokens from all assistant messages' usage blocks.
    Returns dict with totals or empty dict on failure."""
    ti = tc_create = tc_read = to = 0
    model = None
    n_assistant = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                ti += usage.get("input_tokens", 0) or 0
                tc_create += usage.get("cache_creation_input_tokens", 0) or 0
                tc_read += usage.get("cache_read_input_tokens", 0) or 0
                to += usage.get("output_tokens", 0) or 0
                if not model:
                    model = msg.get("model")
                n_assistant += 1
    except (OSError, UnicodeDecodeError):
        return {}
    if n_assistant == 0:
        return {}
    return {
        "input_tokens": ti,
        "cache_creation_tokens": tc_create,
        "cache_read_tokens": tc_read,
        "output_tokens": to,
        "model": model or "",
        "assistant_messages": n_assistant,
    }


def _estimate_cost_usd(metrics: dict) -> float:
    """Compute cost in USD from token breakdown + model. Uses family match
    (opus/sonnet/haiku). Returns 0.0 if model unknown."""
    model = (metrics.get("model") or "").lower()
    if "opus" in model:
        rates = MODEL_PRICING["opus"]
    elif "sonnet" in model:
        rates = MODEL_PRICING["sonnet"]
    elif "haiku" in model:
        rates = MODEL_PRICING["haiku"]
    else:
        return 0.0
    p_in, p_out, p_cread, p_cwrite = rates
    cost = (
        metrics.get("input_tokens", 0) * p_in / 1_000_000.0
        + metrics.get("output_tokens", 0) * p_out / 1_000_000.0
        + metrics.get("cache_read_tokens", 0) * p_cread / 1_000_000.0
        + metrics.get("cache_creation_tokens", 0) * p_cwrite / 1_000_000.0
    )
    return round(cost, 6)


def _post_session_report(agent_name: str, agent_key: str, metrics: dict,
                         task_id: str = "") -> bool:
    """POST to Mesh /api/v1/agents/me/sessions/report. Returns True on 2xx.
    Best-effort; logs failure but never raises (must not break reaper)."""
    if not COST_REPORT_ENABLED:
        return False
    if not agent_key:
        log(agent_name, "cost-report: skip — agent_key missing in mesh-agents.json")
        return False
    if not _API_URL:
        log(agent_name, "cost-report: skip — _API_URL not set")
        return False

    # Mesh expects single tokens_in (sum of all input variants); we expose breakdown
    # in `meta` for future server-side analytics, but main totals are flat.
    tokens_in_total = (
        metrics.get("input_tokens", 0)
        + metrics.get("cache_creation_tokens", 0)
        + metrics.get("cache_read_tokens", 0)
    )
    cost = _estimate_cost_usd(metrics)
    body = {
        "tokens_in": tokens_in_total,
        "tokens_out": metrics.get("output_tokens", 0),
        "model": metrics.get("model", ""),
        "estimated_cost": cost,
        "task_id": task_id,
    }
    url = _API_URL.rstrip("/") + "/api/v1/agents/me/sessions/report"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "X-Agent-Key": agent_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=COST_REPORT_TIMEOUT_SEC) as resp:
            status = resp.status
            if 200 <= status < 300:
                log(agent_name,
                    f"cost-report sent: in={tokens_in_total} out={body['tokens_out']} "
                    f"cost=${cost:.4f} model={body['model']!r} task_id={task_id or '-'}")
                return True
            log(agent_name, f"cost-report: unexpected status {status}")
            return False
    except urllib.error.HTTPError as e:
        # endpoint deployed (PR #115, prod b862db3) — treat all HTTP errors uniformly
        log(agent_name, f"cost-report HTTPError {e.code}: {e.reason}")
        return False
    except urllib.error.URLError as e:
        log(agent_name, f"cost-report URLError: {e.reason}")
        return False
    except Exception as e:
        log(agent_name, f"cost-report unexpected error: {e}")
        return False


# Dedup set: jsonl paths already reported. Prevents double-billing
# when reap_dead fires for the same session twice (or watchdog reap).
_REPORTED_SESSIONS: set = set()
_REPORTED_LOCK = threading.Lock()


def _check_memory_enforcement(agent_name: str, jsonl_path) -> None:
    """Post-reap: detect close-without-memory pattern.

    Parse jsonl for tool_use events. If session had move_task→done call but
    zero mempalace_add_drawer / mcp__evc-mesh__remember calls AND session
    lasted >5min — log warning + post enforcement comment on the done task.

    Detection-based: can't reverse the close, but creates visible feedback
    so agent's next session is aware (commented on its own task, will
    re-load context with the warning visible).

    the operator rule 2026-05-26 («Stop hook hard-enforce для memory writes»).
    """
    if not jsonl_path or not jsonl_path.is_file():
        return
    try:
        first_ts = None
        last_ts = None
        done_task_ids = []   # task_ids that received move_task→done
        memory_writes = 0
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = d.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") or {}
                content = msg.get("content") or []
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "")
                    inp = block.get("input") or {}
                    if name in ("mempalace_add_drawer",
                                "mcp__evc-mesh__remember"):
                        memory_writes += 1
                    elif name == "mcp__evc-mesh__move_task":
                        if inp.get("status_slug") == "done":
                            tid = inp.get("task_id")
                            if tid:
                                done_task_ids.append(tid)
        if not done_task_ids:
            return  # no close happened, nothing to enforce
        if memory_writes > 0:
            return  # had at least one write — pass
        # Compute session duration (best-effort from first/last ts)
        try:
            from datetime import datetime as _dt
            f_dt = _dt.fromisoformat(first_ts.replace("Z", "+00:00")) if first_ts else None
            l_dt = _dt.fromisoformat(last_ts.replace("Z", "+00:00")) if last_ts else None
            duration_min = (l_dt - f_dt).total_seconds() / 60 if f_dt and l_dt else 0
        except Exception:
            duration_min = 0
        if duration_min < 5:
            return  # too short, likely trivial 1-comment close
        log(agent_name,
            f"MEMORY-ENFORCEMENT: agent closed {len(done_task_ids)} task(s) "
            f"without any mempalace/remember call in a {duration_min:.0f}-min session")
        # Post enforcement comment on the most recent done-task
        target_tid = done_task_ids[-1]
        agent_cfg = _AGENTS_BY_NAME.get(agent_name) or {}
        agent_key = agent_cfg.get("agent_key", "")
        if not agent_key:
            return
        body = (
            "⚠️ **Memory enforcement** (the operator rule 2026-05-26): эта session "
            f"закрыла {len(done_task_ids)} задач(у) за {duration_min:.0f} мин "
            "**без единого mempalace_add_drawer / mcp__evc-mesh__remember "
            "вызова**.\n\n"
            "Per `CLAUDE-workflow.md §0f` — before closing a task, write at "
            "least one drawer (kind:session-checkpoint or kind:decision) "
            "with the lesson/state. Otherwise the work is invisible to "
            "future sessions and `mempalace_search` from other agents "
            "returns nothing.\n\n"
            "_This comment is auto-generated by mesh-dispatcher reap-path enforcement._"
        )
        try:
            payload = json.dumps({"body": body}).encode()
            req = Request(
                f"{_API_URL}/api/v1/tasks/{target_tid}/comments",
                data=payload,
                headers={"Content-Type": "application/json",
                         "X-Agent-Key": agent_key},
                method="POST",
            )
            with urlopen(req, timeout=10) as r:
                r.read()
            log(agent_name, f"MEMORY-ENFORCEMENT: posted on {target_tid[:8]}")
        except (HTTPError, URLError, OSError) as e:
            log(agent_name, f"MEMORY-ENFORCEMENT post failed: {e}")
    except Exception as e:
        log(agent_name, f"memory-enforcement check error: {e}")


def _upload_breadcrumbs(agent_name, workspace, agent_key):
    """Upload headless-checkpoint-*.md files from the agent's memory dir to the
    Mesh memories API, then move each successfully-uploaded file to processed/.

    Best-effort: never raises. Runs inside an existing daemon thread."""
    if not _API_URL or not agent_key:
        return
    try:
        import re as _re_bc  # local import; avoids module-level dependency
        slug = _workspace_to_jsonl_slug(workspace)
        memory_dir = CLAUDE_PROJECTS_DIR / slug / "memory"
        if not memory_dir.is_dir():
            return
        candidates = [
            p for p in memory_dir.glob("headless-checkpoint-*.md")
            if p.is_file()
        ]
        if not candidates:
            return
        processed_dir = memory_dir / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)
        uploaded = 0
        for fpath in candidates:
            try:
                raw = fpath.read_text(encoding="utf-8", errors="replace")
                key = fpath.stem
                content = raw
                owner = agent_name
                try:
                    if raw.startswith("---"):
                        parts = raw.split("---")
                        # parts[0] == "" (before opening ---), parts[1] == frontmatter,
                        # parts[2:] joined with "---" == body (handles --- in body).
                        if len(parts) >= 3:
                            frontmatter = parts[1]
                            body = "---".join(parts[2:]).lstrip("\n")
                            for fm_line in frontmatter.splitlines():
                                fm_line = fm_line.strip()
                                if fm_line.startswith("name:"):
                                    val = fm_line[len("name:"):].strip().strip("\"'")
                                    if val:
                                        key = val
                                    break
                            content = body if body else raw
                            m = _re_bc.search(r"- wing: `(\w+)`", content)
                            if m:
                                owner = m.group(1)
                except Exception:
                    key = fpath.stem
                    content = raw
                    owner = agent_name
                payload = json.dumps({
                    "key": key,
                    "content": content,
                    "scope": "workspace",
                    "tags": ["kind:session-checkpoint", "owner:" + owner, "headless:true"],
                    "relevance": 0.5,
                }).encode()
                req = Request(
                    _API_URL.rstrip("/") + "/api/v1/memories",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Agent-Key": agent_key,
                    },
                    method="POST",
                )
                try:
                    with urlopen(req, timeout=15) as resp:
                        status = resp.status
                    if status in (200, 201):
                        fpath.rename(processed_dir / fpath.name)
                        uploaded += 1
                    else:
                        log(agent_name,
                            f"breadcrumb-upload: unexpected status {status} for {fpath.name}")
                except HTTPError as e:
                    log(agent_name,
                        f"breadcrumb-upload: HTTP {e.code} for {fpath.name}: {e.reason}")
                except (URLError, OSError) as e:
                    log(agent_name,
                        f"breadcrumb-upload: network error for {fpath.name}: {e}")
            except Exception as e:
                log(agent_name, f"breadcrumb-upload: error processing {fpath.name}: {e}")
        log(agent_name,
            f"breadcrumb-upload: uploaded {uploaded}/{len(candidates)} for {agent_name}")
    except Exception as e:
        log(agent_name, f"breadcrumb-upload: outer error: {e}")


def _report_session_cost(agent_name: str, workspace: str, spawn_ts: float,
                          pre_set=None, task_id: str = "") -> None:
    """Top-level wrapper: find jsonl, summarize, POST. Best-effort.
    Runs in a daemon thread so reap_dead returns immediately.
    Dedup-safe (2026-05-24). Accepts pre-spawn jsonl snapshot for accurate
    attribution (2026-05-25)."""
    if not COST_REPORT_ENABLED:
        return
    try:
        jsonl = _find_session_jsonl(workspace, spawn_ts, pre_set=pre_set)
        if jsonl is None:
            return  # silent — old sessions may not have jsonl
        jsonl_key = str(jsonl)
        with _REPORTED_LOCK:
            if jsonl_key in _REPORTED_SESSIONS:
                log(agent_name, f"cost-report: skip (already reported {jsonl.name})")
                return
        metrics = _summarize_session_jsonl(jsonl)
        if not metrics or metrics.get("assistant_messages", 0) == 0:
            return  # nothing to report
        agent_cfg = _AGENTS_BY_NAME.get(agent_name) or {}
        agent_key = agent_cfg.get("agent_key", "")
        if _post_session_report(agent_name, agent_key, metrics, task_id=task_id):
            with _REPORTED_LOCK:
                _REPORTED_SESSIONS.add(jsonl_key)
                # Bound the set (avoid unbounded growth)
                if len(_REPORTED_SESSIONS) > 1000:
                    _REPORTED_SESSIONS.pop()
    except Exception as e:
        log(agent_name, f"cost-report wrapper error: {e}")


def _release_checkout(agent_name: str, task_id: str) -> None:
    """Force-release a task checkout lock when its claude session exits.

    Uses DELETE /tasks/:id/checkout?force=true — the token-free path.
    Dispatcher does not cache the checkout_token (returned by checkout_task MCP
    to the agent, not to the dispatcher), so force=true is the correct path.
    Best-effort: never raises. Logs each attempt.
    """
    if not _API_URL:
        return
    agent_cfg = _AGENTS_BY_NAME.get(agent_name) or {}
    agent_key = agent_cfg.get("agent_key", "")
    if not agent_key:
        log(agent_name,
            f"session-exit release: skip {task_id[:8]} — no agent_key")
        return
    try:
        url = f"{_API_URL}/api/v1/tasks/{task_id}/checkout?force=true"
        req = Request(url, method="DELETE", headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=10) as resp:
            status = resp.status
            if status in (200, 204):
                log(agent_name,
                    f"session-exit release: released checkout {task_id[:8]} "
                    f"(HTTP {status})")
            elif status == 404:
                log(agent_name,
                    f"session-exit release: {task_id[:8]} had no active "
                    f"checkout (HTTP 404) — already expired or never acquired")
            else:
                log(agent_name,
                    f"session-exit release: unexpected HTTP {status} "
                    f"for {task_id[:8]}")
    except HTTPError as e:
        if e.code == 404:
            log(agent_name,
                f"session-exit release: {task_id[:8]} had no active "
                f"checkout (HTTP 404) — already expired or never acquired")
        else:
            log(agent_name,
                f"session-exit release: HTTPError {e.code} "
                f"for {task_id[:8]}: {e.reason}")
    except URLError as e:
        log(agent_name,
            f"session-exit release: URLError for {task_id[:8]}: {e.reason}")
    except Exception as e:
        log(agent_name,
            f"session-exit release: error for {task_id[:8]}: {e}")


def reap_dead():
    """Drop slots whose child exited / pid is dead / reservation went stale.
    Idempotent; safe for the reaper thread AND for Watchdog 9fca65aa to call."""
    now = time.monotonic()
    _to_release = []  # (agent, task_id) pairs with real pids to release post-lock
    with _DISPATCH_LOCK:
        dead = []
        for key, v in _LIVE.items():
            proc = v.get("proc")
            if proc is not None and proc.poll() is not None:
                dead.append(key)
                continue
            pid = v.get("pid")
            if pid:
                if not _pid_alive(pid):
                    dead.append(key)
            elif v.get("reserved") and now - v.get("mono", now) > RESERVE_TTL:
                dead.append(key)          # spawn never completed
        for key in dead:
            v = _LIVE.pop(key, None)
            pid = v.get("pid") if v else None
            log_name = v.get("log") if v else None
            mono_started = v.get("mono", 0) if v else 0
            duration_s = now - mono_started if mono_started else 0
            log(key[0], f"reaped slot ({key[0]},{key[1]}) pid={pid}")
            # Mark agent for pull-on-reap (processed AFTER lock released).
            _REAP_AGENTS_TO_PULL.add(key[0])
            # Session-exit checkout release (task 5563f1bc): queue real sessions
            # (pid attached = agent actually ran and may have called checkout_task).
            # Reserved-only slots (spawn aborted before pid attached) are excluded.
            if pid:
                _to_release.append(key)
                # B3.3 event-coalesce: mark this (agent, task_id) as recently-completed
                # so claim_dispatch holds the next event for COALESCE_WINDOW_SEC.
                # Crash-suspects clear quickly via _CRASH_RETRY fast-retry path.
                _COALESCE_COMPLETED[key] = now
            # Schedule cost report (added 2026-05-22). Capture workspace+started_at
            # while we still hold the lock — fire async after release.
            if COST_REPORT_ENABLED:
                _ws = (_AGENTS_BY_NAME.get(key[0]) or {}).get("workspace")
                _spawn_ts = v.get("started_at", 0) if v else 0
                # Pass pre-spawn jsonl snapshot so attribution picks the right file
                # (fix 2026-05-25 — defends against concurrent session misattribution).
                _pre_set = set(v.get("jsonl_pre_set", [])) if v else None
                if _ws and _spawn_ts:
                    threading.Thread(
                        target=_report_session_cost,
                        args=(key[0], _ws, _spawn_ts),
                        kwargs={"pre_set": _pre_set, "task_id": key[1]},
                        daemon=True,
                    ).start()
                    # Memory enforcement check (the operator rule 2026-05-26):
                    # post-reap analysis of close-without-write pattern.
                    def _enforce_wrapper(_an=key[0], _ws=_ws, _ts=_spawn_ts,
                                         _ps=_pre_set):
                        _ak = (_AGENTS_BY_NAME.get(_an) or {}).get("agent_key", "")
                        try:
                            jp = _find_session_jsonl(_ws, _ts, pre_set=_ps)
                            if jp is not None:
                                _check_memory_enforcement(_an, jp)
                            _upload_breadcrumbs(_an, _ws, _ak)
                        except Exception as _e:
                            log(_an, f"memory-enforcement wrapper error: {_e}")
                    threading.Thread(target=_enforce_wrapper, daemon=True).start()
            # Empty-session-log crash detection (added 2026-05-21).
            # If reaped within grace window AND log file is 0 bytes — likely
            # claude crashed on startup (API rate-limit, MCP init fail, OOM).
            # Mark for fast retry, parallel to _REPO_UNSAFE.
            # Skip recovered sessions: they were started by a pre-restart
            # dispatcher, our "duration" is time since recovery not since
            # spawn, and our "log empty" check looks at a stale file.
            is_recovered = v.get("recovered", False) if v else False
            if log_name and duration_s < EMPTY_LOG_CRASH_GRACE_SEC and not is_recovered:
                try:
                    log_path = LOG_DIR / log_name
                    # Auth expiry first: it is NOT retryable, so it must not reach the
                    # crash-retry path below. Re-spawning an unauthenticated agent just
                    # produces another 0.1s corpse — that loop is what burned 1354 Orbit
                    # sessions over 07-08..07-10 while every existing guard stayed silent.
                    auth_kw = _detect_auth_fail_in_log(log_path)
                    # Then — try to detect rate-limit signature in log content
                    rate_kw = "" if auth_kw else _detect_rate_limit_in_log(log_path)
                    log_empty = (not auth_kw) and log_path.is_file() and log_path.stat().st_size == 0
                    if auth_kw:
                        log(key[0],
                            f"AUTH-DEAD ({key[0]},{key[1]}) reap@{duration_s:.0f}s "
                            f"— {auth_kw!r} in session log — NOT retrying (login required)")
                        _trigger_auth_fail_pause(
                            f"keyword {auth_kw!r} in {key[0]}'s session log")
                    elif rate_kw or log_empty:
                        with _REPO_UNSAFE_LOCK:
                            _CRASH_RETRY[key[1]] = now
                            _CRASH_COUNT[key[1]] = _CRASH_COUNT.get(key[1], 0) + 1
                            crash_n = _CRASH_COUNT[key[1]]
                            _CRASH_HISTORY.append((time.monotonic(), key[0], key[1],
                                                   "rate-limit-kw" if rate_kw else "empty-log"))
                        delay = _crash_retry_delay(crash_n)
                        crash_kind = f"rate-limit ({rate_kw!r})" if rate_kw else f"empty log"
                        log(key[0],
                            f"CRASH-SUSPECT ({key[0]},{key[1]}) reap@{duration_s:.0f}s "
                            f"— {crash_kind} — crash #{crash_n}, retry in {delay}s")
                        # Immediate rate-limit detection: if keyword match → pause now,
                        # don't wait for cluster threshold.
                        if rate_kw:
                            _trigger_rate_limit_pause(
                                f"keyword {rate_kw!r} in {key[0]}'s session log")
                        else:
                            # Cluster check — multiple empty-log crashes in window.
                            _check_rate_limit_cluster()
                        # Escalate after CRASH_MAX_RETRIES.
                        if crash_n >= CRASH_MAX_RETRIES:
                            log(key[0],
                                f"CRASH-EXHAUSTED ({key[0]},{key[1]}) — {crash_n} consecutive "
                                "crashes. Per-agent pause set. /restart needed.")
                            try:
                                (PAUSE_DIR / f"PAUSE_{key[0]}").touch()
                            except OSError:
                                pass
                except OSError:
                    pass
        if dead:
            _persist_live()
    # Pull-on-reap (added 2026-05-22): if any slot just freed, immediately
    # try to dispatch next backlog task for that agent. Avoid 5-min
    # stale-redispatch wait. _REAP_AGENTS_TO_PULL set inside the lock above.
    pull_agents = _REAP_AGENTS_TO_PULL.copy()
    _REAP_AGENTS_TO_PULL.clear()
    for agent_name in pull_agents:
        agent_cfg = _AGENTS_BY_NAME.get(agent_name)
        if agent_cfg:
            try:
                _pull_next_task_for_agent(agent_cfg, _API_URL)
            except Exception as e:
                log("reaper", f"pull-on-reap failed for {agent_name}: {e}")
    # Session-exit checkout release (task 5563f1bc): fire DELETE
    # /tasks/:id/checkout?force=true for each real session that just exited.
    # Done post-lock in daemon threads so HTTP I/O never blocks the reaper.
    for (rel_agent, rel_task) in _to_release:
        threading.Thread(
            target=_release_checkout,
            args=(rel_agent, rel_task),
            daemon=True,
        ).start()


def _reaper_loop():
    while True:
        time.sleep(20)
        try:
            reap_dead()
        except Exception as e:
            log("dispatcher", f"reaper error: {e}")


def _breaker_active(agent: str, task_id: str) -> tuple:
    """Check the flap circuit breaker flag for (agent, task_id).

    Returns (tripped, why):
      tripped=True  -> caller MUST refuse to spawn and log a BREAKER line.
      tripped=False, why=""           -> no flag, proceed silently.
      tripped=False, why=<auto-clear> -> flag was stale (> BREAKER_TTL_SEC),
                                          unlinked; caller should log + proceed.
    Fails OPEN on corrupt/unreadable JSON: a typo'd flag must not pin a
    task forever; watchdog will rewrite it on the next real flap.
    """
    if not task_id:
        return False, ""
    flag = BREAKER_DIR / f"breaker-{agent}-{task_id}.flag"
    if not flag.exists():
        return False, ""
    try:
        data = json.loads(flag.read_text())
        tripped_at = float(data.get("tripped_at", 0))
    except (OSError, ValueError, TypeError):
        return False, ""
    age = time.time() - tripped_at
    if age > BREAKER_TTL_SEC:
        try:
            flag.unlink()
        except OSError:
            pass
        return False, f"auto-cleared stale breaker (age={int(age)}s > TTL={BREAKER_TTL_SEC}s)"
    launches = data.get("launches_last_hour", "?")
    return True, (f"tripped {int(age)}s ago, launches_last_hour={launches}, "
                  f"flag={flag.name} — remove flag manually or wait TTL "
                  f"({BREAKER_TTL_SEC}s)")


def claim_dispatch(agent: str, task_id: str) -> tuple:
    """Gate before spawning. Returns (ok, reason).

    ok=False -> caller logs 'skip duplicate spawn' and returns (NO spawn).
    ok=True  -> a slot is RESERVED for (agent,task_id); caller MUST then
                either register_pid() after spawn or unclaim() if it aborts.
    """
    if not task_id:
        return True, "no task_id (no dedup key)"
    key = (agent, task_id)
    now = time.monotonic()
    with _DISPATCH_LOCK:
        # inline reap of just this key so the decision is correct
        v = _LIVE.get(key)
        if v is not None:
            proc = v.get("proc")
            pid = v.get("pid")
            if proc is not None and proc.poll() is not None:
                _LIVE.pop(key, None); v = None
            elif pid and not _pid_alive(pid):
                _LIVE.pop(key, None); v = None
            elif v.get("reserved") and not pid and now - v.get("mono", now) > RESERVE_TTL:
                _LIVE.pop(key, None); v = None
        # 1. a still-live session for this pair -> hard skip
        if v is not None:
            return (False, f"alive pid={v['pid']}") if v.get("pid") \
                else (False, "spawn already in progress")
        # 2. debounce a burst of repeats
        last = _RECENT.get(key)
        if last is not None and now - last < DEBOUNCE_SEC:
            return False, f"debounced ({int(now - last)}s < {DEBOUNCE_SEC}s)"
        # 2b. B3.3 event-coalesce: if a session for this (agent, task) just
        # completed, hold for COALESCE_WINDOW_SEC before allowing a new spawn.
        # Collapses burst @-mentions / status-change events arriving immediately
        # after reap into a single spawn instead of N successive ones.
        coalesce_ts = _COALESCE_COMPLETED.get(key)
        if coalesce_ts is not None and now - coalesce_ts < COALESCE_WINDOW_SEC:
            return False, f"coalesced — completed {int(now - coalesce_ts)}s ago < {COALESCE_WINDOW_SEC}s"
        # 3. clear -> reserve the slot
        # Snapshot existing jsonls so reap can identify the NEW jsonl this
        # spawn creates (fix 2026-05-25 — see _find_session_jsonl docstring).
        _ws_for_snap = (_AGENTS_BY_NAME.get(key[0]) or {}).get("workspace", "")
        _jsonl_pre_snap = _snapshot_jsonls(_ws_for_snap) if _ws_for_snap else set()
        _LIVE[key] = {"pid": None, "proc": None, "mono": now,
                      "started_at": time.time(), "reserved": True, "log": None,
                      "jsonl_pre_set": list(_jsonl_pre_snap)}
        _RECENT[key] = now
        # opportunistic prune of stale debounce keys
        for k in [k for k, ts in _RECENT.items() if now - ts > DEBOUNCE_SEC]:
            _RECENT.pop(k, None)
        _persist_live()
        return True, "claimed"


def register_pid(agent: str, task_id: str, proc, log_name: str):
    """Attach the real child pid to a reserved slot (after a successful spawn)."""
    if not task_id:
        return
    with _DISPATCH_LOCK:
        v = _LIVE.get((agent, task_id))
        if v is not None:
            v.update(pid=proc.pid, proc=proc, reserved=False, log=log_name)
            _persist_live()


def unclaim(agent: str, task_id: str):
    """Release a reserved slot when the launch is aborted before spawn (e.g.
    the reposync gate refused). _RECENT is intentionally kept so a rapid retry
    is still debounced, but a later legitimate re-assignment is not blocked."""
    if not task_id:
        return
    with _DISPATCH_LOCK:
        if _LIVE.pop((agent, task_id), None) is not None:
            _persist_live()


# Phase → default model (overridable via `model:<name>` label).
# Rationale: heavy cognitive work (discuss/plan/debug) gets opus,
# mechanical/verify work gets sonnet. Saves tokens without quality loss.
PHASE_MODEL = {
    "discuss": "opus",
    "plan": "opus",
    "execute": "sonnet",
    "verify": "sonnet",
    "ship": "sonnet",
    "debug": "opus",
}


def fetch_task_meta(agent_key: str, task_id: str, api_url: str) -> tuple:
    """Fetch (labels, title, description, parent_task_id, assignee_name) for a
    task via Mesh API (best-effort).

    SSE task.assigned/task.created events carry no `title` field, so the
    dispatched prompt would otherwise get title='' and the agent has to
    re-fetch it itself (or, worse, hang). We resolve it here once.
    parent_task_id (G1-s3) is used to flag captain/subtask spawns for
    graph-recall context injection.
    assignee_name (task 98a1db69) feeds the lane-identity routing gate — the
    dispatcher must know WHOSE card it is before framing the spawn prompt as
    "you have a new task assigned".
    Returns ([], "", "", None, "") on any failure (preserves prior behavior;
    an empty assignee_name makes the routing gate fail OPEN).
    """
    if not task_id:
        return [], "", "", None, ""
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}"
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return (
            (data.get("labels") or []),
            (data.get("title") or ""),
            (data.get("description") or ""),
            (data.get("parent_task_id") or None),
            (data.get("assignee_name") or ""),
        )
    except Exception:
        return [], "", "", None, ""


def fetch_task_scheduled_until(agent_key: str, task_id: str, api_url: str) -> str:
    """Return the raw `due_date` when it is in the FUTURE, else "".

    Fifth and last site of the scheduled-task gate. The other four —
    stale_redispatcher_loop (2026-05-22), _pull_next_task_for_agent
    (2026-07-28, #ba50c1a2) and both fiddler paths (#c3c906a9 / #202e68ee) —
    read `due_date` off a task dict they already hold. The SSE branch has only
    the event envelope, which carries no due_date, so this costs one GET.

    Fails OPEN (returns "") on any error, matching every other gate in this
    file: an unreachable API must not silently mute dispatch.
    """
    if not task_id:
        return ""
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}"
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        due = data.get("due_date")
        if not due:
            return ""
        due_dt = _parse_iso_utc(due)
        if due_dt is None:
            return ""
        # `timezone` is not module-level in this file (only `datetime` is), and
        # _parse_iso_utc returns a NAIVE UTC datetime — compare like-for-like.
        from datetime import timezone as _timezone
        if due_dt > datetime.now(_timezone.utc).replace(tzinfo=None):
            return str(due)
        return ""
    except Exception:
        return ""


# project_id -> {status_id: category}. Populated only from a NON-EMPTY status
# list. A cache keyed on "have I asked?" rather than "do I have an answer?"
# turns one flaky read into a permanent blindfold (#eec06ed8: `_project_statuses`
# cached [] behind `if pid not in cache`, so a single 20s timeout made every
# later category lookup in the process return "" — and it survived the API
# recovering). Here that failure mode would silently disable this gate for the
# lifetime of the daemon, which is exactly the bug being fixed. So: no negative
# caching. A failed or empty read is retried on the next event.
_STATUS_CAT_CACHE: dict = {}


def fetch_task_status_category(agent_key: str, task_id: str, api_url: str) -> str:
    """Return the task's status CATEGORY (e.g. "backlog"/"todo"/"review"), or "".

    Third site of the `_SKIP_CATEGORIES` gate (#5d821586). The two sweep paths
    (`_pull_next_task_for_agent` :3737, `stale_redispatcher_loop` :4433) read
    `_status_category` off a task dict they already hold, because they fetched
    BY category. The SSE branch has only the event envelope, which carries no
    status at all — so this costs one GET, plus one per-project GET for the
    status table (cached).

    Mesh tasks carry `status_id` (a per-project UUID), never a category, so the
    id must be resolved through `/projects/{pid}/statuses`. Same two-hop lookup
    `_warn_delegation_review_trap` already does; it cannot be reused because it
    is deliberately fire-and-forget in a thread and must stay non-blocking.

    Fails OPEN (returns "") on any error, matching every other gate in this
    file: an unreachable API must not silently mute the whole fleet's dispatch.
    A wrong spawn costs one session; a gate that fails CLOSED on a flake costs
    every session, and looks identical to a healthy quiet day.
    """
    if not task_id:
        return ""
    try:
        def _get(url):
            req = Request(url, headers={"X-Agent-Key": agent_key})
            with urlopen(req, timeout=10) as r:
                return json.loads(r.read())

        data = _get(f"{api_url}/api/v1/tasks/{task_id}")
        task = data.get("task") or data
        pid = task.get("project_id")
        status_id = task.get("status_id")
        if not pid or not status_id:
            return ""
        m = _STATUS_CAT_CACHE.get(pid)
        if not m:
            statuses = _get(f"{api_url}/api/v1/projects/{pid}/statuses")
            m = {s["id"]: (s.get("category") or s.get("type") or "")
                 for s in (statuses or []) if s.get("id")}
            if m:                       # never cache an empty/failed lookup
                _STATUS_CAT_CACHE[pid] = m
        return str(m.get(status_id) or "")
    except Exception:
        return ""


# --- Mention author/body normalization + ask-vs-attribution gate (#5a69b827) ---
# Measured 2026-07-27 on ~/logs/mesh-dispatcher-stdout.log: 290/290 task.mentioned
# wakes logged `mentioned by user ''`, and 1691/1691 task.commented events logged
# "no @<name> mention, skip" with 0 wakes ever. Cause: this file read three field
# names the server has never emitted — `mention_author_kind`, `mention_author_name`,
# `comment_body_preview`. Mesh puts the author at TOP level (`actor_type` /
# `actor_name` / `actor_id`) and the body NESTED at `comment.body`
# (evc-mesh internal/service/agent_notify_service.go:23-37, comment_service.go:536-553).
#
# Three independent defects followed, all fixed here:
#   1. `author_kind` defaulted to "user" → every wake in fleet history told the
#      session a HUMAN was waiting. A missing field must fail HONEST, not fail
#      URGENT ([[learnings_one_sided_framing_banner_mislabels_the_rest]]).
#   2. The self-mention loop guard compared `author_name` (always "") to our own
#      name → unreachable. Re-keyed onto `actor_id` vs the event's recipient
#      `agent_id`; both UUIDs are always present in the frame.
#   3. The spawn fired on any prose citation of the agent's name. Of four measured
#      wakes on #2c087b2a, three were pure attribution («числа 32/45 у @orbit»);
#      one billed $11.55. A trigger keyed on a TOKEN has no upper bound on false
#      wakes — it scales with how often peers cite you.
_MENTION_ASK_GATE_ENABLED = os.environ.get("DISPATCHER_MENTION_ASK_GATE", "1") == "1"
_MENTION_GATE_METRICS = Path(os.environ.get(
    "DISPATCHER_MENTION_GATE_METRICS",
    str(Path.home() / ".openclaw" / "metrics" / "dispatcher-mention-gate.jsonl")))

# Ask cues. Deliberately BROAD: this gate's only job is to catch the pure-citation
# case, so any hint of a request, handoff or hand-back must fall through to a wake
# — narrowing the wake path is what cost 17h of rot in #1c646d59 / #d45d48f5.
# Matched with \b word boundaries (unicode-aware in py3) so that the IMPERATIVE
# «подтверди» does not fire on the PAST TENSE «подтвердил» — the latter is exactly
# the attribution shape we are trying to drop.
_ASK_CUES = re.compile(
    r"\b("
    # --- ru: imperatives / requests ---
    r"нужно|нужен|нужна|нужны|надо|прошу|просьба|пожалуйста|плиз|требуется|"
    r"можешь|сможешь|сделай|сделайте|проверь|проверьте|подтверди|подтвердите|"
    r"ответь|ответьте|реши|решите|закрой|закройте|посмотри|посмотрите|глянь|"
    r"гляньте|погляди|апрувни|прими|забери|возьми|запусти|поправь|"
    r"почини|уточни|скажи|напиши|доложи|верни|перевесь|"
    # --- ru: handoff / ownership / expectation ---
    r"owner|назначил|назначаю|назначен|переназначил|перевесил|перевешиваю|"
    r"ждём|жду|ожидаю|блокер|блокирует|твоя\s+очередь|на\s+тебе|за\s+тобой|"
    r"вопрос|аск|"
    # --- en: imperatives / requests / handoff ---
    r"please|pls|plz|can\s+you|could\s+you|would\s+you|need\s+you|needs\s+your|"
    r"your\s+call|action\s+required|assign|assigned|reassigned|handing|handoff|"
    r"hand\s+off|over\s+to\s+you|take\s+over|review|approve|ack|acknowledge|"
    r"confirm|respond|reply|ping|nudge|blocker|blocked|fyi|cc|todo|ask|"
    r"waiting\s+on|up\s+to\s+you"
    r")\b",
    re.IGNORECASE,
)


def _mention_fields(event: dict) -> dict:
    """Normalize author + body out of a comment-bearing SSE event.

    Reads the names the server ACTUALLY sends, keeping the historical names as
    fallbacks in case an older/newer build emits them. `author_kind` comes back
    "" (not "user") when the server told us nothing — unknown must render as
    unknown downstream.
    """
    comment = event.get("comment")
    if not isinstance(comment, dict):
        comment = {}
    kind = (event.get("actor_type") or event.get("mention_author_kind")
            or event.get("author_kind") or event.get("author_type") or "")
    name = (event.get("actor_name") or event.get("mention_author_name")
            or event.get("author_name") or "")
    body = (comment.get("body") or event.get("comment_body_preview")
            or event.get("comment_body") or event.get("body") or "")
    return {
        "author_kind": str(kind).strip().lower(),
        "author_name": str(name).strip(),
        "author_id": str(comment.get("author_id") or event.get("actor_id") or "").strip(),
        "recipient_id": str(event.get("agent_id") or "").strip(),
        "comment_preview": body,
    }


def _mention_is_self(agent_name: str, fields: dict) -> bool:
    """True when WE authored the comment that is waking us.

    Keyed on UUIDs (`actor_id` == the event's recipient `agent_id`), which are
    always in the frame, instead of the display name that was always empty.
    Falls back to the name comparison only when an id is missing — so the guard
    degrades to the old behaviour rather than to "never self".
    """
    author_id = (fields.get("author_id") or "").lower()
    recipient_id = (fields.get("recipient_id") or "").lower()
    if author_id and recipient_id:
        return author_id == recipient_id
    author_name = (fields.get("author_name") or "").strip().lower()
    return bool(author_name) and author_name == (agent_name or "").strip().lower()


# --- Dispatcher-authored park notice: author is nominally the agent, actually us ---
# (#61ad469a, 2026-07-31.)
#
# `auto_triage_to_creator` posts its "your card is parked, decide (a)/(b)/(c)" notice
# with the STUCK AGENT'S key, because that is the only key the dispatcher holds. When
# creator == assignee — the ordinary shape of a self-filed card — the resulting
# task.mentioned frame has author_id == recipient_id, so `_mention_is_self` is True and
# the wake is dropped. Measured on the live gate log: 18 of 20 self-skips ever recorded
# were this exact comment; only 2 were an agent genuinely citing itself. The card then
# sits in `triage`, which fiddler does not feed (fetch_open_tasks pulls `todo` only) and
# which the dispatcher parks as "for human" — so the ask reaches nobody at all. 9 of the
# 12 triage cards >48h invisible to both the operator channels on 2026-07-31 were silenced here.
#
# The loop guard itself is right: an agent must not wake itself with its own prose. This
# carves out the one case where the authorship is a credential artefact rather than a
# fact — and carves it out on a sentinel the dispatcher emits, never on the prose around
# it, because prose gets quoted back ([[canon-phantom-blocking-marker-in-driver-comments]]:
# a detector that reads quoted text as an assertion is how the phantom-escalation latch
# was built).
#
# Deliberately NOT the shared AUTO_MARKERS list. That list includes `**авто-перепроверка`
# — the re-verify ping, which is posted repeatedly and would become a self-sustaining
# wake loop. This exemption covers only the terminal park notice, which is emitted at
# most once per task (guarded by `_TRIAGED_AUTO` / `triaged_already`).
_DISPATCHER_PARK_SENTINEL = "<!-- mesh-dispatcher:auto-triage-notify -->"


def _is_dispatcher_park_notify(fields: dict) -> bool:
    """True for the dispatcher's own count==3 park notice, whoever's key signed it.

    Requires the sentinel to OPEN the body. A session that quotes the park notice back
    into its own report carries the sentinel mid-body or inside a blockquote; only the
    dispatcher writes it first. Without the leading-position rule, one quoted line would
    re-arm exactly the self-wake loop `_mention_is_self` exists to prevent.

    Only newlines are tolerated ahead of it, never spaces or tabs: `lstrip()` would accept
    a 4-space-indented sentinel, i.e. one sitting inside a markdown code block, which is
    precisely how a session pasting the notice into its own report would carry it. The
    writer stamps the sentinel at index 0, so nothing legitimate needs the slack.

    The sentinel must also be the WHOLE first line. `startswith` alone accepts
    `<!-- … --> — цитирую диспетчера`, i.e. the sentinel with commentary appended, which
    is a shape a session summarising its own card could plausibly produce.
    """
    body = (fields.get("comment_preview") or "").lstrip("\r\n")
    if not body.startswith(_DISPATCHER_PARK_SENTINEL):
        return False
    rest = body[len(_DISPATCHER_PARK_SENTINEL):]
    return rest == "" or rest.startswith("\n") or rest.startswith("\r")


def _park_notify_claim(task_id: str) -> bool:
    """Grant the park-notice self-wake AT MOST ONCE per task, per dispatcher process.

    The sentinel is a fixed public string, so it can in principle be reproduced verbatim
    at position 0 by a session quoting its own card — the verifier on #61ad469a built
    exactly that body and it woke. Tightening the string match narrows the shape but
    cannot close it; what actually matters is that a self-wake can never REPEAT, because
    a loop is the only failure mode `_mention_is_self` exists to prevent. This bounds the
    worst case to one extra spawn per task instead of an unbounded cycle, which is the
    same bound the writer already has (`_TRIAGED_AUTO` posts the notice once).
    """
    with _STALE_LOCK:
        if task_id in _PARK_NOTIFY_WOKEN:
            return False
        _PARK_NOTIFY_WOKEN.add(task_id)
        return True


def _mention_is_ask(agent_name: str, fields: dict) -> tuple:
    """Is this mention a REQUEST, or just prose citing our name?

    Returns (wake: bool, reason: str). Fails OPEN in every ambiguous case — the
    only shape that returns False is "an agent wrote a body we can read, our
    handle appears in it, and nothing anywhere in it asks for anything".

    Wakes unconditionally on:
      • a human author — never drop the operator on a heuristic;
      • an empty/unavailable body — we cannot judge what we cannot read;
      • a question mark ANYWHERE in the body — a question is an ask even when
        it is phrased without a single cue word («у @orbit цифра была такая.
        Правда ведь?» woke nothing until this was widened, 2026-07-27 verify);
      • our handle in an ADDRESSED position (start of a line, or immediately
        followed by `?`/`:`) — that is direct address regardless of vocabulary;
      • any ask cue anywhere in the body (see _ASK_CUES — includes `FYI`/`cc`,
        because Atlas's `/sweep` "FYI @orbit" nudges are early warnings I want).
    """
    if not _MENTION_ASK_GATE_ENABLED:
        return True, "gate-disabled"
    body = fields.get("comment_preview") or ""
    if not body.strip():
        return True, "body-unavailable"
    kind = (fields.get("author_kind") or "").lower()
    if kind in ("user", "human"):
        return True, "human-author"
    if not kind:
        return True, "author-kind-unknown"
    handle = (agent_name or "").strip().lower()
    if not handle:
        return True, "self-name-unknown"
    if "?" in body or "？" in body:
        return True, "question-mark"
    low = body.lower()
    if f"@{handle}" not in low:
        # Server-side mention parsing found something we cannot see (display-name
        # form, slug alias, …). Not ours to second-guess.
        return True, "handle-not-in-body"
    if re.search(r"(?m)^[\s>*_\-#\d.)\[]*@" + re.escape(handle) + r"\b", low):
        return True, "addressed-line-start"
    if re.search(r"@" + re.escape(handle) + r"\s*[?:]", low):
        return True, "addressed-punctuation"
    cue = _ASK_CUES.search(body)
    if cue:
        return True, f"ask-cue:{cue.group(1).lower()}"
    return False, "attribution-only"


def _record_mention_gate(agent_name: str, event_type: str, task_id: str,
                         fields: dict, wake: bool, reason: str) -> None:
    """Append every gate decision to a metrics jsonl.

    A gate nobody measures is indistinguishable from a gate that drops real asks
    ([[learnings_text_optin_gate_is_100pct_blind_measure_it]]). This file is the
    denominator: skip-rate, and — by re-reading the preserved bodies — the
    false-negative audit.
    """
    try:
        _MENTION_GATE_METRICS.parent.mkdir(parents=True, exist_ok=True)
        with open(_MENTION_GATE_METRICS, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "agent": agent_name,
                "event_type": event_type,
                "task_id": task_id,
                "author_kind": fields.get("author_kind", ""),
                "author_name": fields.get("author_name", ""),
                "wake": wake,
                "reason": reason,
                "body": (fields.get("comment_preview") or "")[:600],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass  # never let telemetry break a wake


def _routing_verdict(agent_name: str, assignee_name: str,
                     mention_context: dict = None) -> str:
    """Decide how a card addressed to `assignee_name` may enter `agent_name`'s lane.

    Returns one of:
      "own"          — the card is ours; spawn unchanged (control path).
      "block"        — someone else's card arriving on an ASSIGNMENT-shaped feed
                       (task.assigned / task.created / stale-redispatch /
                       pull-on-reap). Fail CLOSED: not spawning is strictly safer
                       than spawning a session with the wrong credential and the
                       wrong workspace onto another agent's in-flight branch.
      "foreign_wake" — someone else's card, but we got here via an @-mention or
                       comment wake. The wake path must STAY OPEN (invariant from
                       #cd53e9aa / incident E1 #8594d87d: an @agent ping on another
                       agent's thread is the fleet's normal handoff channel — Atlas
                       routing work to a lead is exactly this shape). We spawn, but
                       the prompt is reframed: no checkout order, no "task assigned"
                       framing, explicit "this card is not yours".

    Fails OPEN ("own") whenever the assignee is unknown/unresolvable — a Mesh API
    blip must never wedge the fleet.
    """
    if not ROUTING_GATE_ENABLED:
        return "own"
    if not assignee_name or not agent_name:
        return "own"
    if assignee_name.strip().lower() == agent_name.strip().lower():
        return "own"
    return "foreign_wake" if mention_context else "block"


# --- Umbrella/captain dedup safety-net (task a5c444b4) -------------------------
# The primary dedup gate is creator-side: run `mesh-dedup-check.py` BEFORE
# create_task (CLAUDE-workflow.md §4a). This is the backstop for dupes that slip
# through: on task.created for a captain/umbrella task, fire the same checker in
# --warn-comment mode. It posts ONE idempotent "possible duplicate" comment if a
# >=0.80 title sibling exists. Fully fire-and-forget — never blocks/breaks the
# SSE loop (detached subprocess, in-memory once-guard).
_DEDUP_BIN = os.path.expanduser("~/bin/mesh-dedup-check.py")
_DEDUP_WARNED: set = set()          # task_ids already handled this session
_DELEGATION_REVIEW_WARNED: set = set()  # task_ids already warned (B1.4)
_DEDUP_LABELS = {"captain", "umbrella"}


# --- Deploy-verify gate: "merged != done" backstop (task 50540452) ------------
# The primary rule is authoring-side: a ship/deploy task is not Done until the
# change is live in prod and verified on a real endpoint (CLAUDE-workflow.md
# §1f). This is the Done-side backstop: on task.status_changed for a
# deploy-titled task, fire `mesh-deploy-verify-check.py --task-id --warn-comment`
# detached. The checker self-confirms the task is actually `done`, is a real ship
# task, and has NO prod-verification marker in its comments — only then does it
# post ONE idempotent "⚠️ DEPLOY-UNVERIFIED" comment. A non-done status change
# (todo→in_progress) just makes the checker exit 0. Idempotency is the checker's
# `already_warned`, so no dispatcher once-guard is needed (and the real
# move-to-done always fires). Label-only ship tasks without a deploy verb in the
# title are caught by the periodic `--sweep` (Orbit /sweep), not here.
# Fully fire-and-forget — never blocks/breaks the SSE loop.
_DEPLOY_VERIFY_BIN = os.path.expanduser("~/bin/mesh-deploy-verify-check.py")
_DEPLOY_VERIFY_ENABLED = os.environ.get("DEPLOY_VERIFY_GATE_ENABLED", "1") != "0"
# A ship/deploy task = one of these labels (the deliberate, precise signal) …
_DEPLOY_LABELS = {"deploy", "ship", "release", "rollout"}
# … or a deploy-SPECIFIC verb in the title (NOT bare ship/release — those
# collide with "ship doc" / "release_task"). Kept as pattern STRINGS — `re` is
# imported locally here (module-level `re` is deliberately avoided, ~line 755).
_DEPLOY_TITLE_PAT = (
    r"\b(deploy|deployment|redeploy|rollout|roll out)\b"
    r"|деплой|задеплои|выкат|раскат"
)
# Pure process / doc tasks are never ship tasks even if they mention deploy.
_DEPLOY_EXCLUDE_LABELS = {"process", "workflow", "docs", "documentation",
                          "research", "spec", "design", "triage"}
_DEPLOY_DOC_VETO_PAT = (
    r"\b(design|research|spec|adr)\b[^\n]*\bdoc\b|\bdoc:|\.md\b|dev-docs/|runbook"
)


def _is_ship_task(labels: list, title: str) -> bool:
    """Mirror of mesh-deploy-verify-check.is_ship_task — cheap inline gate so we
    only spawn the checker for plausible ship tasks."""
    import re
    lset = {str(l).lower() for l in (labels or [])}
    if lset & _DEPLOY_EXCLUDE_LABELS:
        return False
    if re.search(_DEPLOY_DOC_VETO_PAT, title or "", re.IGNORECASE):
        return False
    if lset & _DEPLOY_LABELS:
        return True
    return bool(re.search(_DEPLOY_TITLE_PAT, title or "", re.IGNORECASE))


def _deploy_verify_on_status(agent_name: str, agent_key: str, api_url: str,
                             task_id: str) -> None:
    """Best-effort: when a task changes status, decide cheaply whether it's a
    ship/deploy task (labels+title — status_changed events carry NO title, so we
    resolve it via one API read) and, if so, launch the deploy-verify checker
    detached. The checker self-confirms the task is actually `done` and lacks a
    prod-verification marker before posting a one-time warning, and is idempotent
    — so firing on a non-done transition just makes it exit 0. Never raises,
    never blocks the SSE loop beyond the single metadata read."""
    try:
        if not _DEPLOY_VERIFY_ENABLED or not task_id:
            return
        if not os.path.exists(_DEPLOY_VERIFY_BIN):
            return
        labels, title, _, _, _ = fetch_task_meta(agent_key, task_id, api_url)
        if not _is_ship_task(labels, title):
            return
        subprocess.Popen(
            [sys.executable, _DEPLOY_VERIFY_BIN, "--task-id", task_id,
             "--warn-comment", "--quiet",
             "--agent-key", agent_key, "--api-url", api_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log(agent_name, f"deploy-verify: launched for ship task {task_id[:8]} «{title[:48]}»")
    except Exception as e:
        log(agent_name, f"deploy-verify: skipped ({e})")


def _dedup_warn_on_create(agent_name: str, agent_key: str, api_url: str,
                          task_id: str, labels: list, title: str) -> None:
    """Best-effort: if a newly-created task is captain/umbrella-scoped, launch the
    dedup checker detached to post a one-time warning when a near-dup exists."""
    try:
        if not task_id or task_id in _DEDUP_WARNED:
            return
        lbls = {str(l).lower() for l in (labels or [])}
        tl = (title or "").lower()
        is_umbrella = bool(lbls & _DEDUP_LABELS) or "umbrella" in tl or "captain" in tl
        if not is_umbrella:
            return
        if not os.path.exists(_DEDUP_BIN):
            return
        _DEDUP_WARNED.add(task_id)
        subprocess.Popen(
            [sys.executable, _DEDUP_BIN, "--task-id", task_id,
             "--warn-comment", "--quiet",
             "--agent-key", agent_key, "--api-url", api_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log(agent_name, f"dedup-warn: launched for captain/umbrella task {task_id[:8]}")
    except Exception as e:
        log(agent_name, f"dedup-warn: skipped ({e})")


def _warn_delegation_review_trap(agent_name: str, agent_key: str, api_url: str,
                                 task_id: str) -> None:
    """B1.4: best-effort one-time comment when an agent-assigned task arrives with
    status=review. That task will NEVER be retried by stale-redispatch
    (_SKIP_CATEGORIES includes review). Posting a warning so the creator can move
    it to todo. Never raises, fire-and-forget safe.
    """
    if not task_id or task_id in _DELEGATION_REVIEW_WARNED:
        return
    try:
        req = Request(f"{api_url}/api/v1/tasks/{task_id}",
                      headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=10) as r:
            task_data = json.loads(r.read())
        task = task_data.get("task") or task_data
        pid = task.get("project_id")
        status_id = task.get("status_id")
        if not pid or not status_id:
            return
        # Look up status category in the project's status set.
        req2 = Request(f"{api_url}/api/v1/projects/{pid}/statuses",
                       headers={"X-Agent-Key": agent_key})
        with urlopen(req2, timeout=10) as r:
            statuses = json.loads(r.read())
        cat = None
        for s in (statuses or []):
            if s.get("id") == status_id:
                cat = s.get("category") or s.get("type")
                break
        if cat != "review":
            return
        _DELEGATION_REVIEW_WARNED.add(task_id)
        log(agent_name,
            f"DELEGATION-REVIEW-TRAP: {task_id[:8]} assigned to {agent_name} "
            "but status=review — stale-redispatch will never retry (§0a violation)")
        body = (
            "⚠️ **Dispatcher — delegation=review trap** (B1.4)\n\n"
            f"Задача назначена агенту **{agent_name}**, но имеет статус `review`. "
            "Диспетчер фильтрует `review` через `_SKIP_CATEGORIES` — "
            "**stale-redispatch больше не перезапустит** агента на этой задаче, "
            "если первый SSE-спавн пройдёт мимо (reposync abort, rate-limit, краш).\n\n"
            "**Причина:** `delegation_level=review` задаёт *поведение агента после работы* "
            "(переместить в `review` когда готово), но **не начальный статус**. "
            "Исполняемые задачи стартуют с `status=todo` (§0a CLAUDE-workflow.md).\n\n"
            "**Действие:** переведите задачу в `todo`, чтобы диспетчер подхватил.\n\n"
            "_Auto-generated by mesh-dispatcher delegation=review-trap guard (B1.4)._"
        )
        payload = json.dumps({"body": body}).encode()
        req3 = Request(
            f"{api_url}/api/v1/tasks/{task_id}/comments",
            data=payload,
            headers={"Content-Type": "application/json", "X-Agent-Key": agent_key},
            method="POST",
        )
        with urlopen(req3, timeout=10) as r:
            r.read()
        log(agent_name,
            f"DELEGATION-REVIEW-TRAP: warning comment posted on {task_id[:8]}")
    except Exception as e:
        log(agent_name, f"delegation-review-trap check error: {e}")


def _recall_graph_context(agent_name: str, agent_key: str, api_url: str,
                          task_id: str, title: str):
    """G1-s3: build a RELATED MEMORY CONTEXT block via the 2-hop graph-recall
    endpoint, for injection into a captain/parent task's spawn prompt.

    Async-safe by construction:
      - hard per-call timeout (RECALL_GRAPH_TIMEOUT),
      - on graph failure → fall back to flat recall (/memories/search),
      - on any further failure → return None (caller skips injection, spawn
        proceeds unchanged).
    Never raises. Returns a prompt-appendable string, or None.
    """
    if not title:
        return None
    from urllib.parse import quote
    base = api_url.rstrip("/")
    q = quote(title[:200])

    def _get_json(url, timeout):
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    items = None
    source = None
    # 1. Graph recall (2-hop). param is `query`; task_id discriminates the cache.
    try:
        url = (f"{base}/api/v1/memories/recall_graph?query={q}"
               f"&hops={RECALL_GRAPH_HOPS}&task_id={task_id}")
        data = _get_json(url, RECALL_GRAPH_TIMEOUT) or {}
        items = data.get("items") or []
        source = "graph, 2-hop"
    except Exception as e:
        log(agent_name,
            f"recall_graph: graph call failed ({e}); falling back to flat recall")
    # 2. Fallback: flat recall via /memories/search (param is `q`).
    if items is None:
        try:
            url = f"{base}/api/v1/memories/search?q={q}&limit={RECALL_GRAPH_MAX_ITEMS}"
            data = _get_json(url, RECALL_GRAPH_TIMEOUT) or {}
            items = data.get("items") or []
            source = "flat recall (graph unavailable)"
        except Exception as e:
            log(agent_name,
                f"recall_graph: flat-recall fallback also failed ({e}); skip injection")
            return None
    if not items:
        return None
    # 3. Format top-N hits into a compact, clearly-labelled context block.
    lines = []
    for it in items[:RECALL_GRAPH_MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        content = (it.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        if len(content) > 280:
            content = content[:277] + "..."
        meta = []
        hop = it.get("hop_distance")
        if isinstance(hop, int):
            meta.append(f"hop{hop}")
        prov = it.get("provenance")
        if isinstance(prov, dict) and prov.get("via"):
            meta.append(str(prov.get("via")))
        elif isinstance(prov, str) and prov:
            meta.append(prov)
        tag = f" [{', '.join(meta)}]" if meta else ""
        lines.append(f"- {content}{tag}")
    if not lines:
        return None
    return (
        f"\n\n📚 RELATED MEMORY CONTEXT ({source}) — surfaced automatically "
        f"because this is a captain/parent task. Prior decisions/learnings "
        f"related to «{title[:120]}». Use as background; verify before acting "
        f"on anything that may be stale:\n" + "\n".join(lines)
    )


def _record_memory_read(agent_name: str, layer: str, op: str, extra: dict = None):
    """Append a per-agent dispatcher-side memory read to the sidecar JSONL.

    collect-memory-ops.py folds these into the per-agent/per-layer read counts so
    a deterministic spawn-time read actually moves the R:W metric (the transcript
    scan structurally cannot see a daemon read). Never raises.
    """
    try:
        from datetime import timezone as _tz
        MEMORY_READS_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agent": (agent_name or "").lower(),
            "layer": layer,
            "op": op,
        }
        if extra:
            rec.update(extra)
        with open(MEMORY_READS_SIDECAR, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never break a spawn
        pass


def _mempalace_prefetch(agent_name: str, task_title: str):
    """Memory-eval A3-followup (e8344446): build a 🏛️ MEMPALACE CONTEXT block by
    reading the agent's OWN wing directly from chroma.sqlite3 (READ-ONLY), and
    record the read to the sidecar so it counts toward the mempalace R:W metric.

    Two cheap, model-free metadata queries (no embeddings → fast, deterministic):
      A. latest N `sessions` drawers for wing=<agent>  → handoff snapshot
      B. latest N wing drawers whose text matches task-title keywords → topic ctx

    Async-safe by construction: opens the DB read-only with a bounded busy timeout,
    never writes (no contention with the live mempalace MCP), never raises. Returns
    a prompt-appendable string, or None (caller leaves the spawn unchanged).
    """
    wing = (agent_name or "").lower()
    if not wing:
        return None
    if not MEMPALACE_CHROMA_DB.exists():
        return None
    import sqlite3
    import re as _re
    rows_sessions, rows_topic = [], []
    seen_ids = set()
    try:
        # Read-only (mode=ro) + immutable-free so we still see committed WAL data;
        # busy_timeout bounds any lock wait. Reads never block sqlite writers.
        uri = f"file:{MEMPALACE_CHROMA_DB}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=MEMPALACE_PREFETCH_TIMEOUT)
        con.execute(f"PRAGMA busy_timeout={int(MEMPALACE_PREFETCH_TIMEOUT*1000)}")
        cur = con.cursor()

        def _q(extra_join, extra_where, params, limit):
            sql = (
                "SELECT c0.id, fa.string_value, COALESCE(rm.string_value,''), "
                "       d.string_value "
                "FROM embedding_metadata c0 "
                "JOIN embedding_metadata w  ON w.id=c0.id  AND w.key='wing' "
                "       AND w.string_value=? "
                "JOIN embedding_metadata fa ON fa.id=c0.id AND fa.key='filed_at' "
                "JOIN embedding_metadata d  ON d.id=c0.id  AND d.key='chroma:document' "
                "LEFT JOIN embedding_metadata rm ON rm.id=c0.id AND rm.key='room' "
                + extra_join +
                "WHERE c0.key='chunk_index' AND c0.int_value=0 " + extra_where +
                "ORDER BY fa.string_value DESC LIMIT ?"
            )
            cur.execute(sql, (wing, *params, limit))
            return cur.fetchall()

        # A. handoff snapshot — latest `sessions` drawers
        rows_sessions = _q(
            "JOIN embedding_metadata rs ON rs.id=c0.id AND rs.key='room' "
            "       AND rs.string_value='sessions' ",
            "", (), MEMPALACE_PREFETCH_SESSIONS)
        for r in rows_sessions:
            seen_ids.add(r[0])

        # B. topic context — wing drawers whose text LIKE any significant keyword
        kws = [w for w in _re.findall(r"[A-Za-zА-Яа-я0-9]{4,}", task_title or "")][:6]
        if kws:
            like_clause = " OR ".join(["d.string_value LIKE ?"] * len(kws))
            params = tuple(f"%{k}%" for k in kws)
            cand = _q("", f"AND ({like_clause}) ", params,
                      MEMPALACE_PREFETCH_TOPIC + MEMPALACE_PREFETCH_SESSIONS)
            for r in cand:
                if r[0] not in seen_ids:
                    rows_topic.append(r)
                    seen_ids.add(r[0])
                if len(rows_topic) >= MEMPALACE_PREFETCH_TOPIC:
                    break
        con.close()
    except Exception as e:  # noqa: BLE001
        log(agent_name, f"mempalace_prefetch: read failed ({e}); skip injection")
        return None

    def _fmt(row, tag):
        text = (row[3] or "").strip().replace("\n", " ")
        if not text:
            return None
        if len(text) > 300:
            text = text[:297] + "..."
        room = row[2] or "?"
        return f"- [{tag}/{room}] {text}"

    lines = []
    for r in rows_sessions:
        ln = _fmt(r, "handoff")
        if ln:
            lines.append(ln)
    for r in rows_topic:
        ln = _fmt(r, "topic")
        if ln:
            lines.append(ln)
    if not lines:
        # Still record the read attempt (a real DB query ran) so new/empty wings
        # are visibly covered, but don't inject an empty block.
        _record_memory_read(agent_name, "mempalace", "prefetch_search",
                            {"wing": wing, "hits": 0})
        return None

    _record_memory_read(agent_name, "mempalace", "prefetch_search",
                        {"wing": wing, "hits": len(lines),
                         "sessions": len(rows_sessions), "topic": len(rows_topic)})
    return (
        f"\n\n🏛️ MEMPALACE CONTEXT (wing={wing}) — your own prior memory, surfaced "
        f"automatically at spawn so you don't re-derive it. Latest handoff state + "
        f"drawers related to «{(task_title or '')[:100]}». Use as background; "
        f"verify before acting on anything that may be stale:\n" + "\n".join(lines)
    )


def _needs_splitter(description: str) -> bool:
    """Heuristic: return True if description matches the multi-deliverable auto-split threshold.

    Thresholds (any one triggers):
    - word count > 300
    - ## section headers > 3

    Intentionally conservative — false positives are cheap (agent reads task, skips if prior
    verdict exists); false negatives mean a missed split opportunity.
    """
    if not description or len(description) < 100:
        return False
    if len(description.split()) > 300:
        return True
    header_count = sum(1 for line in description.splitlines() if line.startswith("## "))
    return header_count > 3


def pick_model(default_model: str, labels: list) -> tuple:
    """Pick model based on labels. Returns (model, reason)."""
    # 1. Explicit override: `model:opus` / `model:sonnet`
    for l in labels:
        if l.startswith("model:"):
            return l.split(":", 1)[1], f"explicit label `{l}`"
    # 2. Phase-driven selection
    for l in labels:
        if l.startswith("phase:"):
            phase = l.split(":", 1)[1]
            if phase in PHASE_MODEL:
                return PHASE_MODEL[phase], f"phase `{phase}`"
    # 3. Default from agent config
    return default_model, "default"


def _git(repo: str, *args, timeout: int = 120) -> tuple:
    """Run `git -C repo <args>`. Returns (rc, stdout, stderr) — never raises."""
    try:
        p = subprocess.run(["git", "-C", repo, *args],
                            capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:  # timeout, git missing, etc.
        return 255, "", f"{type(e).__name__}: {e}"


def resolve_repos(agent: dict) -> list:
    """Repos the dispatcher must keep fresh before launching this agent.

    Source of truth = `repos` in mesh-agents.json (explicit, reviewable —
    NOT parsed from CLAUDE.md prose, which is the very thing agents fail to
    execute reliably). Fallback: the workspace itself, only if it is a git
    repo (most agent workspaces are non-repo home dirs and need no sync).
    """
    repos = agent.get("repos")
    if repos:
        return list(repos)
    ws = agent.get("workspace", "")
    # .git may be a directory (full clone) or a file (linked worktree) — accept both
    if ws and os.path.exists(os.path.join(ws, ".git")):
        return [ws]
    return []


def safe_sync_repo(repo: str) -> tuple:
    """Bring repo's current branch up to its upstream WITHOUT clobbering.

    Returns (ok, summary). ok=False => caller MUST abort the launch and
    escalate to a human. We never force-push, never reset --hard, never
    discard local work. Failure-safe: anything ambiguous => not ok.
    """
    # .git is a directory for full clones, a file for linked worktrees — accept both
    if not os.path.exists(os.path.join(repo, ".git")):
        return False, f"{repo}: configured but not a git repo (missing/invalid)"

    # 1. fetch (one retry — a transient network blip shouldn't bounce a task)
    rc, _, err = _git(repo, "fetch", "--all", "--prune", timeout=180)
    if rc != 0:
        time.sleep(3)
        rc, _, err = _git(repo, "fetch", "--all", "--prune", timeout=180)
        if rc != 0:
            return False, f"{repo}: git fetch failed twice — can't verify freshness ({err[:160]})"

    # 2. current branch + its upstream
    rc, branch, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or not branch or branch == "HEAD":
        # B1.3: skip sync instead of aborting. Detached HEAD is not a clobber risk —
        # there's no local branch to diverge from. Returning False here triggers an
        # ABORT+alert+return-to-todo loop that traps rarely-spawned agents (workspace
        # arrives detached or becomes detached transiently). Agent handles re-attach
        # via §1a (git checkout <default-branch> + pull) on its own startup.
        return True, f"{repo}: detached HEAD — skipping sync (agent re-attaches via §1a)"
    rc, upstream, _ = _git(repo, "rev-parse", "--abbrev-ref",
                           "--symbolic-full-name", "@{u}")
    if rc != 0 or not upstream:
        # local-only branch: nothing on the remote to be stale against
        return True, f"{repo}@{branch}: no upstream (local-only) — fetched, nothing to sync"

    # 3. ahead/behind vs upstream
    rc, lr, _ = _git(repo, "rev-list", "--left-right", "--count",
                     f"{upstream}...HEAD")
    parts = lr.replace("\t", " ").split()
    if rc != 0 or len(parts) != 2:
        return False, f"{repo}@{branch}: cannot compute ahead/behind vs {upstream}"
    behind, ahead = int(parts[0]), int(parts[1])

    # 4. dirty? Tracked-only — untracked files don't conflict with ff/rebase
    # and agents legitimately keep scratch files in the repo dir (e.g.
    # evc-mesh has .golangci.bck.yml, mempalace.yaml). Including them here
    # would falsely BLOCK every dispatch whenever the agent is behind.
    _, st, _ = _git(repo, "status", "--porcelain", "--untracked-files=no")
    dirty = bool(st.strip())
    nfiles = len(st.splitlines()) if dirty else 0

    # 5. decide — only the "remote moved ahead" cases are clobber-risky
    if behind == 0:
        # up-to-date, or only local-ahead (agent's own unpushed work).
        # A dirty tree here is the agent's WIP, not a staleness risk.
        note = "up-to-date" if ahead == 0 else f"{ahead} local-ahead (unpushed)"
        return True, f"{repo}@{branch}: {note}{' +dirty-WIP' if dirty else ''} — OK"

    if dirty:
        # Auto-recovery (task bfa8e55a). The old behaviour refused here →
        # dead loop: an ephemeral agent left tracked changes on a branch that
        # has since moved BEHIND upstream, so every subsequent spawn saw the
        # same dirty+behind tree → refuse → respawn-churn, and the operator got a TG
        # alert each cycle. Instead recover non-destructively: stash the WIP,
        # bring the branch to upstream, then restore. Work is NEVER lost — worst
        # case it stays in the stash (recover via `git stash list`).
        # status used --untracked-files=no, matching `git stash`'s default
        # (tracked modified/staged only; untracked scratch files left in place).
        rc, _, serr = _git(repo, "stash", "push", "-m",
                           f"dispatcher-auto-recovery {branch}")
        if rc != 0:
            return False, (f"{repo}@{branch}: BEHIND {behind} + DIRTY ({nfiles}); "
                           f"git stash failed ({serr[:120]}) — refuse, human needed")
        if ahead == 0:
            rc, _, err = _git(repo, "merge", "--ff-only", upstream)
            synced = f"fast-forwarded +{behind}"
        else:
            rc, _, err = _git(repo, "rebase", upstream, timeout=180)
            if rc != 0:
                _git(repo, "rebase", "--abort")
            synced = f"rebased {ahead} local commit(s) onto {upstream} (+{behind})"
        if rc != 0:
            # Could not sync even on a clean tree (e.g. unrebasable divergence).
            # Restore the WIP and bail — the tree returns to its prior state.
            _git(repo, "stash", "pop")
            return False, (f"{repo}@{branch}: BEHIND {behind} + DIRTY; sync after "
                           f"stash failed ({err[:120]}) — WIP restored, human needed")
        rc, _, perr = _git(repo, "stash", "pop")
        if rc != 0:
            # Pop conflicted: git retains the stash on conflict, so the WIP is
            # safe. Hard-reset the half-applied tree back to the freshened HEAD
            # so the agent starts clean (content lives in the stash, recoverable
            # via `git stash list` / `git stash show -p`).
            _git(repo, "reset", "--hard", "HEAD")
            return True, (f"{repo}@{branch}: BEHIND+DIRTY auto-recovered — {synced}; "
                          f"WIP stash-pop CONFLICTED → kept in stash (recover: "
                          f"git stash list), proceeding on clean synced tree")
        return True, (f"{repo}@{branch}: BEHIND+DIRTY auto-recovered — stashed WIP, "
                      f"{synced}, popped clean — OK")

    if ahead == 0:
        # clean, behind only -> fast-forward to remote
        rc, _, err = _git(repo, "merge", "--ff-only", upstream)
        if rc != 0:
            return False, f"{repo}@{branch}: ff-only merge failed ({err[:160]})"
        return True, f"{repo}@{branch}: fast-forwarded +{behind} from {upstream} — OK"

    # diverged, clean tree -> rebase local commits onto remote (reconcile,
    # never overwrite). Abort the rebase on conflict so the tree stays clean.
    rc, _, err = _git(repo, "rebase", upstream, timeout=180)
    if rc != 0:
        _git(repo, "rebase", "--abort")
        return False, (f"{repo}@{branch}: DIVERGED (behind {behind}, ahead {ahead}); "
                       f"rebase onto {upstream} failed — refuse to clobber, human needed")
    return True, (f"{repo}@{branch}: rebased {ahead} local commit(s) onto "
                  f"{upstream} (+{behind}) — OK")


def alert_pavel(text: str):
    """Escalate to the operator via the always-on Orbit Telegram bridge."""
    try:
        subprocess.run(
            [TG_REPLY, PAVEL_CHAT_ID],
            input=text, text=True,
            env={**os.environ, "TELEGRAM_OUTBOX": RIKER_OUTBOX},
            timeout=20, check=False,
        )
    except Exception as e:
        log("dispatcher", f"alert_pavel failed: {e}")


def add_task_comment(api_url: str, agent_key: str, task_id: str, body: str,
                     internal: bool = True):
    """Best-effort: post a comment on a task (default internal/agent-only).

    Used to record reposync aborts on the task itself instead of paging the operator
    in Telegram (task bfa8e55a). Never raises — a failed comment must not block
    or crash a dispatch cycle.
    """
    try:
        payload = json.dumps({"body": body, "is_internal": internal}).encode()
        req = Request(f"{api_url}/api/v1/tasks/{task_id}/comments", data=payload,
                      method="POST",
                      headers={"X-Agent-Key": agent_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        log("dispatcher", f"add_task_comment failed for {task_id}: {e}")


def return_task_to_todo(api_url: str, agent_key: str, task_id: str):
    """Best-effort: move an aborted task back to its project's todo column."""
    try:
        req = Request(f"{api_url}/api/v1/tasks/{task_id}",
                      headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=15) as r:
            task = json.loads(r.read())
        pid, cur = task.get("project_id"), task.get("status_id")
        if not pid:
            return
        req = Request(f"{api_url}/api/v1/projects/{pid}/statuses",
                      headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=15) as r:
            sd = json.loads(r.read())
        statuses = sd if isinstance(sd, list) else sd.get("statuses", sd.get("items", []))
        todo = next((s for s in statuses
                     if (s.get("category") or s.get("type")) == "todo"), None)
        if not todo or todo.get("id") == cur:
            return
        if _dispatch_move(api_url, agent_key, task_id, todo["id"], "dispatcher"):
            log("dispatcher", f"task {task_id} returned to todo")
    except Exception as e:
        log("dispatcher", f"return_task_to_todo failed: {e}")


def ensure_repos_fresh(agent_name: str, repos: list, api_url: str,
                       agent_key: str, task_id: str, task_title: str) -> bool:
    """Gate before launching an agent. True => safe to launch.

    False => one or more repos couldn't be brought to remote without risk:
    we abort the launch, return the task to todo, and alert the operator. Never
    clobber, never work on a stale tree.
    """
    blocked, ok_lines = [], []
    for repo in repos:
        ok, summary = safe_sync_repo(repo)
        log(agent_name, f"reposync: {summary}")
        (ok_lines if ok else blocked).append(summary)
    if not blocked:
        if ok_lines:
            log(agent_name, "reposync OK — all repos fresh")
        return True
    log(agent_name, f"ABORT launch for task {task_id}: {len(blocked)} repo(s) unsafe")
    # Mark for fast retry + track CONSECUTIVE unrecoverable aborts (task bfa8e55a).
    # With BEHIND+DIRTY now auto-recovering in safe_sync_repo, reaching this path
    # means a genuinely unrecoverable state (fetch failed twice, unrebasable
    # divergence, stash failed). Don't page the operator in TG every cycle — that was
    # pure operational noise he can't act on per-cycle: log it, post an internal
    # task comment (auditable, Orbit-visible), and escalate to the operator ONLY after
    # REPO_UNSAFE_ALERT_AFTER consecutive aborts on the same task. Race-with-
    # another-agent (the common transient cause) clears on the next ~10-min retry
    # and never reaches the escalation threshold.
    with _REPO_UNSAFE_LOCK:
        _REPO_UNSAFE[task_id] = time.monotonic()
        _REPO_UNSAFE_COUNT[task_id] = _REPO_UNSAFE_COUNT.get(task_id, 0) + 1
        attempts = _REPO_UNSAFE_COUNT[task_id]
    return_task_to_todo(api_url, agent_key, task_id)
    detail = "\n".join(f"• {b}" for b in blocked)
    retry_min = REPO_UNSAFE_RETRY_SEC // 60
    # Internal task comment (best-effort) — keeps the abort auditable on the task
    # without paging the operator.
    add_task_comment(
        api_url, agent_key, task_id,
        f"⚠️ dispatcher reposync abort #{attempts} for {agent_name} — repo not "
        f"safely at remote:\n{detail}\n\nTask returned to todo; auto-retry in "
        f"~{retry_min} min. Escalates to the operator only after "
        f"{REPO_UNSAFE_ALERT_AFTER} consecutive aborts.",
        internal=True,
    )
    if attempts < REPO_UNSAFE_ALERT_AFTER:
        log(agent_name,
            f"reposync abort {attempts}/{REPO_UNSAFE_ALERT_AFTER} for {task_id} — "
            f"logged + task-commented, NOT escalating to the operator yet")
        return False
    # Persistent unrecoverable state — now page the operator.
    msg = (
        f"⚠️ mesh-dispatcher: задача застряла на reposync ({attempts}× подряд)\n"
        f"{agent_name} · «{task_title}» ({task_id})\n\n"
        f"Репозиторий не приведён к remote безопасно (unrecoverable):\n"
        f"{detail}\n\n"
        f"Авто-ретрай каждые ~{retry_min} мин не помог {attempts} раз. Задача "
        f"возвращена в todo. Нужна твоя проверка дерева (commit / stash / push / "
        f"rebase) и переназначь задачу."
    )
    alert_pavel(msg)
    return False


def _is_paused(agent_name: str) -> tuple:
    """Check pause flags. Returns (paused: bool, reason: str).

    Defensive: agents in EXEMPT_AGENTS (Aux1, Aux2) are never paused
    even when PAUSE_ALL is set (the operator rule 2026-05-26).
    """
    if agent_name in EXEMPT_AGENTS:
        return False, ""
    if PAUSE_GLOBAL_FILE.exists():
        return True, "global PAUSE_ALL flag set"
    per_agent = PAUSE_DIR / f"PAUSE_{agent_name}"
    if per_agent.exists():
        return True, f"per-agent PAUSE_{agent_name} flag set"
    return False, ""


def _crash_retry_delay(crash_count: int) -> int:
    """Exponential backoff: 2m, 5m, 15m, 1h, 4h. count starts at 1 (first crash)."""
    idx = min(crash_count - 1, len(CRASH_RETRY_BACKOFF_SEC) - 1)
    return CRASH_RETRY_BACKOFF_SEC[max(0, idx)]


def _build_hermes_prompt(task_id: str, task_title: str, task_desc: str) -> str:
    """Lean spawn prompt for a comet-runtime agent (e.g. Lumen, task ae7efdd0).

    Comet runs DeepSeek (mechanic tier) with a trimmed evc-mesh + data MCP set.
    The big Claude-Code spawn prompt (triage / checkout / wake-up memory / pre-done
    gate) references tools and conventions a DeepSeek agent doesn't reliably have,
    so the comet branch gets its own short, task-shaped contract instead — the
    "Контракт спавна Lumen" from the task: analyse via its MCP, comment the result,
    move the task to review, closing the loop with its own evc-mesh key.
    """
    desc = (task_desc or "").strip()
    desc_block = f"\n\n{desc}" if desc else ""
    return (
        f"Mesh-задача {task_id}: {task_title}{desc_block}\n\n"
        "Ты — Lumen, аналитик-механик (SEO / product-analytics). Возьми эту задачу "
        "в работу через свой evc-mesh MCP:\n"
        f"1. checkout_task(task_id='{task_id}') — если занято другим, оставь короткий "
        "комментарий и выйди.\n"
        f"2. move_task(task_id='{task_id}', status_slug='in_progress').\n"
        "3. Сделай анализ по сути задачи своими инструментами (PostHog / SEO / "
        "DataForSEO — что применимо). Опирайся на цифры, без воды.\n"
        f"4. add_comment(task_id='{task_id}', body=...) — положи результат "
        "(вывод + ключевые метрики) комментарием в задачу.\n"
        f"5. move_task(task_id='{task_id}', status_slug='review') — закрой петлю.\n"
        "Всё делаешь сам, своим evc-mesh MCP. Не выдумывай данные — если инструмент "
        "не дал цифр, скажи об этом прямо в комментарии."
    )


def _build_runtime_spawn(runtime: str, agent_name: str, agent_cfg: dict,
                         task_id: str, task_title: str, task_desc: str,
                         env_file: str = None):
    """Build (cmd, spawn_env) for a NON-claude runtime (task ae7efdd0).

    Returns (None, None) for an unknown runtime so the caller skips the spawn
    safely. Currently supports `comet` (Nous Comet Agent runtime, DeepSeek
    mechanic tier — e.g. Lumen).

    The comet profile carries its OWN Mesh key + MCP config
    (~/.comet/profiles/<profile>/config.yaml), so this branch does NOT inject
    the claude-only env (MESH_AGENT_KEY, claude_env_file, TELEGRAM_STATE_DIR,
    workflows flag). Profile is selected with `-p <profile>` (NOT --profile),
    `-z <prompt>` is the one-shot agentic run, `--yolo` auto-approves tool calls
    so a headless session can call evc-mesh/posthog without an interactive gate.
    """
    if runtime != "comet":
        return None, None
    profile = agent_cfg.get("hermes_profile") or agent_name.lower()
    prompt = _build_hermes_prompt(task_id, task_title, task_desc)
    cmd = [
        HERMES_BIN,
        "-p", profile,
        "--yolo",
        "-z", prompt,
    ]
    spawn_env = dict(ENV)
    # ~/.local/bin holds the comet wrapper + its bootstrapped deps (node, rg,
    # ffmpeg); ensure it's on PATH for anything comet shells out to.
    spawn_env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + spawn_env.get("PATH", "")
    # Optional secrets (OPENROUTER_API_KEY, DATAFORSEO_*, POSTHOG_*) via the
    # agent's env_file — comet also loads ~/.comet/.env itself, but a launchd
    # dispatcher has a minimal env, so pass the configured file through if set.
    if env_file:
        extra = _load_env_file(env_file)
        if extra:
            spawn_env.update(extra)
            log(agent_name, f"[comet] loaded {len(extra)} env vars from {env_file}")
        else:
            log(agent_name, f"[comet] WARN: env_file {env_file} empty or unreadable")
    return cmd, spawn_env


def dispatch_claude(*args, budget_token: str = None, **kwargs):
    """Refund-safe entry point. Every caller goes through here.

    `_dispatch_claude_impl` has NINE early returns that abort before
    `subprocess.Popen()` — concurrency cap, per-agent cap, pause, unknown
    runtime, missing config, … Each one means "the agent never actually ran",
    which under B3·dq must not count toward the card's give-up ladder.

    Patching all nine to refund by hand is how the tenth gets missed, so the
    accounting is inverted instead: the impl returns True only on the line after
    `register_pid`, and anything else — including a `return` someone adds later,
    and including an exception — refunds here. New abort paths are covered by
    construction rather than by remembering.
    """
    spawned = False
    try:
        spawned = bool(_dispatch_claude_impl(*args, **kwargs))
        return spawned
    finally:
        if budget_token:
            if spawned:
                _respawn_budget_settle(budget_token)
            else:
                _respawn_budget_refund(budget_token, "dispatch aborted before spawn")


def _dispatch_claude_impl(agent_name: str, agent_key: str, workspace: str, model: str,
                          task_id: str, task_title: str, api_url: str,
                          env_file: str = None, repos: list = None,
                          mention_context: dict = None, claude_env_file: str = None):
    """Launch a Claude Code (or other runtime) session for a task.

    Returns True IFF a child process was actually started. Callers must not
    invoke this directly — go through `dispatch_claude` so the respawn-budget
    attempt is settled or refunded.

    Runtime-aware (task ae7efdd0): the agent's `runtime` config key selects the
    spawn command — `claude_code` (default) → `claude -p`; `comet` →
    `comet -p <profile> -z <prompt> --yolo`. All gates/dedup/caps are shared.

    mention_context (optional): if event_type was task.mentioned, contains
    {'author_kind': 'user'|'agent', 'author_name': str, 'comment_preview': str}.
    Used to extend the prompt so the agent immediately knows WHY it was woken up.
    """
    # Kill-switch (added 2026-05-22). the operator can /stop globally or per-agent
    # via Telegram bot, dispatcher refuses to spawn until /restart.
    paused, why = _is_paused(agent_name)
    if paused:
        log(agent_name, f"SKIP spawn — {why}")
        unclaim(agent_name, task_id) if task_id else None
        return
    # Human-gate freeze (dispatcher half of audit fix #1). A task waiting on the operator must
    # never be re-fed — see _human_gate_blocks_feed. Placed here, at the single choke
    # point every feed path funnels through (SSE task.assigned/created, stale-redispatch,
    # pull-on-reap), so no caller can bypass it. Mention/comment wakes pass through.
    if _human_gate_blocks_feed(agent_name, agent_key, task_id, api_url, mention_context):
        unclaim(agent_name, task_id) if task_id else None
        return
    # Dependency freeze (#8f0f9ef6) — the SAME choke point, one line below, because it
    # is the same decision: "this card cannot move yet". The difference is who is being
    # waited on and whether the operator gets a second line in his queue. See
    # _dep_freeze_blocks_feed / human_gate.dep_freeze_reason.
    if _dep_freeze_blocks_feed(agent_name, agent_key, task_id, api_url, mention_context):
        unclaim(agent_name, task_id) if task_id else None
        return
    log_file = LOG_DIR / f"{agent_name.lower()}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    # Adaptive model selection by labels (GSD-inspired profiles)
    labels, fetched_title, task_desc, parent_task_id, assignee_name = fetch_task_meta(
        agent_key, task_id, api_url)
    if not task_title and fetched_title:
        log(agent_name, f"title resolved via API: {fetched_title!r}")
        task_title = fetched_title

    # Lane-identity routing gate (task 98a1db69). Placed at the same single choke
    # point as the human-gate: every feed path funnels through dispatch_claude, so
    # no caller can bypass it. See _routing_verdict for why the mention/comment
    # wake path is reframed rather than blocked.
    routing = _routing_verdict(agent_name, assignee_name, mention_context)
    if routing == "block":
        log(agent_name,
            f"routing-gate SKIP spawn — task {task_id} is assigned to "
            f"{assignee_name!r}, this lane is {agent_name!r} (no @-mention wake). "
            f"Not spawning a foreign-credential session.")
        unclaim(agent_name, task_id) if task_id else None
        return
    if routing == "foreign_wake":
        log(agent_name,
            f"routing-gate FOREIGN-WAKE — task {task_id} is assigned to "
            f"{assignee_name!r}, this lane is {agent_name!r}; spawning read-only "
            f"wake (no checkout, no assignment framing).")

    # Flap circuit breaker honor (task d52d7b0f, Watchdog gap 2). If watchdog
    # tripped on (agent_name, task_id), refuse to spawn until TTL expires or
    # the flag is removed manually — otherwise the alert "dispatcher не будет
    # бесконечно рестартить" was a lie. Done BEFORE claim_dispatch so we don't
    # even reserve a slot when we know we're going to skip.
    tripped, breaker_why = _breaker_active(agent_name, task_id)
    if tripped:
        log(agent_name,
            f"BREAKER agent={agent_name} task={task_id} skip launch — {breaker_why}")
        return
    if breaker_why:
        log(agent_name,
            f"BREAKER agent={agent_name} task={task_id} {breaker_why}")

    # Dedup/lock gate (task 31bb7aad): never spawn a 2nd claude for an
    # (agent,task_id) that is already live or just spawned — parallel writers
    # clobber the shared workspace. Done BEFORE reposync so a burst of repeats
    # bails fast instead of running N× git-fetch and N× spawn.
    ok, why = claim_dispatch(agent_name, task_id)
    if not ok:
        log(agent_name, f"skip duplicate spawn ({agent_name},{task_id}) — {why}")
        return

    # HARD anti commit-clobber gate: every repo this agent works in must be
    # at remote (fetch + safe ff/rebase) before we hand the task over. If
    # not, we abort here — the agent never starts on a stale tree.
    if not ensure_repos_fresh(agent_name, repos or [], api_url,
                              agent_key, task_id, task_title):
        unclaim(agent_name, task_id)
        return

    chosen_model, reason = pick_model(model, labels)
    if chosen_model != model:
        log(agent_name, f"model override: {model} → {chosen_model} ({reason})")
    # Runtime-aware spawn (task ae7efdd0): resolve the agent's runtime from its
    # mesh-agents.json block (default claude_code). The comet branch builds a
    # different command + a lean, runtime-appropriate prompt; everything else
    # (gates, caps, dedup, completion-by-Mesh-status) is shared. Claude-only
    # prompt augmentation (recall_graph / mempalace_prefetch) is skipped for
    # non-claude runtimes — those tools/wings are Claude-Code-local.
    agent_cfg = _AGENTS_BY_NAME.get(agent_name) or {}
    runtime = (agent_cfg.get("runtime") or "claude_code").lower()
    # Pre-task triage gate (task 24e33cf2). The dispatcher will keep re-spawning
    # agents whose tasks rot in_progress (>4h with no activity); the only way
    # that loop converges is if each spawned session FIRST closes or escalates
    # its own ghosts instead of compounding new ones on top.
    # Routing-aware prompt framing (task 98a1db69). On a foreign-card wake the
    # session must NOT be told "you have a new task assigned" and must NOT be
    # ordered to check the card out: the credential it holds posts as THIS lane's
    # agent, and its cwd is THIS lane's workspace, so acting on the card produces
    # wrong-identity comments and edits in the wrong repo. Before this fix the
    # checkout 409 was the only thing standing between a foreign wake and another
    # agent's in-flight branch.
    _foreign = (routing == "foreign_wake")
    if _foreign:
        _second_step = (
            f"MANDATORY SECOND STEP — DO NOT check this task out. Task {task_id} "
            f"is assigned to {assignee_name!r}, NOT to you. You were woken only "
            f"because you were @-mentioned in its thread. Do NOT call "
            f"checkout_task on it, do NOT move it, do NOT edit files on its "
            f"behalf, and do NOT touch any branch or PR it references — the "
            f"owner may be working it right now, and your credential and "
            f"workspace are not theirs.\n\n"
        )
        _closing = (
            f"You were @-MENTIONED on task '{task_title}' (ID: {task_id}), which "
            f"is assigned to {assignee_name!r}. This card is NOT yours and is NOT "
            f"an assignment to you. Read the mention with "
            f"list_comments(task_id='{task_id}') and answer ONLY what was asked "
            f"of you, as a comment on that thread. If the mention routes actual "
            f"work to you, that work lives on a DIFFERENT card — find it with "
            f"get_my_tasks and work there, not here. Follow CLAUDE.md workflow."
        )
    else:
        _second_step = (
            f"MANDATORY SECOND STEP — checkout this task. Call "
            f"checkout_task(task_id='{task_id}', ttl_minutes=120) before touching "
            f"any files or making any changes.\n"
            f"  • 200 OK → proceed.\n"
            f"  • 409 conflict → another agent holds the lock. Read "
            f"checked_out_by_name and expires_in_seconds from the response. "
            f"Add ONE comment: «Skipping — task locked by @<name>, expires in "
            f"~<N> min. Will retry after expiry.» Then EXIT immediately — do NOT "
            f"start work on a task you do not hold.\n"
            f"  • checkout is auto-released when you move the task to "
            f"done/review/cancelled — no manual release_task needed at the end.\n\n"
        )
        _closing = (
            f"You have a new task assigned: '{task_title}' (ID: {task_id}). "
            f"Use get_task to read full details, then work on it autonomously. "
            f"Follow CLAUDE.md workflow."
        )
    prompt = (
        "MANDATORY FIRST STEP — own-backlog triage. BEFORE this task, call "
        "get_my_tasks with status_category=in_progress. For EACH of YOUR "
        "in_progress tasks whose last comment is >24h old (or which has no "
        "comments at all), do ONE of:\n"
        "  (a) close it as done with a concrete result (link / artifact / "
        "verifiable outcome), OR\n"
        "  (b) leave a hard-blocker comment naming the EXACT ask — one "
        "specific decision or input you need from the owner — and move it "
        "to a blocked status if available, otherwise just comment.\n"
        "Phrasings like «в очереди», «приоритезирую», «честно не сделал», "
        "«will get to it», «in queue», «прорабатываю», «буду делать», "
        "«запланировал» are FORBIDDEN — they hide the agent. Either close, "
        "or name the blocker. Only AFTER triaging your own stale backlog "
        "do you start this task.\n\n"
        + _second_step +
        # Wake-up read-protocol enforcement (memory-eval A3, task 0948aa57).
        # The fleet WRITES memory aggressively but almost never READS it back
        # (mempalace R:W was 1:56, episodic recall ~dead). This step forces ONE
        # light recall pass at spawn so each agent loads its own prior context
        # before re-deriving it. recall() is available to every Mesh agent;
        # mempalace tools only to Mac-Mini-local agents → guarded "if available".
        # Kept light + non-blocking on empty (plan §7): a single 1+1 read, no
        # loop, empty result is fine. This is what the D1 transcript-cron counts
        # toward the R:W target — it must be a real tool_use by the agent, NOT a
        # dispatcher-side pre-fetch (that is the separate G1-s3 recall_graph block).
        "MANDATORY THIRD STEP — wake-up memory read (load your own context "
        "BEFORE working). Make ONE light recall pass so you don't re-derive "
        "what past-you already knew:\n"
        "  • Call recall(query='<3-5 keywords from this task's title/domain>', "
        "min_importance=0.3) — your Mesh episodic memory of prior sessions on "
        "this topic. The min_importance=0.3 is REQUIRED: prior-session handoffs "
        "are stored as kind:session-checkpoint at importance 0.3, and recall's "
        "default 0.4 silently filters ALL of them out (that is why episodic "
        "recall measured ~17% — the answers exist but are below the default "
        "threshold). Pass 0.3 to actually see them.\n"
        "  • Call recall(tags_any=['pavel-decision'], scope='workspace') — "
        "canonical the operator decisions/directives (C4 channel; replaces "
        "get_canonical_updates). Apply any that touch your task.\n"
        "  • Call recall(tags_any=['solution'], query='<task topic>', "
        "order_by='decayed_relevance') — solution-journal: prior VERIFIED wins "
        "on a similar topic to reuse BEFORE starting.\n"
        "Skim the top hits and carry forward anything relevant. This is a "
        "light read — do NOT loop, broaden, or block if results come back "
        "empty; an empty result is fine, just proceed. Then start the task.\n\n"
        # READ-BEFORE-ACT (task dcdfe6a4, 2026-07-13). The ACP mandated a *memory* read at
        # wake-up but never a read of the task's OWN comment thread — so 27% of first comments
        # fleet-wide land on a thread the agent never opened. Measured by comment-read-probe.py
        # (per-task, per-session); the old `task-comments R:W` ratio could not see this — it
        # compares read CALLS to written COMMENTS and is anti-correlated with real compliance.
        "READ-BEFORE-ACT — read the task's own comment thread before you touch it. "
        "Call get_task(task_id, include_comments=true) (one call returns the whole thread) "
        "and read it to the end BEFORE your first comment or edit. The thread is where the "
        "prior session's findings, the reviewer's bounce reason, and any blocking ask already "
        "live — 27% of first comments fleet-wide are currently posted onto a thread the agent "
        "never read, which is how work gets re-derived and answered questions get re-asked. "
        "Reading it is one call.\n\n"
        # B3: pre-handoff VERIFIER-SUBAGENT gate (task 41e58997, the operator-approved 2026-06-15,
        # fleet-wide). Independent fresh-context verify before review/done — lifts orchestrator
        # subagent-adoption + cuts review-bounces. `already-verified` exemption avoids exec
        # double-work; the B3.5 self-verify checklist below stays as the criteria the verifier checks.
        "PRE-HANDOFF VERIFY GATE — before moving any task to `review` OR `done`, you MUST "
        "spawn the cheap `verifier` subagent (Agent tool, subagent_type='verifier') with the "
        "task's acceptance criteria + the proving commands/URLs/paths. Do NOT self-verify "
        "in-context — an independent fresh-context pass catches confirmation-bias misses that "
        "cause review-bounces. Paste the verifier's per-criterion VERDICT + overall "
        "SHIP/DO-NOT-SHIP/ROUTE as a task comment. DO-NOT-SHIP → fix and re-spawn the verifier; do "
        "not hand off. Exemptions (state in the handoff comment): trivial/no-op tasks (typo, "
        "label-only, pure-monitor/passive-wait) and tasks already verified by a verifier "
        "subagent this session. DEFAULT path; skipping is the exception.\n\n"
        # A4-2 (task b6be48ec, 2026-07-28): ROUTE — third verdict. 19 of Grove's 24
        # review-bounces in 07-20→07-27 were correct work with merge/deploy/release
        # outstanding — steps the executor has no rights to. Handing those to `review`
        # guarantees a bounce however good the work is (A4 #232fa45c).
        "ROUTE-GATE — the verifier has a THIRD verdict and it does NOT go to `review`. Before "
        "it may say SHIP it runs the two probes the reviewer runs anyway: P1 `gh pr view <n> "
        "--repo <org>/<repo> --json state,mergedAt,baseRefName` (accepted ONLY on state==MERGED "
        "+ non-null mergedAt + base main/master) and P2 the live probe named in the AC (content, "
        "not status code). If the work itself holds up but P1/P2 is outstanding AND you do not "
        "hold the rights to clear it (merge / release / deploy / backfill), the verdict is ROUTE "
        "with fields missing / probe / rights / next. On ROUTE: move_task → `todo` (NOT `review`), "
        "assign_task → the agent named in `rights` (merge → Delta; deploy/release → the repo's "
        "named owner or CI per §1f; rights unknown → the task creator, and say so), and comment "
        "the ROUTE block verbatim so the next holder sees the probe output instead of re-deriving "
        "it. `review` means \"this is live, please accept\" — nothing weaker.\n\n"
        # B3.5: pre-Done self-verify gate — cut 24 Done-reopens + 88 review-bounces
        "PRE-DONE GATE — before moving any task to `done`, verify each point:\n"
        "  1. List every acceptance criterion from the task description and confirm "
        "each with evidence (file path, command output, or Mesh comment).\n"
        "  2. Are there open subtasks assigned to you? If yes → cannot be done.\n"
        "  3. Code/PR task: is the PR merged (not just submitted)?\n"
        "  4. Deploy task: is the change live on the prod endpoint?\n"
        "Only mark done when ALL criteria have verifiable evidence. If point 3 or 4 "
        "is what fails and clearing it needs rights you do not hold → this is a ROUTE, "
        "not a review: `todo` + assign the rights-holder (see ROUTE-GATE above). Move to "
        "`review` only when the work IS live and what remains is someone's acceptance. "
        "Never claim done on 'should work' logic.\n\n"
        + _closing
    )
    # Auto-splitter gate (task 2dde31d9): if description meets multi-deliverable
    # threshold, inject a MANDATORY PRE-WORK step so the agent calls task-splitter
    # before any implementation — even if they skip the CLAUDE.md rule.
    # Skipped on a foreign-card wake (98a1db69): decomposing someone else's card
    # is exactly the wrong instruction — the misroute that prompted this gate had
    # Orbit told to task-split Nova's in-flight card.
    if not _foreign and _needs_splitter(task_desc):
        log(agent_name, f"auto-splitter hint injected for task {task_id} "
            f"({len(task_desc.split())} words, "
            f"{sum(1 for l in task_desc.splitlines() if l.startswith('## '))} ## headers)")
        prompt += (
            "\n\nPRE-WORK GATE — task-splitter auto-trigger: this task's description "
            "matches the multi-deliverable threshold (>300 words OR >3 ## sections). "
            "BEFORE starting any implementation:\n"
            "1. Call get_task to check subtask_count and read comments — "
            "if a 'verdict: split' or 'verdict: do_not_split' comment already exists, skip to step 5.\n"
            "2. Otherwise invoke task-splitter subagent with this task_id.\n"
            "3. Post the full YAML plan as a comment on this task (transparency rule).\n"
            "4. Write a mempalace drawer: kind=decomposition, verdict=<split|do_not_split>, "
            f"task_id={task_id}.\n"
            "5. If verdict=split: create subtasks + add_dependency per the DAG, then work on root subtasks.\n"
            "   If verdict=do_not_split: proceed with the task as a single unit."
        )
    if mention_context:
        # Author framing must be HONEST (#5a69b827). The old banner defaulted an
        # absent author_kind to "user" and then asserted «твой previous response,
        # видимо, оставил вопрос» — i.e. it told 290/290 sessions that a human was
        # waiting on an answer. Only the human case gets that framing now; an agent
        # author gets a neutral one, and an unknown author is named unknown.
        author_kind = (mention_context.get("author_kind") or "").lower()
        author_name = mention_context.get("author_name") or ""
        preview = (mention_context.get("comment_preview") or "")[:400]
        if author_kind in ("user", "human"):
            _who = f"**человек** ({author_name or 'the operator'})"
            _expect = ("Человек чего-то ждёт от тебя — это приоритет. Вероятно, твой "
                       "previous response оставил вопрос, и теперь пришёл ответ.")
        elif author_kind:
            _who = f"агент {author_name or '(имя не пришло)'}"
            _expect = ("Это peer-сигнал от другого агента, НЕ от the operator. Может быть "
                       "хэндофф, может быть FYI — реши по тексту, что от тебя нужно, "
                       "и если ничего — так и напиши, не изобретай работу.")
        else:
            _who = "**неизвестен** (сервер не прислал поле автора)"
            _expect = ("Не предполагай, что это the operator — определи автора по треду "
                       "прежде чем расставлять приоритет.")
        _prev = (f"Preview: «{preview}»." if preview.strip()
                 else "Тело комментария не пришло в событии — прочитай его сам, "
                      "не додумывай.")
        prompt += (
            f"\n\n⚡ WAKE-UP REASON: тебя @-tagged в комментарии task'и {task_id} "
            f"(автор: {_who}). {_expect} "
            f"ПЕРВЫМ ДЕЛОМ вызови list_comments(task_id={task_id}) "
            f"и прочитай **последние 3-5 комментариев** (включая тот, где тебя tag'нули). {_prev} "
            f"Затем реагируй по существу. Не повторяй уже сделанное. Если тебя "
            f"упомянули только как источник факта, а просьбы нет — не начинай работу."
        )
    # G1-s3: auto-inject 2-hop graph-recall memory context for captain/parent
    # tasks. SHIP-DARK behind RECALL_GRAPH_ENABLED (default OFF) → when disabled
    # this whole block is skipped and the prompt is unchanged. Per-task opt-out
    # via the `no-recall-graph` label. Wrapped so an unexpected error can never
    # abort a spawn.
    if runtime == "claude_code" and RECALL_GRAPH_ENABLED and "no-recall-graph" not in (labels or []):
        is_captain = ("captain" in (labels or [])) or bool(parent_task_id)
        if is_captain:
            try:
                _rg_block = _recall_graph_context(
                    agent_name, agent_key, api_url, task_id, task_title)
                if _rg_block:
                    prompt += _rg_block
                    log(agent_name,
                        f"recall_graph: injected memory context for captain task {task_id}")
            except Exception as e:
                log(agent_name,
                    f"recall_graph: injection skipped (unexpected error: {e})")
    # A3-followup (e8344446): DETERMINISTIC mempalace read for EVERY agent (not
    # captain-only) — the deterministic counterpart to the prompt-instructed wake-up
    # read the LLM keeps skipping. Reads the agent's own wing read-only + records a
    # per-agent sidecar read that moves the mempalace R:W metric. Never aborts a spawn.
    if runtime == "claude_code" and MEMPALACE_PREFETCH_ENABLED and "no-mempalace-prefetch" not in (labels or []):
        try:
            _mp_block = _mempalace_prefetch(agent_name, task_title)
            if _mp_block:
                prompt += _mp_block
                log(agent_name,
                    f"mempalace_prefetch: injected wing={agent_name.lower()} "
                    f"context for task {task_id}")
        except Exception as e:
            log(agent_name,
                f"mempalace_prefetch: injection skipped (unexpected error: {e})")
    cmd = [
        CLAUDE_BIN,
        "--dangerously-skip-permissions",
        "--permission-mode", "bypassPermissions",
        "--model", chosen_model,
        # Cost-saver (added 2026-05-24): cap turns to prevent context inflation
        # mega-sessions. Comet hit 273 turns x 145K avg ctx = 39.7M tokens
        # ($94 single session). After max-turns agent saves progress and exits;
        # stale-redispatch picks up follow-up next cycle.
        "--max-turns", "100",
        # Stabilises system-prompt hash → better cache_read hit rate across sessions.
        # Moves volatile per-machine sections (cwd/env/git) to first user turn.
        "--exclude-dynamic-system-prompt-sections",
        "-p", prompt,
    ]
    spawn_env = dict(ENV)
    # ISOLATION (2026-06-12 incident): the telegram channel plugin is enabled
    # globally, so every spawned `claude` loads it. Without TELEGRAM_STATE_DIR it
    # defaults to ~/.claude/channels/telegram = @kodiak_coach_bot (aux1 owns the
    # DEFAULT path). A dispatcher -p spawn that touches the channel would then post
    # through KODIAK's family-health bot (fleet→aux1 leak — the operator incident). Pin a
    # per-agent isolated state dir so a fleet spawn can NEVER reach the default
    # (aux1) channel; fleet agents reach the operator via their own bot / tg-reply outbox.
    spawn_env["TELEGRAM_STATE_DIR"] = os.path.expanduser(
        f"~/.claude-{agent_name.lower()}/channels/telegram")
    # Mesh MCP stdio server (~/bin/mesh-mcp) reads MESH_AGENT_KEY from its OWN
    # process environment and hard-exits if it's absent ("MESH_AGENT_KEY
    # environment variable is required for stdio mode") → 0 evc-mesh tools in
    # the session (task db9072a6). The mcp-registry loader bakes the key into
    # <workspace>/.mcp.json's env block, but that is a single point of failure
    # (loader.enabled flag + regen success + Claude Code applying the env block,
    # which flakes — cf. mcp_silent_subprocess_nospawn). Inject the key into the
    # spawned claude's env so mesh-mcp INHERITS it even if .mcp.json baking is
    # disabled or fails. .mcp.json baking stays primary; this is the fallback.
    if agent_key:
        spawn_env["MESH_AGENT_KEY"] = agent_key
        spawn_env.setdefault("MESH_API_URL", "https://mesh.example.com")
    if env_file:
        extra = _load_env_file(env_file)
        if extra:
            spawn_env.update(extra)
            log(agent_name, f"Loaded {len(extra)} env vars from {env_file}")
        else:
            log(agent_name, f"WARN: env_file {env_file} empty or unreadable")
    # Per-agent prod DB read-only creds (lead-access model, task 3b272632).
    # Convention: ~/.config/agents/<agent>-prod.env (mode 600) sources the
    # per-product secret files. Loaded ON TOP of the github env_file so a lead
    # gets their DB creds WITHOUT polluting the shared github env_file (which
    # several agents reuse — least-privilege). Absent file → no-op (only leads
    # have one). This is the "2nd env file" mechanism without config threading.
    prod_env_path = os.path.expanduser(f"~/.config/agents/{agent_name.lower()}-prod.env")
    if os.path.exists(prod_env_path):
        prod_extra = _load_env_file(prod_env_path)
        if prod_extra:
            spawn_env.update(prod_extra)
            log(agent_name, f"Loaded {len(prod_extra)} prod-DB env vars from {prod_env_path}")
        else:
            log(agent_name, f"WARN: prod env_file {prod_env_path} present but empty/unreadable")
    # Per-agent Claude API credential override (B3.P1, task f3863f38).
    # Atlas/Orbit use the MacBook-Pro Anthropic key (higher session budget);
    # all other agents fall back to the Mac-Mini key in the system env.
    # The credential file is provisioned by Orbit at ~/.config/agents/ (600).
    # Loaded LAST so it wins over any ANTHROPIC_API_KEY set by earlier blocks.
    # Only ONE file format is supported: shell KEY=VALUE (*.env) via _load_env_file.
    #
    # The `*.token` → CLAUDE_CODE_OAUTH_TOKEN branch was REMOVED 2026-07-24
    # (#c8a722c3, root cause #8eba4210). It was already dead — no agent in
    # mesh-agents.json has ever set `claude_env_file` — and it injected exactly the
    # credential shape this task is eradicating fleet-wide: with
    # CLAUDE_CODE_OAUTH_TOKEN in the environment the CLI builds a session with
    # refreshToken=null that CANNOT refresh, which is the real source of the
    # recurring "4-hour" 401 windows. Subscription auth must come from the ambient
    # Keychain login, never from an env var. Deleted, not disabled, so it cannot
    # quietly grow back — if a *.token file is configured, fail loudly instead.
    if claude_env_file:
        if claude_env_file.endswith(".token"):
            log(agent_name,
                f"ERROR: claude_env_file {claude_env_file} is a *.token file — "
                f"env-injected CLAUDE_CODE_OAUTH_TOKEN is REMOVED (#c8a722c3); it "
                f"makes the session refresh-incapable. Use ambient Keychain login "
                f"or a *.env file with an API key. Ignoring this setting.")
        else:
            claude_extra = _load_env_file(claude_env_file)
            if claude_extra:
                spawn_env.update(claude_extra)
                log(agent_name, f"Claude credential loaded from {claude_env_file}")
            else:
                log(agent_name, f"WARN: claude_env_file {claude_env_file} empty or unreadable")
    # Two-tier workflow gating (the operator 2026-06-04). Dynamic Workflows enabled
    # ONLY for orchestrator-tier agents; worker-tier (product leads + devs)
    # stay deterministic — no fan-out fleets, no ultracode, no captain-via-
    # workflow. Tier controlled by per-agent `workflows: true` flag in
    # mesh-agents.json — adding an orchestrator = config change, not code
    # change (B4·P1, task e25b14b7). Agents without the flag default to false.
    # Orchestrators run workflows in the headless captain path (verified
    # 2026-06-04: headless claude -p awaits + returns workflow result).
    if _AGENTS_BY_NAME.get(agent_name, {}).get("workflows", False):
        spawn_env.pop("CLAUDE_CODE_DISABLE_WORKFLOWS", None)
    else:
        spawn_env["CLAUDE_CODE_DISABLE_WORKFLOWS"] = "1"
    # Runtime override (task ae7efdd0). For non-claude runtimes, discard the
    # claude command + env built above and build the runtime-specific spawn
    # (its own lean prompt + minimal env). The claude block above ran harmlessly
    # for comet (read-only env probing; lumen has no claude_env_file/prod-env),
    # only its `cmd`/`spawn_env` are replaced here. Comet carries its own Mesh
    # key + MCP config in its profile, so none of the claude env injections apply.
    if runtime != "claude_code":
        cmd, spawn_env = _build_runtime_spawn(
            runtime, agent_name, agent_cfg, task_id, task_title, task_desc, env_file)
        if cmd is None:
            log(agent_name, f"unknown runtime {runtime!r} for {agent_name} — skip spawn")
            unclaim(agent_name, task_id)
            return
    # Concurrency cap (added 2026-05-21). Prevents thundering herd → API rate-limit.
    active = _live_active_count()
    if active >= MAX_CONCURRENT_SPAWNS:
        log(agent_name,
            f"defer spawn — concurrency cap {active}/{MAX_CONCURRENT_SPAWNS} reached; "
            "will retry next stale-cycle")
        unclaim(agent_name, task_id)
        # B3·dq (cap-reap must not count toward give-up) is now handled by the
        # `dispatch_claude` wrapper's refund, which is scoped to THIS attempt's
        # token. The old inline decrement here was unscoped and could refund a
        # concurrent path's in-flight increment instead.
        return
    # Per-agent fair queueing (added 2026-05-22). Prevent any single agent
    # from holding all global slots while others starve. E.g. Atlas cascading
    # 5 stale-redispatches locked Kilo out of his 24-task UI sprint backlog.
    # B3.4: cap is now per-agent configurable via `max_concurrent` in mesh-agents.json.
    per_agent = _live_per_agent_count(agent_name)
    _per_agent_cap = (_AGENTS_BY_NAME.get(agent_name) or {}).get("max_concurrent", MAX_PER_AGENT_SPAWNS)
    if per_agent >= _per_agent_cap:
        log(agent_name,
            f"defer spawn — per-agent cap {per_agent}/{_per_agent_cap} "
            "reached; other agents go first")
        unclaim(agent_name, task_id)
        # Refund is the wrapper's job — see the concurrency-cap branch above.
        return
    # Regenerate .mcp.json from the central registry (task 75de4532). Done after
    # all spawn-gates pass and right before launch so we only touch the file when
    # we're actually about to start the agent. Fail-safe: no-op unless the loader
    # flag is set; any error keeps the existing .mcp.json (logged, never fatal).
    # Claude-Code-only: comet carries its MCP config in its own profile, not a
    # workspace .mcp.json (task ae7efdd0).
    if runtime == "claude_code" and _mcp_registry is not None:
        _mcp_registry.regenerate_safe(agent_name, workspace, agent_key, log)
    _launch_label = "claude" if runtime == "claude_code" else runtime
    log(agent_name, f"Launching {_launch_label} ({chosen_model if runtime == 'claude_code' else runtime}) → {log_file.name}")
    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            cmd,
            cwd=workspace,
            stdout=lf,
            stderr=lf,
            env=spawn_env,
        )
    # PAST THE POINT OF NO RETURN: the child exists. Nothing below may be
    # allowed to make this function report "did not spawn" — the wrapper would
    # then refund a spawn that really ran, which is the one thing the shared
    # ladder cannot tolerate (a card that spawns without being charged for it
    # re-enters forever). Bookkeeping failures are logged and swallowed.
    try:
        register_pid(agent_name, task_id, proc, log_file.name)
        log(agent_name, f"{_launch_label} pid={proc.pid} registered for ({agent_name},{task_id})")
        # Successful spawn — clear repo-unsafe abort marker AND crash-retry if present
        with _REPO_UNSAFE_LOCK:
            if _REPO_UNSAFE.pop(task_id, None) is not None:
                log(agent_name, f"cleared repo-unsafe marker for {task_id} after successful spawn")
            _REPO_UNSAFE_COUNT.pop(task_id, None)  # reset consecutive abort counter (task bfa8e55a)
            if _CRASH_RETRY.pop(task_id, None) is not None:
                log(agent_name, f"cleared crash-retry marker for {task_id} after successful spawn")
            if _CRASH_COUNT.pop(task_id, None) is not None:
                pass  # crash counter reset on successful spawn
    except Exception as e:
        log(agent_name, f"post-spawn bookkeeping failed for {task_id} "
                        f"(child pid={proc.pid} IS running): {e}")
    # The ONLY truthy return in this function: a child process exists. The
    # wrapper settles the respawn-budget attempt on True and refunds on
    # anything else, so this line is what separates "ran" from "refused".
    return True


def _parse_iso_utc(ts: str):
    """Parse RFC3339 'Z' or +00:00 timestamp -> naive UTC datetime, or None."""
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def fetch_in_progress_tasks(agent_key: str, api_url: str) -> list:
    """List one agent's in_progress tasks via Mesh API. Returns [] on failure."""
    try:
        url = f"{api_url}/api/v1/agents/me/tasks?status_category=in_progress&limit=200"
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        tasks = data.get("tasks") or []
        for t in tasks:
            t["_status_category"] = "in_progress"
        return tasks
    except Exception as e:
        log("stale-redispatch", f"list in_progress failed: {e}")
        return []


def fetch_open_tasks(agent_key: str, api_url: str) -> list:
    """List one agent's open tasks eligible for stale-redispatch recovery.

    SSE event delivery in Mesh is not durable: if the agent's listener was
    disconnected when a task.assigned fired (e.g. dispatcher restart, server
    blip), the event is lost and the task can sit in todo untouched. We pull
    `todo` so the stale-redispatch loop can recover those.

    `triage` is intentionally EXCLUDED (task 9dd40e25). triage is the human
    parking lane: agents move blocked tasks there to STOP respawn, and the
    count==3 auto-triage escalation moves stuck tasks there for the operator. The old
    code still re-fetched triage every 4h, so "moving to triage" never actually
    parked anything — task 963f7e95 (blocked on a missing API key) was
    re-dispatched 39×, and each fresh session was a chance to re-fire a stale
    request. Excluding triage makes it a real, restart-durable parking brake;
    a human moving the task back to todo/in_progress re-arms dispatch.

    `review`, `done`, and `cancelled` stay excluded — tasks awaiting human
    review or in terminal states must NOT trigger agent respawn (task 9090b3fc).
    """
    seen = set()
    out: list = []
    for cat in ("todo",):
        try:
            url = (f"{api_url}/api/v1/agents/me/tasks"
                   f"?status_category={cat}&limit=200")
            req = Request(url, headers={"X-Agent-Key": agent_key})
            with urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            for t in data.get("tasks") or []:
                tid = t.get("id")
                if tid and tid not in seen:
                    seen.add(tid)
                    # Tag stale threshold and status category.
                    # todo = fast retry; triage = full (parked, awaits human).
                    t["_stale_threshold"] = (
                        TODO_STALE_THRESHOLD_SEC if cat == "todo"
                        else STALE_THRESHOLD_SEC
                    )
                    t["_status_category"] = cat
                    out.append(t)
        except Exception as e:
            log("stale-redispatch", f"list {cat} failed: {e}")
    return out


def fetch_todo_tasks(agent_key: str, api_url: str) -> list:
    """List one agent's todo tasks via Mesh API. Used to find repo-unsafe-aborted
    tasks which were returned to todo by return_task_to_todo()."""
    try:
        url = f"{api_url}/api/v1/agents/me/tasks?status_category=todo&limit=200"
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        return data.get("tasks") or []
    except Exception as e:
        log("stale-redispatch", f"list todo failed: {e}")
        return []


def _detect_rate_limit_in_log(log_path) -> str:
    """Read up to last 4KB of session log, search for rate-limit keywords.

    Returns matched keyword if found, empty string otherwise.
    """
    try:
        if not log_path.is_file():
            return ""
        size = log_path.stat().st_size
        if size == 0:
            return ""
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 4096))
            data = f.read(4096).decode("utf-8", errors="replace")
        for kw in RATE_LIMIT_KEYWORDS:
            if kw.lower() in data.lower():
                return kw
        return ""
    except Exception:
        return ""


def _detect_auth_fail_in_log(log_path) -> str:
    """Read up to last 4KB of session log, detect a real auth-expiry death.

    Returns matched keyword if found, empty string otherwise. An auth-dead
    session writes its error and exits in ~0.1s, so the log is short but NOT
    empty — hence this cannot be folded into the empty-log check.

    The keyword must sit on a line the CLI ITSELF emitted (full-line anchored,
    in a log of at most AUTH_FAIL_MAX_LINES lines) — not merely appear somewhere
    in the text. See the AUTH_FAIL_* block for why: agents write reports ABOUT
    auth incidents, and matching those halts the fleet. #3146391b
    """
    try:
        if not log_path.is_file():
            return ""
        size = log_path.stat().st_size
        if size == 0:
            return ""
        with open(log_path, "rb") as f:
            truncated = size > 4096
            f.seek(max(0, size - 4096))
            data = f.read(4096).decode("utf-8", errors="replace")
        # Cheap pre-filter: no keyword anywhere → definitely not an auth death.
        low = data.lower()
        if not any(kw.lower() in low for kw in AUTH_FAIL_KEYWORDS):
            return ""
        lines = [_AUTH_ANSI_RE.sub("", ln).strip() for ln in data.splitlines()]
        if truncated and lines:
            lines = lines[1:]      # first line of a tail read may be a fragment
        lines = [ln for ln in lines if ln]
        if len(lines) <= AUTH_FAIL_MAX_LINES:
            for ln in lines:
                if AUTH_FAIL_LINE_RE.match(ln):
                    lnl = ln.lower()
                    for kw in AUTH_FAIL_KEYWORDS:
                        if kw.lower() in lnl:
                            return kw
        # Keyword present but the structure says "text, not event". Almost always
        # an agent reporting on a past auth incident (5 such logs in 6656 as of
        # 2026-07-30) — correct to ignore.
        #
        # But it is ALSO where a format drift would land: if the CLI ever prefixes
        # the banner with a timestamp, wraps it, or precedes it with >5 lines of
        # preamble, this branch swallows a REAL death and the 1354-session churn
        # loop returns SILENTLY — the same silence that let the original bug run
        # for two days. So leave a trace. Log-only on purpose: this must never
        # reach _trigger_auth_fail_pause, or we are back to halting the fleet on
        # an agent's prose. If these lines start appearing in bursts rather than
        # every few weeks, the banner format changed — re-derive AUTH_FAIL_LINE_RE
        # from fresh dead logs before trusting the gate again.
        try:
            log("dispatcher", f"auth-keyword in {getattr(log_path, 'name', log_path)} "
                              f"but not an emitted banner ({len(lines)} lines) — "
                              f"treated as discussion, NOT an auth death")
        except Exception:
            pass
        return ""
    except Exception:
        return ""


def _trigger_auth_fail_pause(reason: str) -> None:
    """Auto-create PAUSE_ALL + TG alert on an expired Claude login.

    Fires 1× per dispatcher session (resets on restart). Unlike the rate-limit
    pause, waiting does NOT help — this stays paused until a human runs /login.
    """
    global _AUTH_FAIL_PAUSED
    if _AUTH_FAIL_PAUSED:
        return
    _AUTH_FAIL_PAUSED = True
    try:
        PAUSE_DIR.mkdir(parents=True, exist_ok=True)
        PAUSE_GLOBAL_FILE.touch()
    except OSError:
        pass
    log("dispatcher", f"AUTH-FAIL-DETECTED — auto-paused (PAUSE_ALL set): {reason}")
    tg_body = (
        "\U0001f510 **Claude login expired** — dispatcher auto-paused.\n"
        f"Reason: {reason}\n"
        "Every spawn dies instantly at auth and re-spawns, so this is paused to "
        "stop the churn. Waiting does NOT fix it.\n"
        "Fix: run `claude` on the Mac Mini and `/login`, then `/restart` the dispatcher."
    )
    _post_tg_nag(TG_NAG_CHAT_ID, tg_body)


def _trigger_rate_limit_pause(reason: str) -> None:
    """Auto-create PAUSE_ALL + TG alert. Fires 1× per dispatcher session
    (resets on restart). Caller MUST hold _REPO_UNSAFE_LOCK or similar guard."""
    global _RATE_LIMIT_PAUSED
    if _RATE_LIMIT_PAUSED:
        return
    _RATE_LIMIT_PAUSED = True
    try:
        PAUSE_DIR.mkdir(parents=True, exist_ok=True)
        PAUSE_GLOBAL_FILE.touch()
    except OSError:
        pass
    log("dispatcher", f"RATE-LIMIT-DETECTED — auto-paused (PAUSE_ALL set): {reason}")
    tg_body = (
        "\u26d4 **API rate-limit detected** \u2014 dispatcher auto-paused.\n"
        f"Reason: {reason}\n"
        "All agents will not spawn until /restart. Wait for limit window "
        "to reset before /restart-ing."
    )
    _post_tg_nag(TG_NAG_CHAT_ID, tg_body)


def _check_rate_limit_cluster() -> None:
    """Sliding-window cluster check across agents. If RATE_LIMIT_CLUSTER_THRESHOLD
    or more crashes happened within the last RATE_LIMIT_CLUSTER_WINDOW seconds,
    likely API rate-limit, not isolated bug. Trigger pause."""
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_CLUSTER_WINDOW
    recent = [r for r in _CRASH_HISTORY if r[0] >= cutoff]
    _CRASH_HISTORY[:] = recent  # prune
    if len(recent) >= RATE_LIMIT_CLUSTER_THRESHOLD:
        agents_involved = sorted({r[1] for r in recent})
        reason = (
            f"{len(recent)} empty-log crashes across {len(agents_involved)} "
            f"agents ({', '.join(agents_involved)}) in last "
            f"{RATE_LIMIT_CLUSTER_WINDOW}s — likely API rate-limit"
        )
        _trigger_rate_limit_pause(reason)


def post_dispatcher_nag(api_url: str, agent_key: str, task_id: str,
                        agent_name: str, count: int, age_hours: float) -> None:
    """Post a public comment on a task stuck through 2+ stale-redispatches.

    Fires exactly once per task (tracked in _STALE_NAGGED). Best-effort —
    failures are logged but never crash the loop.
    """
    body = (
        f"\U0001f501 **Dispatcher nag** \u2014 \u0437\u0430\u0434\u0430\u0447\u0430 in_progress ~{age_hours:.1f}h, "
        f"\u0430\u0433\u0435\u043d\u0442 `{agent_name}` \u043f\u0435\u0440\u0435-\u0441\u043f\u0430\u0432\u043d\u0435\u043d dispatcher'\u043e\u043c **{count}** "
        "\u0440\u0430\u0437 \u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f \u0441\u0442\u0430\u0442\u0443\u0441\u0430.\n\n"
        "\u042d\u0442\u043e \u043d\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u043e \u0435\u0441\u043b\u0438 \u0436\u0434\u0451\u0442\u0441\u044f review/deploy/decision \u043e\u0442 \u0447\u0435\u043b\u043e\u0432\u0435\u043a\u0430 \u2014 "
        "\u043e\u0441\u0442\u0430\u0432\u044c \u043a\u0430\u043a \u0435\u0441\u0442\u044c, \u044f \u043d\u0435 \u0431\u0443\u0434\u0443 \u043d\u0430\u0433'\u0430\u0442\u044c \u0441\u043d\u043e\u0432\u0430. \u0415\u0441\u043b\u0438 \u0436\u0435 "
        "\u044d\u0442\u043e **stuck** \u2014 \u0437\u0430\u043a\u0440\u043e\u0439 \u0447\u0435\u0440\u0435\u0437 `move_task` \u2192 `review`/`done` \u043b\u0438\u0431\u043e \u044f\u0432\u043d\u044b\u0439 blocker \u0441 ask. "
        "\u0422\u0440\u0435\u0442\u0438\u0439 + \u043f\u043e\u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0435 re-\u0441\u043f\u0430\u0432\u043d\u044b \u044f \u043d\u0435 \u0431\u0443\u0434\u0443 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0438\u0440\u043e\u0432\u0430\u0442\u044c "
        "(\u0442\u043e\u043b\u044c\u043a\u043e \u0441\u043f\u0430\u0432\u043d\u0438\u0442\u044c).\n\n"
        "_\u0430\u0432\u0442\u043e\u043a\u043e\u043c\u043c\u0435\u043d\u0442 mesh-dispatcher \u043d\u0430 2-\u043c stale-redispatch, 1\u00d7 per task_"
    )
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/comments"
        data = json.dumps({"body": body}).encode("utf-8")
        req = Request(url, data=data, method="POST",
                      headers={"X-Agent-Key": agent_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201):
                log("stale-redispatch",
                    f"nag comment unexpected status {resp.status} for {task_id}")
            else:
                log("stale-redispatch",
                    f"nag comment posted for {task_id} count={count}")
    except Exception as e:
        log("stale-redispatch", f"nag comment failed for {task_id}: {e}")


def _pull_next_task_for_agent(agent_cfg: dict, api_url: str) -> None:
    """Reap-driven work-steal: when slot frees, immediately dispatch next task.

    Called from _reaper_loop after a slot transitions from alive → reaped.
    Honors all the same caps & cooldowns as the stale-redispatch loop, but
    runs reactively (on reap) instead of on a fixed 5-minute timer.
    """
    name = agent_cfg["name"]
    agent_key = agent_cfg["agent_key"]

    # Capacity check first — cheap, no API call.
    if _live_active_count() >= MAX_CONCURRENT_SPAWNS:
        return
    if _live_per_agent_count(name) >= MAX_PER_AGENT_SPAWNS:
        return

    # Fetch this agent's open backlog. fetch_in_progress + fetch_open (triage/todo only; review excluded by task 9090b3fc).
    candidates = (fetch_in_progress_tasks(agent_key, api_url) or [])
    candidates += (fetch_open_tasks(agent_key, api_url) or [])
    if not candidates:
        return

    # Skip tasks we already hold a live slot for (avoid double-spawn).
    with _DISPATCH_LOCK:
        held = {tid for (a, tid) in _LIVE.keys() if a == name}

    # Skip tasks under cooldown (just crashed / just defer'd / repo-unsafe).
    mono = time.monotonic()
    # `timezone` is not module-level in this file (only `datetime` is); every
    # other caller imports it function-locally, so do the same here.
    from datetime import timezone as _timezone
    _now_utc_naive = datetime.now(_timezone.utc).replace(tzinfo=None)
    eligible = []
    for t in candidates:
        tid = t.get("id")
        if not tid or tid in held:
            continue
        # Skip review/done/cancelled — never re-dispatch awaiting-human tasks.
        _cat = t.get("_status_category", "in_progress")
        if _cat in _SKIP_CATEGORIES:
            log("reaper",
                f"pull-on-reap: skip {tid[:8]} reason=status_{_cat}")
            continue
        # Skip scheduled tasks: a future due_date means the task is intentionally
        # WAITING, not stuck. The stale-redispatch loop has had this gate since
        # 2026-05-22; pull-on-reap did not, despite this docstring claiming it
        # "honors all the same caps & cooldowns". A scheduled task therefore
        # survived one path and was re-dispatched by the other the moment a slot
        # freed — an unbounded respawn loop at full session cost (#ba50c1a2:
        # measurement card due 08-04 re-picked 10 min after reap, $22.47/spawn).
        # Parking in `todo` is the documented way to schedule work, so the loop
        # hits precisely the tasks that were parked correctly.
        due = t.get("due_date")
        if due:
            due_dt = _parse_iso_utc(due)
            if due_dt and due_dt > _now_utc_naive:
                log("reaper",
                    f"pull-on-reap: skip {tid[:8]} reason=scheduled_until_{due}")
                continue
        with _REPO_UNSAFE_LOCK:
            if tid in _REPO_UNSAFE and mono - _REPO_UNSAFE[tid] < REPO_UNSAFE_RETRY_SEC:
                continue
            if tid in _CRASH_RETRY and mono - _CRASH_RETRY[tid] < _crash_retry_delay(_CRASH_COUNT.get(tid, 1)):
                continue
        with _STALE_LOCK:
            last = _STALE_LAST.get(tid)
            if last is not None and mono - last < TODO_STALE_THRESHOLD_SEC:
                continue
        eligible.append(t)
    if not eligible:
        return

    # Sort: priority desc, then age desc (oldest first within same priority).
    PRIORITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
    def _sort_key(t):
        pri = (t.get("priority") or "none").lower()
        pri_rank = PRIORITY_ORDER.get(pri, 5)
        upd = _parse_iso_utc(t.get("updated_at"))
        from datetime import datetime
        ts = upd.timestamp() if upd else 0.0
        return (pri_rank, ts)  # lower priority rank first, older first
    eligible.sort(key=_sort_key)

    next_task = eligible[0]
    tid = next_task["id"]
    title = (next_task.get("title") or "").replace("\n", " ")
    log(name, f"pull-on-reap → next task {tid[:8]} pri={next_task.get('priority','?')} '{title[:50]}'")
    # Shared respawn ladder (#3788c8f0). This path was the single largest
    # unbounded re-entry source — 86 of 226 surplus spawns in the 7d window —
    # because it read `_STALE_LAST` for a cooldown that only the stale loop ever
    # wrote. Consuming here both bounds the ladder AND finally stamps that
    # cooldown, so the check at the top of this function starts working.
    _ok, _count, _token = _respawn_budget(name, tid, "pull-on-reap")
    if not _ok:
        return
    try:
        dispatch_claude(
            agent_name=name,
            agent_key=agent_key,
            workspace=agent_cfg["workspace"],
            model=agent_cfg.get("model", "sonnet"),
            task_id=tid,
            task_title=title,
            api_url=api_url,
            env_file=agent_cfg.get("env_file"),
            repos=resolve_repos(agent_cfg),
            claude_env_file=_resolve_claude_env_file(agent_cfg),
            budget_token=_token,
        )
    except Exception as e:
        log(name, f"pull-on-reap dispatch failed for {tid}: {e}")


_STATUS_ID_CACHE: dict = {}   # project_id -> {"slug:x"/"cat:x": status_id}


def _resolve_status_id(api_url: str, agent_key: str, task_id: str,
                       want: str = "triage"):
    """Resolve a status_id for the task's project by slug, then category.

    The REST /tasks/{id}/move endpoint requires `status_id` (a UUID) — it does
    NOT accept `status_slug`. The per-project status set is fetched once and
    cached. Returns the id, or None on any failure / no matching status.
    """
    def _get(url):
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    try:
        task = _get(f"{api_url}/api/v1/tasks/{task_id}")
        pid = (task.get("task") or task).get("project_id")
        if not pid:
            return None
        if pid not in _STATUS_ID_CACHE:
            statuses = _get(f"{api_url}/api/v1/projects/{pid}/statuses")
            m = {}
            for s in (statuses or []):
                if s.get("slug"):
                    m[f"slug:{s['slug']}"] = s["id"]
                if s.get("category"):
                    m.setdefault(f"cat:{s['category']}", s["id"])
            _STATUS_ID_CACHE[pid] = m
        m = _STATUS_ID_CACHE[pid]
        return m.get(f"slug:{want}") or m.get(f"cat:{want}")
    except Exception:
        return None


def _dispatch_move(api_url: str, agent_key: str, task_id: str,
                   status_id: str, log_ctx: str = "dispatcher") -> bool:
    """POST /tasks/{id}/move, or log-only when DISPATCHER_MUTATIONS=report."""
    if DISPATCHER_MUTATIONS == "report":
        log(log_ctx, f"[dry-run] would move {task_id[:8]} → {status_id[:8]}")
        return True
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/move"
        data = json.dumps({"status_id": status_id}).encode("utf-8")
        req = Request(url, data=data, method="POST",
                      headers={"X-Agent-Key": agent_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as r:
            ok = r.status in (200, 201)
            if not ok:
                log(log_ctx, f"move unexpected status {r.status} for {task_id}")
            return ok
    except Exception as e:
        log(log_ctx, f"move failed for {task_id}: {e}")
        return False



# --- Recurring throwaway auto-close (added 2026-05-31) -------------------
# Daily monitoring tasks (drift / analytics) where the next recurring
# instance will fire on schedule — no the operator decision needed if the agent
# missed this one (dispatcher respawn loop, auth flap, etc).
# the operator rule 2026-05-31 after 5 false-positive auto-triages on 2026-05-29:
# "recurring tasks where there's nothing to do should auto-close, not pile
# up in triage as if they need decisions."
_THROWAWAY_KIND_LABELS = frozenset({"kind:drift", "kind:analytics"})

def _is_recurring_throwaway(t: dict) -> bool:
    """True if task is a recurring monitoring check that can safely auto-close
    on stale-respawn loop (next instance will fire from the schedule)."""
    if not t.get("recurring_schedule_id"):
        return False
    labels = set(t.get("labels") or [])
    if "phase:execute" not in labels:
        return False
    return bool(labels & _THROWAWAY_KIND_LABELS)


# --- Passive-wait park (the operator rule 2026-06-03) --------------------------
# Internal verify / monitor / passive-wait tasks that are idle BY DESIGN
# (waiting on a 7-day window, a scheduled reactivation, an external clock).
# On a stale-respawn loop these have nothing to triage — there is NO human
# decision pending. Park to backlog silently instead of pinging the operator.
# the operator 2026-06-03: "такое приходит на триаж, а там триадить нечего".
# The MEMBERSHIP now lives in `human_gate.PASSIVE_WAIT_LABELS` and is imported at the top of
# this file (#7da3577d) — it was a second copy of a set S5 also renders from, and a label
# added to one copy and not the other produces a card this file parks while the digest asks
# the operator to decide it, a divergence no status reports. The PREDICATE stays here and stays
# distinct from `_is_human_verify` below: this one drives the stale-respawn park, that one
# drives digest routing, and merging them into the union would fuse two the operator rules.
def _is_passive_wait(t: dict) -> bool:
    """True if task is an internal verify/monitor/passive-wait task that
    should park to backlog (NOT pester the operator) on stale-respawn loop."""
    labels = set(t.get("labels") or [])
    return bool(labels & _PASSIVE_WAIT_LABELS)


# --- Human-verify / host-impossible (the operator rule 2026-06-06) -------------
# Tasks that REQUIRE a human or a specific host an agent's headless session
# can't reach (e.g. MacBook-only filesystem step #bff66151 — servers.md lives
# in the operators MacBook Obsidian vault, unreachable from Mac Mini). These are
# NOT decisions: they are known human/host chores that an interactive human
# session does on its own clock. Two leaks they caused (2026-06-06):
#   (1) on a stale-respawn loop the agent host can never satisfy them →
#       count==3 → auto-triage-to-the operator with a "needs your decision" comment,
#       even though there is no decision. FIX: park to backlog, off the operator.
#   (2) in the review-sweep, a kind:human-verify task assigned to a user lands
#       in the "🔴 awaiting your decision" digest. FIX: route to a non-decision
#       info bucket so it stays visible in Mesh but never pings the operator as a gate.
# Membership imported from `human_gate.HUMAN_VERIFY_LABELS` at the top of this file
# (#7da3577d); see the note on `_is_passive_wait` above for why the two stay separate.
def _is_human_verify(t: dict) -> bool:
    """True if task needs a human / specific host an agent cannot satisfy."""
    labels = set(t.get("labels") or [])
    return bool(labels & _HUMAN_VERIFY_LABELS)


def auto_close_throwaway_task(api_url: str, agent_key: str,
                              task_id: str, agent_name: str,
                              count: int, age_hours: float) -> bool:
    """Auto-close a recurring throwaway task as done. Posts a brief
    explanatory comment (no @operator mention, no TG ping). Returns True on
    successful move. Tracked in _TRIAGED_AUTO same as auto_triage so the
    respawn-skip circuit-breaker fires.
    """
    status_id = _resolve_status_id(api_url, agent_key, task_id, "done")
    moved = False
    if status_id:
        moved = _dispatch_move(api_url, agent_key, task_id, status_id, "stale-redispatch")
    else:
        log("stale-redispatch",
            f"throwaway auto-close: no done status_id resolvable for {task_id}")

    if moved:
        log("stale-redispatch",
            f"throwaway auto-closed {task_id} (count={count})")

    body = (
        f"\ud83d\udd01 **Recurring throwaway auto-closed** \u2014 "
        f"\u0437\u0430\u0434\u0430\u0447\u0430 {count}\u00d7 \u0440\u0435\u0441\u043f\u0430\u0432\u043d\u0438\u043b\u0430\u0441\u044c "
        f"\u0430\u0433\u0435\u043d\u0442\u043e\u043c `{agent_name}` (age ~{age_hours:.1f}h) "
        f"\u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f \u0441\u0442\u0430\u0442\u0443\u0441\u0430.\n\n"
        "\u042d\u0442\u043e recurring monitoring check (phase:execute + kind:drift|analytics). "
        "\u0421\u043b\u0435\u0434\u0443\u044e\u0449\u0430\u044f \u0438\u043d\u0441\u0442\u0430\u043d\u0446\u0438\u044f "
        "\u0440\u0430\u0441\u043f\u0438\u0441\u0430\u043d\u0438\u044f \u043e\u0442\u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442 "
        "\u0432 \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 cycle \u2014 "
        "the operator decision \u043d\u0435 \u043d\u0443\u0436\u0435\u043d.\n\n"
        "_\u0410\u0432\u0442\u043e\u0437\u0430\u043a\u0440\u044b\u0442\u0438\u0435 \u0432\u043c\u0435\u0441\u0442\u043e auto-triage (the operator rule 2026-05-31)._"
    )
    # body uses \u escapes \u2014 decode to real bytes for POST
    body = body.encode().decode("unicode_escape")

    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/comments"
        data = json.dumps({"body": body}).encode("utf-8")
        req = Request(url, data=data, method="POST",
                      headers={"X-Agent-Key": agent_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201):
                log("stale-redispatch",
                    f"throwaway auto-close comment unexpected status {resp.status} for {task_id}")
    except Exception as e:
        log("stale-redispatch",
            f"throwaway auto-close comment failed for {task_id}: {e}")
    return moved


def auto_close_review_throwaway(api_url: str, agent_key: str,
                               task_id: str, assignee_name: str) -> bool:
    """Close a recurring-throwaway monitoring check (drift/analytics) that
    rotted in `review`. Moves it to done and posts a brief note (no @operator, no
    TG). Used by the review-sweep — these instances are disposable; the next
    scheduled run fires on its own. the operator rule 2026-06-06 (leak #2).
    Gated by REVIEW_SWEEP_AUTOCLOSE (default 1). DISPATCHER_MUTATIONS=report
    suppresses the POST."""
    if not REVIEW_SWEEP_AUTOCLOSE:
        log("review-sweep", f"autoclose disabled (REVIEW_SWEEP_AUTOCLOSE=0): {task_id[:8]}")
        return False
    status_id = _resolve_status_id(api_url, agent_key, task_id, "done")
    moved = False
    if status_id:
        moved = _dispatch_move(api_url, agent_key, task_id, status_id, "review-sweep")
    else:
        log("review-sweep",
            f"review-throwaway close: no done status_id resolvable for {task_id}")
    if moved:
        log("review-sweep",
            f"closed recurring-throwaway {task_id[:8]} stuck in review "
            f"(was on {assignee_name})")
    body = (
        "🔁 **Recurring-throwaway авто-закрыта из review** — "
        "это ежедневный мониторинг-чек (kind:drift|analytics), который "
        "завис в review на человеке. Инстанс одноразовый: любая реальная "
        "регрессия уже завела бы child-задачу, сам чек — просто лог. "
        "Перевёл в **done**, снял с очереди. Следующий инстанс расписания "
        "отработает сам. **the operator decision не нужен.**\n\n"
        "_Авто-close в review-sweep (the operator rule 2026-06-06, leak #2)._"
    )
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/comments"
        data = json.dumps({"body": body}).encode("utf-8")
        req = Request(url, data=data, method="POST",
                      headers={"X-Agent-Key": agent_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201):
                log("review-sweep",
                    f"review-throwaway close comment status {resp.status} for {task_id}")
    except Exception as e:
        log("review-sweep", f"review-throwaway close comment failed for {task_id}: {e}")
    return moved


def auto_park_passive_task(api_url: str, agent_key: str,
                           task_id: str, agent_name: str,
                           count: int, age_hours: float) -> bool:
    """Park an internal verify/monitor/passive-wait task to backlog on a
    stale-respawn loop. No @operator mention, no TG ping — there is no human
    decision pending; the task is idle by design (waiting on a window/clock/
    scheduled reactivation). Tracked in _TRIAGED_AUTO so the respawn
    circuit-breaker fires. the operator rule 2026-06-03."""
    status_id = _resolve_status_id(api_url, agent_key, task_id, "backlog")
    moved = False
    if status_id:
        moved = _dispatch_move(api_url, agent_key, task_id, status_id, "stale-redispatch")
    else:
        log("stale-redispatch",
            f"passive-park: no backlog status_id resolvable for {task_id}")
    if moved:
        log("stale-redispatch", f"passive-parked {task_id} (count={count})")

    body = (
        f"\U0001f17f\ufe0f **\u0410\u0432\u0442\u043e-park (passive-wait)** \u2014 "
        f"\u0437\u0430\u0434\u0430\u0447\u0430 {count}\u00d7 \u0440\u0435\u0441\u043f\u0430\u0432\u043d\u0438\u043b\u0430\u0441\u044c "
        f"\u0430\u0433\u0435\u043d\u0442\u043e\u043c `{agent_name}` (age ~{age_hours:.1f}h) "
        f"\u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f \u0441\u0442\u0430\u0442\u0443\u0441\u0430.\n\n"
        "\u042d\u0442\u043e \u0432\u043d\u0443\u0442\u0440\u0435\u043d\u043d\u044f\u044f verify/monitor "
        "\u0437\u0430\u0434\u0430\u0447\u0430 (label phase:verify | kind:monitor | no-pavel-triage), "
        "\u043e\u043d\u0430 \u0436\u0434\u0451\u0442 \u043e\u043a\u043d\u0430/\u0442\u0430\u0439\u043c\u0435\u0440\u0430 \u2014 "
        "\u0440\u0435\u0448\u0430\u0442\u044c \u043d\u0435\u0447\u0435\u0433\u043e. "
        "\u041f\u0435\u0440\u0435\u0432\u0451\u043b \u0432 **backlog**, respawn \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d. "
        "**the operator decision \u043d\u0435 \u043d\u0443\u0436\u0435\u043d.**\n\n"
        "_\u0410\u0432\u0442\u043e-park \u0432\u043c\u0435\u0441\u0442\u043e auto-triage (the operator rule 2026-06-03)._"
    )
    body = body.encode().decode("unicode_escape")

    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/comments"
        data = json.dumps({"body": body}).encode("utf-8")
        req = Request(url, data=data, method="POST",
                      headers={"X-Agent-Key": agent_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201):
                log("stale-redispatch",
                    f"passive-park comment unexpected status {resp.status} for {task_id}")
    except Exception as e:
        log("stale-redispatch",
            f"passive-park comment failed for {task_id}: {e}")
    return moved


def auto_park_human_verify(api_url: str, agent_key: str,
                           task_id: str, agent_name: str,
                           count: int, age_hours: float) -> bool:
    """Park a human-verify / host-impossible task to backlog on a stale-respawn
    loop. No @operator mention, no TG ping — the agent host literally cannot do it
    (MacBook-only step, human sign-off), and there is NO decision pending. The
    task stays in Mesh, picked up by an interactive human/MacBook session on its
    own clock. Tracked in _TRIAGED_AUTO so the respawn circuit-breaker fires.
    the operator rule 2026-06-06."""
    status_id = _resolve_status_id(api_url, agent_key, task_id, "backlog")
    moved = False
    if status_id:
        moved = _dispatch_move(api_url, agent_key, task_id, status_id, "stale-redispatch")
    else:
        log("stale-redispatch",
            f"human-verify park: no backlog status_id resolvable for {task_id}")
    if moved:
        log("stale-redispatch", f"human-verify parked {task_id} (count={count})")

    body = (
        "\U0001f464 **Авто-park (human-verify / host-impossible)** — "
        f"задача {count}× респавнилась "
        f"агентом `{agent_name}` (age ~{age_hours:.1f}h) без "
        "изменения статуса.\n\n"
        "Это задача для человека / конкретного "
        "хоста (label kind:human-verify | host:macbook), который "
        "headless-агент не может выполнить. "
        "Решать the operatorю нечего — перевёл в **backlog**, respawn остановлен. "
        "Подхватит интерактивная человеческая сессия. "
        "**the operator decision не нужен.**\n\n"
        "_Авто-park вместо auto-triage (the operator rule 2026-06-06)._"
    )
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/comments"
        data = json.dumps({"body": body}).encode("utf-8")
        req = Request(url, data=data, method="POST",
                      headers={"X-Agent-Key": agent_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201):
                log("stale-redispatch",
                    f"human-verify park comment status {resp.status} for {task_id}")
    except Exception as e:
        log("stale-redispatch", f"human-verify park comment failed for {task_id}: {e}")
    return moved


# --- Dependency-blocked / prose-gated park (the operator rule 2026-06-04) -------
# Tasks waiting on another task/PR/phase get spawned, the agent correctly
# no-ops (precondition unmet), status doesn't change, count==3 fires — and the
# old code auto-triaged them to the operator, who has NOTHING to decide. ~39% of the
# triage backlog was this. These belong in backlog, not the operators queue.
# Primary signal = prose-gate phrases in the description (no API call). Formal
# depends_on is checked best-effort (fail-open). Only AGENT-assigned tasks are
# parked — the operator-assigned tasks always pass through to triage.
_GATE_PHRASES = (
    "gate: do after", "do after phase", "do after the phase",
    "после merge", "после мерж", "merged into main", " is merged",
    "depends on:", "blocked on pr", "blocked on #",
    "зависит от wave", "зависит от phase", "после wave", "после phase",
    "не стартовать до", "не начинать до", "gated on the",
)

def _task_prose_gated(t: dict) -> bool:
    """True if the description carries a strong 'waiting on other agent work'
    gate phrase. String-only, no API call."""
    desc = (t.get("description") or "").lower()
    return any(ph in desc for ph in _GATE_PHRASES)

def _fetch_task_deps(api_url: str, agent_key: str, task_id: str) -> list:
    """Fetch a task's formal dependencies, NORMALIZED for human_gate.dep_freeze_reason
    (each row gains the blocker's `blocker_completed_at`). [] on any error → fail-open.

    ⚠️ The transport here is load-bearing and was wrong for as long as the check
    existed. `GET /api/v1/tasks/<id>?include_dependencies=true` does NOT return a
    `dependencies` key at all — not under `?include_dependencies=1`, not under
    `?include=dependencies` (all three probed live 2026-08-02, all 200 OK, none
    carrying the field). The old `_task_formal_blocked` read exactly that field,
    so `deps` was ALWAYS `[]` and the function ALWAYS returned False. That is the
    mechanism behind the "dep-park: 0 firings ever" line in #8f0f9ef6 — the branch
    was not rare, it was unreachable, and a green stub-driven test would have hidden
    it because the stub returns what the real endpoint does not.

    The endpoint that works is the dedicated sub-resource `GET /tasks/<id>/dependencies`,
    and it requires the FULL uuid (a short 8-hex prefix 400s).
    """
    if not task_id or len(str(task_id)) < 36:
        return []  # short id → the endpoint 400s; fail-open rather than guess
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/dependencies"
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=12) as r:
            rows = json.loads(r.read())
        if isinstance(rows, dict):
            rows = rows.get("dependencies") or rows.get("items") or []
        out = []
        for dep in rows or []:
            if (dep.get("dependency_type") or "blocks").lower() not in _DEP_BLOCKING_TYPES:
                continue  # relates_to / is_child_of are cross-refs, not gates
            blocker = dep.get("depends_on_task_id")
            if not blocker:
                continue
            btask = _fetch_task_full(agent_key, blocker, api_url)
            if btask is None:
                continue  # fail-open on this edge — an unreadable blocker never freezes
            out.append({
                "dependency_type": dep.get("dependency_type"),
                "depends_on_task_id": blocker,
                "blocker_completed_at": (btask.get("task") or btask).get("completed_at")
                if isinstance(btask.get("task"), dict) else btask.get("completed_at"),
            })
        return out
    except Exception:
        return []  # fail-open


def _dep_freeze_blocks_feed(agent_name: str, agent_key: str, task_id: str,
                            api_url: str, mention_context: dict = None) -> bool:
    """Second freeze signal (#8f0f9ef6): "frozen, the ask lives on #X".

    The `❓ Blocking @operator` marker freezes the feed AND queues an ask for the operator;
    there was no way to request one without the other, so an agent that correctly
    refused to double-ping the operator silently gave up the freeze too ($87.91 on
    #739ee655). A formal `depends_on` carries the freeze with NO second line in
    the operators queue.

    Same choke point, same rules as _human_gate_blocks_feed:
      * mention/comment wakes pass through — a reply is the un-freeze signal;
      * fail-open on every error (no deps readable → feed).
    """
    if not DEP_FREEZE_ENABLED or mention_context or not task_id:
        return False
    reason = dep_freeze_reason(_fetch_task_deps(api_url, agent_key, task_id))
    if not reason:
        return False
    log(agent_name, f"dep-freeze skip feed #{str(task_id)[:8]} — {reason}")
    return True


def _task_formal_blocked(api_url: str, agent_key: str, task_id: str) -> bool:
    """Best-effort: True if the task has a `blocks` dependency whose blocker is
    not yet done. Fail-open: any error → False (never breaks a spawn cycle).

    Now shares BOTH the transport (_fetch_task_deps) and the rule
    (human_gate.dep_freeze_reason) with the feed-path gate, so the count==3 park
    ladder and the feed gate cannot drift apart."""
    return bool(dep_freeze_reason(_fetch_task_deps(api_url, agent_key, task_id)))


def _task_dep_blocked(api_url: str, agent_key: str, t: dict):
    """Returns (blocked: bool, reason: str). Only agent-assigned tasks qualify —
    the operator-assigned tasks always go to triage (his decision)."""
    if t.get("assignee_type") == "user":
        return False, ""
    if _task_prose_gated(t):
        return True, "prose-gate (waiting on a task/PR/phase, no formal depends_on)"
    if _task_formal_blocked(api_url, agent_key, t.get("id")):
        return True, "formal depends_on blocker not done"
    return False, ""

def auto_park_dep_blocked(api_url: str, agent_key: str,
                          task_id: str, agent_name: str,
                          count: int, age_hours: float, reason: str) -> bool:
    """Park a dependency-blocked / prose-gated task to backlog. No @operator, no TG.
    Recoverable: owner sets a formal depends_on + moves to todo when unblocked."""
    status_id = _resolve_status_id(api_url, agent_key, task_id, "backlog")
    moved = False
    if status_id:
        moved = _dispatch_move(api_url, agent_key, task_id, status_id, "stale-redispatch")
    if moved:
        log("stale-redispatch", f"dep-park {task_id} (count={count}, {reason})")
    body = (
        "\U0001f17f\ufe0f **\u0410\u0432\u0442\u043e-park (dependency-blocked)** \u2014 "
        f"\u0437\u0430\u0434\u0430\u0447\u0430 {count}\u00d7 \u0440\u0435\u0441\u043f\u0430\u0432\u043d\u0438\u043b\u0430\u0441\u044c "
        f"\u0430\u0433\u0435\u043d\u0442\u043e\u043c `{agent_name}` (age ~{age_hours:.1f}h) \u0431\u0435\u0437 "
        "\u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f \u0441\u0442\u0430\u0442\u0443\u0441\u0430.\n\n"
        f"\u041f\u0440\u0438\u0447\u0438\u043d\u0430: **{reason}**. \u0410\u0433\u0435\u043d\u0442 \u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u043e "
        "\u043d\u0435 \u043d\u0430\u0447\u0430\u043b \u2014 \u0436\u0434\u0451\u0442 \u043f\u0440\u0435\u0434\u0443\u0441\u043b\u043e\u0432\u0438\u044f. "
        "\u042d\u0442\u043e **\u043d\u0435 the operator-\u0440\u0435\u0448\u0435\u043d\u0438\u0435** \u2014 \u043f\u0435\u0440\u0435\u0432\u0451\u043b \u0432 **backlog**, respawn \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d.\n\n"
        "\u26a0\ufe0f Owner: \u043f\u043e\u0441\u0442\u0430\u0432\u044c \u0444\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u044b\u0439 `add_dependency` \u043d\u0430 \u0431\u043b\u043e\u043a\u0435\u0440 \u0438 \u0432\u0435\u0440\u043d\u0438 \u0432 todo, "
        "\u043a\u043e\u0433\u0434\u0430 \u0431\u043b\u043e\u043a\u0435\u0440 \u0437\u0430\u043a\u0440\u043e\u0435\u0442\u0441\u044f (\u043f\u0440\u043e\u0437\u0430-\u0433\u0435\u0439\u0442 \u2192 \u0444\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u0430\u044f \u0437\u0430\u0432\u0438\u0441\u0438\u043c\u043e\u0441\u0442\u044c).\n\n"
        "_\u0410\u0432\u0442\u043e-park \u0432\u043c\u0435\u0441\u0442\u043e auto-triage (the operator rule 2026-06-04)._"
    )
    body = body.encode().decode("unicode_escape")
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/comments"
        data = json.dumps({"body": body}).encode("utf-8")
        req = Request(url, data=data, method="POST",
                      headers={"X-Agent-Key": agent_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201):
                log("stale-redispatch", f"dep-park comment status {resp.status} for {task_id}")
    except Exception as e:
        log("stale-redispatch", f"dep-park comment failed for {task_id}: {e}")
    return moved


def _fetch_newest_comments(api_url: str, agent_key: str, task_id: str,
                           page_size: int = 50) -> list:
    """Return the NEWEST page of a card's comments (ascending within the page).

    Mesh serves comments OLDEST-FIRST and paginates. Two things bite here and
    both fail silently, which is why this is not `_fetch_comments_for_gate`:

    * `?limit=N` is ignored — measured 2026-08-04 against the live API, a
      `?limit=5` request on an 18-comment card returned all 18 with
      `page_size=50`. Only `page_size` moves the window.
    * with the default 50, a card carrying more than 50 comments hands back the
      50 OLDEST. For a "has the executor said anything since we last spawned it"
      question that is the exact wrong half, and the newest comment is not
      missing-with-an-error, it is just absent — the answer comes back "no
      progress" and reads like a measurement.

    So: read page 1 for `total_pages`, then fetch the last page if there is more
    than one. Returns [] on any failure — callers must treat empty as "could not
    tell", never as "nothing happened".
    """
    def _page(n: int) -> dict:
        url = (f"{api_url}/api/v1/tasks/{task_id}/comments"
               f"?page_size={page_size}&page={n}")
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    try:
        data = _page(1)
        total_pages = int(data.get("total_pages") or 1)
        if total_pages > 1:
            data = _page(total_pages)
        return data.get("items") or data.get("comments") or []
    except Exception as e:
        log("stale-redispatch",
            f"progress-check: comment fetch failed for {task_id[:8]}: {e}")
        return []


def _executor_progress_since(api_url: str, agent_key: str, task: dict,
                             agent_name: str, since_epoch):
    """Did the card's executor say anything after the last spawn? (#19d9a1d1)

    Returns `(progressed: bool, reason: str)`.

    WHY THIS SIGNAL and not a bigger one:

    * `status changed` — the thing the give-up test uses today — is the wrong
      end of the work. A session that is reading, editing, committing and
      commenting does not touch status until it hands off, so "status unchanged"
      is the NORMAL state of a healthy in-flight card, not a symptom.
    * `updated_at moved` is worse than useless as a progress signal: the
      dispatcher's own respawn causes a checkout, and a checkout bumps
      `updated_at`. The counter would then be reading its own noise, which is
      the defect this card is about.
    * a comment authored BY THE ASSIGNEE, after the last spawn, with the driver
      markers filtered out, cannot be produced by the dispatcher. Somebody with
      the card's context wrote prose. It is one cheap read, it is already the
      fleet's convention for "leave evidence on the thread", and it is exactly
      what the nine mis-parked cards had (13, 11 and 6 comments respectively,
      the newest minutes before the park).

    Deliberately NOT counted as progress:
    * automated comments (`_is_automated_comment`) and this file's own park
      notice — otherwise the dispatcher's escalation prose would prove the card
      alive and no card could ever be parked;
    * comments by anyone other than the assignee — a reviewer or the creator
      commenting says nothing about whether the executor is running.

    `since_epoch` is None when no spawn has been recorded (fresh process, no
    persisted stamp). Then the question "since the last spawn" has no referent,
    so this returns False: the check declines to claim progress rather than
    inventing a window, and the card follows the pre-fix path.
    """
    if since_epoch is None:
        return (False, "no recorded spawn to measure from")
    assignee = (task.get("assignee_name") or agent_name or "").strip().lower()
    if not assignee:
        return (False, "no assignee name on the card")
    comments = _fetch_newest_comments(api_url, agent_key, task.get("id") or "")
    if not comments:
        return (False, "no comments readable")
    newest_ts, newest_author = None, None
    for c in comments:
        body = c.get("body") or ""
        author = (c.get("author_name") or "").strip().lower()
        if author != assignee:
            continue
        if _is_automated_comment(body) or _DISPATCHER_PARK_SENTINEL in body:
            continue
        dt = _parse_iso_utc(c.get("created_at"))
        if dt is None:
            continue
        ts = dt.replace(tzinfo=timezone.utc).timestamp()
        if newest_ts is None or ts > newest_ts:
            newest_ts, newest_author = ts, c.get("author_name")
    if newest_ts is None:
        return (False, f"no non-automated comment by @{assignee}")
    delta = newest_ts - since_epoch
    if delta <= 0:
        return (False,
                f"newest @{newest_author} comment is {-delta / 60:.0f} min OLDER "
                "than the last spawn")
    return (True,
            f"@{newest_author} commented {delta / 60:.0f} min after the last spawn")


def auto_triage_to_creator(api_url: str, agent_key: str,
                          task_id: str, agent_name: str,
                          creator_name: str,
                          count: int, age_hours: float) -> None:
    """On count==3, no human-gate: move_task → triage + notify task creator.

    Routes to created_by_name instead of @operator when no explicit human-gate
    signal (label 'blocked:pavel' or ❓Blocking @operator comment) is present.
    The creator (often an orchestrator agent) can redirect or unblock without
    involving the operator. No TG nag is sent — this is not a the operator decision.
    (B3·dq fix 2026-06-08)
    """
    status_id = _resolve_status_id(api_url, agent_key, task_id, "triage")
    moved = False
    if status_id:
        moved = _dispatch_move(api_url, agent_key, task_id, status_id, "stale-redispatch")
    else:
        log("stale-redispatch",
            f"creator-triage: no triage status_id resolvable for {task_id}")
    if moved:
        log("stale-redispatch",
            f"creator-triaged {task_id} → @{creator_name} (count={count})")

    moved_line = (
        "Я перевёл задачу в статус **triage** — увидишь её в Mesh UI."
        if moved else
        "⚠️ Не смог автоматически перевести в **triage** (ошибка перемещения) — "
        "задача осталась в текущем статусе."
    )
    body = (
        # Machine sentinel FIRST (#61ad469a): this comment is signed with the agent's
        # own key, so when creator == assignee the self-mention loop guard would drop
        # the only notification this card ever gets. See _is_dispatcher_park_notify —
        # it requires the sentinel in leading position, so keep it at index 0.
        f"{_DISPATCHER_PARK_SENTINEL}\n"
        f"@{creator_name} ⚠️ **Авто-triage** — "
        f"задача {count}× респавнилась агентом "
        f"`{agent_name}` (age ~{age_hours:.1f}h) без изменения статуса. "
        f"{moved_line}\n\n"
        "Нужно: либо (а) дать input/разблокировку "
        "и вернуть в in_progress, "
        "либо (б) закрыть как done/cancelled, "
        "либо (в) переназначить другому агенту.\n\n"
        "_Диспетчер перестал respawn'ить эту задачу (count==3). "
        "Маршрут: creator — нет human-gate сигнала "
        "(label `blocked:pavel` или ❓Blocking @operator в комментарии), "
        "поэтому @operator не тревожится. (B3·dq fix)_"
    )
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/comments"
        data = json.dumps({"body": body}).encode("utf-8")
        req = Request(url, data=data, method="POST",
                      headers={"X-Agent-Key": agent_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201):
                log("stale-redispatch",
                    f"creator-triage comment unexpected status {resp.status} for {task_id}")
    except Exception as e:
        log("stale-redispatch", f"creator-triage comment failed for {task_id}: {e}")


def auto_triage_with_pavel_mention(api_url: str, agent_key: str,
                                  task_id: str, agent_name: str,
                                  count: int, age_hours: float) -> None:
    """On count==3: move_task → triage + post @operator mention comment.

    Surfaces stuck task to the operator via Mesh UI (Activity feed + status change).
    Fires once per task (tracked in _TRIAGED_AUTO). Best-effort.

    Order matters (task 75de4532 fix): we MOVE first (using the resolved
    status_id — the REST /move endpoint rejects status_slug with HTTP 400
    "status_id or position is required"), THEN post a comment whose text
    reflects whether the move actually succeeded. The old code did the reverse
    and hard-coded "Я переместил в triage", so every comment lied because the
    slug-based move always 400'd.
    """
    # 1) MOVE first, using the resolved status_id (slug is rejected by REST).
    status_id = _resolve_status_id(api_url, agent_key, task_id, "triage")
    moved = False
    if status_id:
        moved = _dispatch_move(api_url, agent_key, task_id, status_id, "stale-redispatch")
    else:
        log("stale-redispatch",
            f"auto-triage: no triage status_id resolvable for {task_id}")
    if moved:
        log("stale-redispatch", f"auto-triaged {task_id} (count={count})")

    # 2) COMMENT after \u2014 text reflects the ACTUAL outcome (never claim a move
    #    that didn't happen). Respawns are stopped by the caller (_TRIAGED_AUTO)
    #    regardless, so we say so honestly either way.
    moved_line = (
        "\u042f \u043f\u0435\u0440\u0435\u0432\u0451\u043b \u0437\u0430\u0434\u0430\u0447\u0443 \u0432 \u0441\u0442\u0430\u0442\u0443\u0441 **triage** \u2014 \u0443\u0432\u0438\u0434\u0438\u0448\u044c \u0435\u0451 \u0432 Mesh UI."
        if moved else
        "\u26a0\ufe0f \u041d\u0435 \u0441\u043c\u043e\u0433 \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438 \u043f\u0435\u0440\u0435\u0432\u0435\u0441\u0442\u0438 \u0432 **triage** (\u043e\u0448\u0438\u0431\u043a\u0430 \u043f\u0435\u0440\u0435\u043c\u0435\u0449\u0435\u043d\u0438\u044f) \u2014 "
        "\u0437\u0430\u0434\u0430\u0447\u0430 \u043e\u0441\u0442\u0430\u043b\u0430\u0441\u044c \u0432 \u0442\u0435\u043a\u0443\u0449\u0435\u043c \u0441\u0442\u0430\u0442\u0443\u0441\u0435."
    )
    body = (
        f"@operator \u26a0\ufe0f **\u0410\u0432\u0442\u043e-triage** \u2014 \u0437\u0430\u0434\u0430\u0447\u0430 {count}\u00d7 \u0440\u0435\u0441\u043f\u0430\u0432\u043d\u0438\u043b\u0430\u0441\u044c \u0430\u0433\u0435\u043d\u0442\u043e\u043c "
        f"`{agent_name}` (age ~{age_hours:.1f}h) \u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f \u0441\u0442\u0430\u0442\u0443\u0441\u0430. "
        f"{moved_line}\n\n"
        "\u041d\u0443\u0436\u043d\u043e \u0440\u0435\u0448\u0435\u043d\u0438\u0435: \u043b\u0438\u0431\u043e (\u0430) \u0434\u0430\u0442\u044c \u0438\u043d\u043f\u0443\u0442/\u0440\u0430\u0437\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u043a\u0443 \u0438 \u0432\u0435\u0440\u043d\u0443\u0442\u044c "
        "\u0432 in_progress, \u043b\u0438\u0431\u043e (\u0431) \u0437\u0430\u043a\u0440\u044b\u0442\u044c \u043a\u0430\u043a done/cancelled, \u043b\u0438\u0431\u043e "
        "(\u0432) \u043f\u0435\u0440\u0435\u043d\u0430\u0437\u043d\u0430\u0447\u0438\u0442\u044c \u0434\u0440\u0443\u0433\u043e\u043c\u0443 \u0430\u0433\u0435\u043d\u0442\u0443.\n\n"
        "_\u0414\u0438\u0441\u043f\u0435\u0442\u0447\u0435\u0440 \u043f\u0435\u0440\u0435\u0441\u0442\u0430\u043b respawn'\u0438\u0442\u044c \u044d\u0442\u0443 \u0437\u0430\u0434\u0430\u0447\u0443 (count==3)._"
    )
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/comments"
        data = json.dumps({"body": body}).encode("utf-8")
        req = Request(url, data=data, method="POST",
                      headers={"X-Agent-Key": agent_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201):
                log("stale-redispatch",
                    f"auto-triage comment unexpected status {resp.status} for {task_id}")
    except Exception as e:
        log("stale-redispatch", f"auto-triage comment failed for {task_id}: {e}")


def stale_redispatcher_loop(agents_cfg: list, api_url: str):
    """Periodic scan: re-fire dispatch on any in_progress task whose
    updated_at is older than STALE_THRESHOLD_SEC. Honors dedup 31bb7aad
    (alive session => claim_dispatch refuses, no duplicate spawn) and a
    per-task respawn cooldown so a session that keeps dying immediately
    doesn't get hammered every cycle.
    """
    from datetime import datetime, timezone
    log("stale-redispatch",
        f"thread started; threshold={STALE_THRESHOLD_SEC}s "
        f"interval={STALE_CHECK_INTERVAL_SEC}s "
        f"cooldown={STALE_RESPAWN_COOLDOWN_SEC}s "
        f"repo_unsafe_retry={REPO_UNSAFE_RETRY_SEC}s")
    # Give listener threads a beat to recover live PIDs from a launchd
    # reload before we start judging staleness.
    time.sleep(60)
    while True:
        try:
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            mono = time.monotonic()
            for agent in agents_cfg:
                name = agent["name"]
                tasks = fetch_in_progress_tasks(agent["agent_key"], api_url)
                # Also include triage/todo so a missed task.assigned (SSE
                # delivery isn't durable across reconnects) doesn't leave an
                # assigned task forgotten. `review` excluded (task 9090b3fc).
                open_extra = fetch_open_tasks(agent["agent_key"], api_url)
                if open_extra:
                    tasks = list(tasks or []) + open_extra
                # Also fetch todo tasks if we have repo-unsafe aborts pending —
                # those tasks were returned to todo and need fast retry.
                with _REPO_UNSAFE_LOCK:
                    pending_repo_unsafe = set(_REPO_UNSAFE.keys())
                if pending_repo_unsafe:
                    todo = fetch_todo_tasks(agent["agent_key"], api_url)
                    repo_unsafe_todos = [t for t in (todo or [])
                                         if t.get("id") in pending_repo_unsafe]
                    if repo_unsafe_todos:
                        tasks = list(tasks or []) + repo_unsafe_todos
                for t in tasks or []:
                    tid = t.get("id")
                    if not tid:
                        continue
                    # Skip tasks in terminal or await-human status (task 9090b3fc).
                    # Belt-and-suspenders: fetch_open_tasks no longer returns
                    # review/done/cancelled, but guard here in case status changed
                    # between fetch and this decision point.
                    _cat = t.get("_status_category", "in_progress")
                    if _cat in _SKIP_CATEGORIES:
                        log("stale-redispatch",
                            f"dispatcher: skip respawn agent={name} task={tid[:8]} "
                            f"reason=status_{_cat}")
                        continue
                    # #aca99f88: the card is in a FEEDABLE status. If it still
                    # carries a count==3 latch, something moved it back out of
                    # the parking lane (a human, the triage-drain healer) or the
                    # park move never landed — either way the latch is now the
                    # only thing keeping it dead. Release BEFORE the due_date
                    # gate below so a staggered card is re-armed on schedule
                    # rather than staying latched until its due date passes.
                    _release_latch_if_feedable(tid, _cat, name)
                    # Skip scheduled tasks (added 2026-05-22). If due_date is
                    # in the future, the task is intentionally waiting — not stuck.
                    due = t.get("due_date")
                    if due:
                        due_dt = _parse_iso_utc(due)
                        if due_dt and due_dt > now_utc:
                            continue
                    updated = _parse_iso_utc(t.get("updated_at"))
                    if updated is None:
                        continue
                    age = (now_utc - updated).total_seconds()
                    # Race-aborted tasks: shorter threshold (REPO_UNSAFE_RETRY_SEC).
                    # Crash-suspect tasks: even shorter (CRASH_RETRY_SEC).
                    # Real stale tasks: full STALE_THRESHOLD_SEC (30min, B5.3).
                    with _REPO_UNSAFE_LOCK:
                        repo_unsafe_ts = _REPO_UNSAFE.get(tid)
                        crash_retry_ts = _CRASH_RETRY.get(tid)
                    if crash_retry_ts is not None:
                        since_crash = mono - crash_retry_ts
                        with _REPO_UNSAFE_LOCK:
                            crash_n = _CRASH_COUNT.get(tid, 1)
                        crash_delay = _crash_retry_delay(crash_n)
                        if since_crash < crash_delay:
                            continue
                    if repo_unsafe_ts is not None:
                        # use REPO_UNSAFE_RETRY_SEC since the abort, not since
                        # task updated_at — abort sets a fresh marker
                        since_abort = mono - repo_unsafe_ts
                        if since_abort < REPO_UNSAFE_RETRY_SEC:
                            continue
                    else:
                        # Per-task threshold (todo=30min, in_progress=4h)
                        threshold = t.get("_stale_threshold", STALE_THRESHOLD_SEC)
                        if age < threshold:
                            continue
                    # Capacity / pause gate (the operator 2026-05-23): a task blocked purely
                    # because there's no free slot OR the dispatcher is paused (incl.
                    # auto rate-limit PAUSE_ALL) must NOT accrue respawn-count or fire
                    # auto-triage / TG spam — the agent never actually ran, dispatch
                    # would just SKIP/defer below. Skip this cycle WITHOUT counting; it
                    # retries cleanly once a slot frees / pause lifts, so count==3 again
                    # means a genuinely stuck agent (ran 3×), not a slot/pause shortage.
                    # (Repo-unsafe retries keep their own path above, unaffected.)
                    _paused_now, _pause_reason = _is_paused(name)
                    _stale_per_cap = (agent.get("max_concurrent") or MAX_PER_AGENT_SPAWNS)
                    if repo_unsafe_ts is None and (
                            _paused_now
                            or _live_active_count() >= MAX_CONCURRENT_SPAWNS
                            or _live_per_agent_count(name) >= _stale_per_cap):
                        log("stale-redispatch",
                            f"defer {name}/{tid[:8]} — "
                            f"{_pause_reason or 'at capacity'} "
                            f"({_live_active_count()}/{MAX_CONCURRENT_SPAWNS}, "
                            f"agent {_live_per_agent_count(name)}/{_stale_per_cap}); "
                            "not counted as respawn")
                        continue
                    with _STALE_LOCK:
                        last = _STALE_LAST.get(tid)
                        # Per-task cooldown: todo gets fast retry, others full.
                        if repo_unsafe_ts is not None:
                            cooldown = REPO_UNSAFE_RETRY_SEC
                        elif t.get("_stale_threshold") == TODO_STALE_THRESHOLD_SEC:
                            cooldown = TODO_STALE_THRESHOLD_SEC
                        else:
                            cooldown = STALE_RESPAWN_COOLDOWN_SEC
                        if last is not None and mono - last < cooldown:
                            continue
                        _STALE_LAST[tid] = mono
                        # opportunistic prune of long-stale tracker entries
                        for k in [k for k, ts in _STALE_LAST.items()
                                  if mono - ts > STALE_RESPAWN_COOLDOWN_SEC * 4]:
                            _STALE_LAST.pop(k, None)
                            _STALE_COUNTS.pop(k, None)
                            _STALE_NAGGED.discard(k)
                            _TG_NAGGED.discard(k)
                            _TRIAGED_AUTO.discard(k)
                            # Keep the stamp map from outliving the latch it
                            # describes. This loop is no longer the only path
                            # that clears a latch (#aca99f88) — it cannot be,
                            # since it iterates the memory-only _STALE_LAST.
                            _LATCH_TS.pop(k, None)
                            _LATCH_RELEASES.pop(k, None)
                            # Same reason as the entries above: these two are
                            # keyed by the card and would otherwise outlive
                            # every other trace of it (#19d9a1d1).
                            _LAST_SPAWN_WALL.pop(k, None)
                            _PROGRESS_REPRIEVES.pop(k, None)
                    title = (t.get("title") or "").replace("\n", " ")
                    # Jitter (added 2026-05-21): spread spawns post-restart to
                    # avoid thundering herd → API rate-limit.
                    if SPAWN_JITTER_SEC > 0:
                        import random as _r
                        time.sleep(_r.uniform(0, SPAWN_JITTER_SEC))
                    # enforce=False: this loop owns the ESCALATION ACTION, so it
                    # must keep counting even at or above the ceiling — its own
                    # `tid in _TRIAGED_AUTO` circuit breaker below is what stops
                    # the dispatch. See `_respawn_budget`'s path table.
                    # Read the PREVIOUS spawn stamp before consuming an attempt:
                    # `_respawn_budget` overwrites it with `now`, and comparing a
                    # comment against a spawn that is happening in this very
                    # iteration would make progress unobservable by construction
                    # (#19d9a1d1).
                    with _STALE_LOCK:
                        _prev_spawn_wall = _LAST_SPAWN_WALL.get(tid)
                    _ok, nag_count, _stale_token = _respawn_budget(
                        name, tid, "stale-redispatch", enforce=False)
                    with _STALE_LOCK:
                        nag_already = tid in _STALE_NAGGED
                    log("stale-redispatch",
                        f"stale {name}/{tid} '{title[:60]}' "
                        f"age={age/3600:.1f}h count={nag_count} -> re-dispatching")
                    # Escalation ladder (revised 2026-05-22 after the operator feedback):
                    #   count==2 → Mesh nag-comment (1x per task)
                    #   count==3 → TG ping + auto-move to triage + @operator mention
                    #              (triage status takes task OUT of stale-redispatch loop,
                    #               so no more respawns until the operator acts)
                    #   count>=4 → silent (shouldn't happen given triage skip)
                    # Repo-unsafe retries do not count as "stuck" — skip all escalation.
                    # ⚠️ These are >=, not ==, and that is load-bearing now that
                    # the ladder is shared (#3788c8f0). With one writer the
                    # counter could only ever arrive here having just stepped by
                    # exactly 1, so `== 3` was safe. With six paths incrementing
                    # it, another path can carry the count from 2 to 3 between
                    # two passes of this loop — this loop then sees 4, `== 3` is
                    # false, and the card respawns FOREVER without ever
                    # escalating. That is the #8b5f818a mirror defect, and it is
                    # exactly the failure a shared budget would otherwise
                    # introduce. Idempotence is preserved by the guards that
                    # were already here (`nag_already`, `not in _TRIAGED_AUTO`),
                    # not by the equality.
                    if repo_unsafe_ts is None:
                        if (nag_count >= 2 and not nag_already
                                and nag_count < RESPAWN_LADDER_MAX):
                            post_dispatcher_nag(api_url, agent["agent_key"], tid,
                                                name, nag_count, age / 3600)
                            with _STALE_LOCK:
                                _STALE_NAGGED.add(tid)
                        elif nag_count >= RESPAWN_LADDER_MAX and tid not in _TRIAGED_AUTO:
                            with _STALE_LOCK:
                                tg_already = tid in _TG_NAGGED
                                triaged_already = tid in _TRIAGED_AUTO
                            # FREEZE GUARD (P3 #9, 2026-06-16): a human-gated task
                            # must NEVER be silently auto-parked/closed — human_gate
                            # wins over EVERY passive-park branch (freeze rule). A
                            # task carrying phase:verify | kind:monitor that is ALSO
                            # human-gated (server human_gate flag per PR #258, a gate
                            # label, or a ❓Blocking @operator comment) used to hit
                            # _is_passive_wait FIRST and get buried in backlog, never
                            # reaching the operator. Evaluate the gate ONCE up front; gated →
                            # route to the operator and skip all park/close branches below.
                            _gate = _has_human_gate_signal(
                                api_url, agent["agent_key"], t)
                            if _gate:
                                if not triaged_already:
                                    auto_triage_with_pavel_mention(
                                        api_url, agent["agent_key"], tid,
                                        name, nag_count, age / 3600)
                                    with _STALE_LOCK:
                                        _latch_add(tid)
                                if not tg_already:
                                    tg_body = (
                                        f"⚠️ #{tid[:8]} ({name}) auto-triaged "
                                        f"(human-gate) after {nag_count}× respawn, "
                                        f"age={age/3600:.1f}h. "
                                        f"https://mesh.example.com/t/{tid}"
                                    )
                                    _post_tg_nag(TG_NAG_CHAT_ID, tg_body)
                                    with _STALE_LOCK:
                                        _TG_NAGGED.add(tid)
                                _respawn_budget_settle(_stale_token)
                                continue
                            # PROGRESS REPRIEVE (#19d9a1d1). Everything below
                            # this point parks the card. The ladder that got us
                            # here counted RESPAWNS — 64% of them fired by
                            # `pull-on-reap`, i.e. by the dispatcher's own
                            # reap-and-refill — so before any park branch runs,
                            # ask the one question the ladder never asked: has
                            # the executor said anything since the last spawn.
                            #
                            # Placed AFTER the freeze guard on purpose: a
                            # human-gated card must reach the operator on schedule, and
                            # a reprieve there would delay a blocking ask by up
                            # to three more rungs. Placed BEFORE throwaway /
                            # passive-wait / human-verify / dep-park because
                            # every one of those buries a card that is, by this
                            # measurement, being actively worked.
                            #
                            # Bounded by PROGRESS_REPRIEVE_MAX. A card that
                            # comments on every rung without finishing still
                            # lands in the parking lane; a fix that emptied
                            # `triage` outright would be a disabled watchdog,
                            # not a fix.
                            with _STALE_LOCK:
                                _reprieves = _PROGRESS_REPRIEVES.get(tid, 0)
                            if _reprieves < PROGRESS_REPRIEVE_MAX:
                                _progressed, _why = _executor_progress_since(
                                    api_url, agent["agent_key"], t, name,
                                    _prev_spawn_wall)
                                if _progressed:
                                    # REFUND, not settle: this branch returns
                                    # before `dispatch_claude`, so the attempt
                                    # never reached Popen. Settling would leave
                                    # `_LAST_SPAWN_WALL` pointing at a spawn
                                    # that did not happen, and the next rung
                                    # would measure progress against a fiction.
                                    #
                                    # bump → own? → refund → reset → restamp is
                                    # ONE critical section (#ee63bb07). Asking
                                    # who owns the cooldown stamp and then acting
                                    # on the answer after the lock was released is
                                    # a TOCTOU: the refund's own CAS would decline
                                    # correctly, and the reset would clobber the
                                    # sibling anyway one call later. The network
                                    # call above stays outside the lock — see the
                                    # helper's docstring.
                                    _respawn_budget_reprieve(
                                        _stale_token, tid, _reprieves + 1,
                                        mono, _why)
                                    log("stale-redispatch",
                                        f"progress reprieve {name}/{tid[:8]} — "
                                        f"{_why}; ladder reset, NOT parked "
                                        f"(reprieve {_reprieves + 1}/"
                                        f"{PROGRESS_REPRIEVE_MAX})")
                                    continue
                                log("stale-redispatch",
                                    f"no progress {name}/{tid[:8]} — {_why}; "
                                    "proceeding to park")
                            else:
                                log("stale-redispatch",
                                    f"progress reprieve EXHAUSTED {name}/{tid[:8]} "
                                    f"({_reprieves}/{PROGRESS_REPRIEVE_MAX}) — "
                                    "parking regardless of thread activity")
                            # THROWAWAY-AUTOCLOSE branch (added 2026-05-31):
                            # recurring monitoring checks (drift / analytics)
                            # auto-close instead of pestering the operator.
                            if _is_recurring_throwaway(t):
                                if not triaged_already:
                                    auto_close_throwaway_task(
                                        api_url, agent["agent_key"], tid,
                                        name, nag_count, age / 3600)
                                    with _STALE_LOCK:
                                        _latch_add(tid)
                                # No TG ping for throwaway closures
                                _respawn_budget_settle(_stale_token)
                                continue
                            # PASSIVE-WAIT-PARK branch (the operator rule 2026-06-03):
                            # internal verify/monitor tasks waiting on a
                            # window/clock park to backlog instead of pinging
                            # the operator — nothing to triage, no human decision.
                            if _is_passive_wait(t):
                                if not triaged_already:
                                    auto_park_passive_task(
                                        api_url, agent["agent_key"], tid,
                                        name, nag_count, age / 3600)
                                    with _STALE_LOCK:
                                        _latch_add(tid)
                                # No TG ping for passive-wait parks
                                _respawn_budget_settle(_stale_token)
                                continue
                            # HUMAN-VERIFY-PARK branch (the operator rule 2026-06-06):
                            # kind:human-verify / host:macbook tasks the agent
                            # host can't satisfy — park to backlog instead of
                            # auto-triaging to the operator. No decision pending; an
                            # interactive human session picks them up.
                            if _is_human_verify(t):
                                if not triaged_already:
                                    auto_park_human_verify(
                                        api_url, agent["agent_key"], tid,
                                        name, nag_count, age / 3600)
                                    with _STALE_LOCK:
                                        _latch_add(tid)
                                # No TG ping for human-verify parks
                                _respawn_budget_settle(_stale_token)
                                continue
                            # DEPENDENCY-PARK branch (the operator rule 2026-06-04):
                            # agent-assigned tasks waiting on another task/PR/
                            # phase (prose-gate or formal depends_on) park to
                            # backlog instead of pinging the operator — nothing to triage.
                            _dep_blocked, _dep_reason = _task_dep_blocked(
                                api_url, agent["agent_key"], t)
                            if _dep_blocked:
                                if not triaged_already:
                                    auto_park_dep_blocked(
                                        api_url, agent["agent_key"], tid,
                                        name, nag_count, age / 3600, _dep_reason)
                                    with _STALE_LOCK:
                                        _latch_add(tid)
                                # No TG ping for dependency parks
                                _respawn_budget_settle(_stale_token)
                                continue
                            # Non-gated fall-through: the human-gate was already
                            # evaluated up front (FREEZE GUARD) and is False here \u2014
                            # the task is a plain impl task that isn't throwaway /
                            # passive-wait / human-verify / dep-blocked. Route to the
                            # creator (NOT @operator) so the operator isn't pinged on pure impl
                            # tasks; no TG nag for creator-routed escalations.
                            if not triaged_already:
                                _creator = t.get("created_by_name") or "task-creator"
                                auto_triage_to_creator(
                                    api_url, agent["agent_key"], tid,
                                    name, _creator, nag_count, age / 3600)
                                with _STALE_LOCK:
                                    _latch_add(tid)
                    # Circuit breaker (task 9dd40e25): once a task has been
                    # auto-triaged (count==3 → moved to the human parking lane),
                    # do NOT respawn it — not even the final spawn in this very
                    # iteration. Previously the code auto-triaged AND then still
                    # dispatched once more; combined with triage being re-fetched
                    # every 4h, a permanently-blocked task respawned indefinitely
                    # (963f7e95: 39 dispatches). It now waits for a human to move
                    # it back to todo/in_progress.
                    if tid in _TRIAGED_AUTO:
                        log("stale-redispatch",
                            f"skip respawn {name}/{tid[:8]} — auto-triaged, parked for human")
                        # Counted, then parked: the increment stands, drop the token.
                        _respawn_budget_settle(_stale_token)
                        continue
                    try:
                        dispatch_claude(
                            agent_name=name,
                            agent_key=agent["agent_key"],
                            workspace=agent["workspace"],
                            model=agent.get("model", "sonnet"),
                            task_id=tid,
                            task_title=title,
                            api_url=api_url,
                            env_file=agent.get("env_file"),
                            repos=resolve_repos(agent),
                            claude_env_file=_resolve_claude_env_file(agent),
                            budget_token=_stale_token,
                        )
                    except Exception as e:
                        log("stale-redispatch",
                            f"dispatch_claude crashed for {name}/{tid}: {e}")
        except Exception as e:
            log("stale-redispatch", f"loop error: {e}")
        _save_counters()  # P2 #7: persist stale-circuit counters once per scan pass
        time.sleep(STALE_CHECK_INTERVAL_SEC)


def listen_agent(name: str, agent_key: str, workspace: str, model: str, api_url: str,
                 env_file: str = None, repos: list = None, claude_env_file: str = None):
    """SSE listener loop for one agent."""
    url = f"{api_url}/api/v1/agents/me/events/stream"

    while True:
        try:
            headers = {
                "X-Agent-Key": agent_key,
                "Accept": "text/event-stream",
            }
            cursor = _read_cursor(name)
            if cursor:
                headers["Last-Event-ID"] = cursor
                log(name, f"Connecting with cursor {cursor[:8]}...")
            else:
                log(name, "Connecting to SSE stream...")

            req = Request(url, headers=headers)
            try:
                resp = urlopen(req, timeout=300)
            except HTTPError as e:
                if e.code == 410:
                    log(name, "410 Gone — cursor expired, full recovery")
                    _delete_cursor(name)
                    # Immediate poll of triage/todo to recover missed tasks
                    # The FIFTH re-entry path, and it was the least bounded of
                    # all: a cursor expiry re-dispatches the ENTIRE open backlog
                    # in one burst, with no cooldown, no latch check and no
                    # ladder (#3788c8f0). It contributed 0 to the measured 226
                    # surplus only because it fired once in five weeks
                    # (2026-07-01) and `fetch_open_tasks` happened to return
                    # empty that time — latent, not safe. A 410 during a busy
                    # backlog would have re-spawned every open card at once.
                    for t in fetch_open_tasks(agent_key, api_url):
                        tid = t.get("id", "")
                        ttitle = t.get("title", "")
                        _ok, _count, _token = _respawn_budget(
                            name, tid, "410-recovery")
                        if not _ok:
                            continue
                        log(name, f"410-recovery: dispatching {tid}")
                        dispatch_claude(name, agent_key, workspace, model, tid, ttitle,
                                        api_url, env_file, repos,
                                        budget_token=_token)
                    log(name, "Reconnecting in 10s after 410 recovery...")
                    time.sleep(10)
                    continue
                raise

            # Per-event state; reset on blank line (SSE event boundary)
            current_id: str | None = None
            server_sends_ids = False  # backward-compat feature-detect

            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")

                if line.startswith("id:"):
                    current_id = line[3:].strip()
                    server_sends_ids = True
                    continue

                if not line.startswith("data:"):
                    if not line:
                        # Blank line = SSE event boundary
                        current_id = None
                    continue

                json_str = line[5:].strip()
                if not json_str:
                    continue

                # Advance cursor BEFORE processing — ensures crash mid-handle
                # still advances our position (no re-deliver on next reconnect).
                if current_id and server_sends_ids:
                    _write_cursor(name, current_id)

                try:
                    event = json.loads(json_str)
                except json.JSONDecodeError:
                    log(name, f"Bad JSON: {json_str[:100]}")
                    current_id = None
                    continue

                event_type = event.get("event_type", "")
                task_id = event.get("task_id", "")
                task_title = event.get("title", "")
                log(name, f"Event: {event_type} | {task_title} ({task_id})")

                if event_type in ("task.assigned", "task.created"):
                    # Scheduled-task gate — the FIFTH and last feed path (#ba50c1a2).
                    # The reaper and stale-redispatch loops have skipped future-dated
                    # cards for days (35 logged `pull-on-reap: skip ba50c1a2` between
                    # 07-29 and 07-30), yet the card was still respawned: parking it
                    # emits task.assigned (move_task re-stamps the assignee), and THIS
                    # branch dispatched on the event without ever reading due_date.
                    # So the act of parking re-armed the spawn — a self-perpetuating
                    # loop at ~$22 a session, hitting precisely the cards that were
                    # scheduled correctly. Gating the other paths could not fix it
                    # because this one re-fires one second after every park.
                    #
                    # Measured 2026-07-30 across the live fleet DB: of 75 dated tasks
                    # only 4 carry a FUTURE due_date (2 Backlog, 2 Done) and ZERO sit
                    # in Todo/In Progress — so this cannot mute dated work at large.
                    # Narrow by construction: a PAST due date, an undated task, and an
                    # unparseable one all fail OPEN and dispatch as before.
                    _sched = fetch_task_scheduled_until(agent_key, task_id, api_url)
                    if _sched:
                        log(name, f"{event_type}: skip {task_id[:8]} "
                                  f"reason=scheduled_until_{_sched}")
                        continue
                    if event_type == "task.created":
                        # Umbrella/captain dedup backstop (task a5c444b4) — fetch
                        # labels once, fire a detached warning only for captain/
                        # umbrella scope. Never blocks dispatch.
                        _lbls, _ttl, _, _, _ = fetch_task_meta(agent_key, task_id, api_url)
                        _dedup_warn_on_create(name, agent_key, api_url, task_id,
                                              _lbls, _ttl or task_title)
                    # B1.4: delegation=review-trap guard — if this task has
                    # status=review, stale-redispatch will never retry it on failure.
                    # Post a one-time comment so the creator can move it to todo.
                    threading.Thread(
                        target=_warn_delegation_review_trap,
                        args=(name, agent_key, api_url, task_id),
                        daemon=True,
                    ).start()
                    # Status gate — THIRD site of _SKIP_CATEGORIES (#5d821586).
                    # The frozenset is enforced on both sweep paths (:3737,
                    # :4433) and was bypassed here, so an assigned card spawned
                    # a live session regardless of status — including `backlog`,
                    # which this file's own policy declares non-dispatchable.
                    # Demonstrated on #023ddc30: created in Lab at 08:22:46Z in
                    # a backlog status, `claude pid=72130 registered` at
                    # 08:22:47 — ONE second, never having left backlog.
                    #
                    # Why it is not merely a stray spawn: `--park`-to-backlog is
                    # the fleet's documented "stop working this" lever and the
                    # agent-eval harness relies on it to hold golden fixtures
                    # dormant between seed and promote. It never held for an
                    # ASSIGNED card, so a seeded fixture could be worked before
                    # its own liveness measurement started, and the step-2
                    # time-to-first-spawn figure measured an agent that had
                    # already run. Nearly corrupted a live negative control on
                    # 07-28 (a session recognised the fixture and withheld the
                    # write — luck, not a gate).
                    #
                    # Placed AFTER the review-trap warning deliberately: a card
                    # arriving with status=review still needs that comment —
                    # more so now, since it no longer gets the one SSE spawn
                    # that used to paper over the trap. No comment is posted for
                    # `backlog`: it is a park target, commenting on every parked
                    # card would be noise, and a comment SETTLES an eval fixture
                    # (see `run-golden.py::_unsettled`) — the gate must not
                    # disturb the controls it exists to protect.
                    #
                    # Fails OPEN: fetch_task_status_category returns "" on any
                    # error, and "" is not in _SKIP_CATEGORIES.
                    _cat = fetch_task_status_category(agent_key, task_id, api_url)
                    if _cat in _SKIP_CATEGORIES:
                        log(name, f"{event_type}: skip {task_id[:8]} "
                                  f"reason=status_{_cat}")
                        continue
                    # CONSULT-only, deliberately: 144 first-spawns against 7
                    # surplus says the per-assignment model is healthy, and
                    # charging a genuine new assignment to the ladder would park
                    # cards for being worked normally. It still consults so a
                    # park-emitted `task.assigned` — `move_task` re-stamps the
                    # assignee, which is how parking a card used to re-arm its
                    # own spawn (#ba50c1a2) — cannot walk past a latch the
                    # ladder has already set. (#3788c8f0)
                    if not _respawn_budget_consult(name, task_id, "task.assigned"):
                        continue
                    dispatch_claude(name, agent_key, workspace, model, task_id, task_title, api_url, env_file, repos, claude_env_file=claude_env_file)
                elif event_type == "task.mentioned":
                    # Server emits task.mentioned per recipient when this agent is
                    # @-tagged. Stream is pre-filtered to our key — no recipient check.
                    # Wake on BOTH user (human) and agent author @-mentions: agent→agent
                    # mentions are legitimate captain/lead handoffs (Atlas→Orbit FYI,
                    # lead→dev). The original user-only gate (Orbit 2026-05-25) meant
                    # Atlas's `/sweep` "FYI @orbit" nudges were silently dropped — those
                    # are exactly the early warnings I need. Loop safety: skip
                    # self-mention (own name); claim_dispatch dedup + DEBOUNCE_SEC=120
                    # block re-spawn of an already-alive session.
                    # (Orbit 2026-05-28, after 17h-rot incident #1c646d59 / #d45d48f5.)
                    # Field names + author default + self-guard + ask gate all
                    # rebuilt 2026-07-27 (#5a69b827) — see _mention_fields.
                    mention_context = _mention_fields(event)
                    _who = (f"{mention_context['author_kind'] or 'unknown-kind'} "
                            f"{mention_context['author_name'] or '?'!r}")
                    _park_notify = (_is_dispatcher_park_notify(mention_context)
                                    and _park_notify_claim(task_id))
                    if _mention_is_self(name, mention_context) and not _park_notify:
                        log(name, f"@-mentioned in {task_id} by SELF — skip (loop guard)")
                        _record_mention_gate(name, event_type, task_id,
                                             mention_context, False, "self-mention")
                    else:
                        _wake, _why = _mention_is_ask(name, mention_context)
                        if _park_notify:
                            _wake, _why = True, "dispatcher-park-notify"
                        _record_mention_gate(name, event_type, task_id,
                                             mention_context, _wake, _why)
                        if not _wake:
                            log(name, f"@-mention in {task_id} by {_who} is ATTRIBUTION, "
                                      f"not an ask — no spawn ({_why}). "
                                      f"Body: {(mention_context['comment_preview'] or '')[:200]!r}. "
                                      f"To actually route work here: assign_task + move_task→todo.")
                        else:
                            # Shared respawn ladder (#3788c8f0). With
                            # task.commented below this was 63 of 226 surplus
                            # spawns: `_mention_is_ask` + DEBOUNCE_SEC=120 bound
                            # the BURST, nothing bounded the total. A card that
                            # keeps attracting @-mentions now escalates like any
                            # other stuck card instead of re-entering forever.
                            _ok, _count, _token = _respawn_budget(
                                name, task_id, "task.mentioned")
                            if not _ok:
                                continue
                            log(name, f"@-mentioned by {_who} in {task_id} — waking up ({_why})")
                            dispatch_claude(name, agent_key, workspace, model, task_id, task_title,
                                            api_url, env_file, repos, mention_context=mention_context,
                                            claude_env_file=claude_env_file, budget_token=_token)
                elif event_type == "task.commented":
                    # Fallback wake on inline `@<self>` in a comment when the server
                    # emits task.commented but NOT task.mentioned (older server builds,
                    # inline @-tags not yet parsed server-side, etc.). Allows BOTH
                    # user and agent authors (see task.mentioned rationale above).
                    # Self-skip + claim_dispatch dedup + DEBOUNCE_SEC=120 are the
                    # loop safety. (Orbit 2026-05-28.)
                    # Same field-name defect as task.mentioned, and it made this
                    # ENTIRE branch dead: it read the body from the top level while
                    # the server nests it at comment.body, so the body was always ""
                    # and the `@<name>` test never matched — 1691 events, 0 wakes,
                    # 0 self-skips, in the whole log. The fallback that was supposed
                    # to insure us against a missing task.mentioned never once fired.
                    # (Orbit 2026-07-27, #5a69b827.)
                    mention_context = _mention_fields(event)
                    comment_body = mention_context["comment_preview"]
                    _who = (f"{mention_context['author_kind'] or 'unknown-kind'} "
                            f"{mention_context['author_name'] or '?'!r}")
                    _park_notify = (_is_dispatcher_park_notify(mention_context)
                                    and _park_notify_claim(task_id))
                    if f"@{name.lower()}" not in comment_body.lower():
                        log(name, f"task.commented — no @{name} mention, skip")
                    elif _mention_is_self(name, mention_context) and not _park_notify:
                        log(name, f"task.commented @-mention in {task_id} by SELF — skip (loop guard)")
                        _record_mention_gate(name, event_type, task_id,
                                             mention_context, False, "self-mention")
                    else:
                        _wake, _why = _mention_is_ask(name, mention_context)
                        if _park_notify:
                            _wake, _why = True, "dispatcher-park-notify"
                        _record_mention_gate(name, event_type, task_id,
                                             mention_context, _wake, _why)
                        if not _wake:
                            log(name, f"task.commented @-mention in {task_id} by {_who} is "
                                      f"ATTRIBUTION, not an ask — no spawn ({_why}). "
                                      f"Body: {comment_body[:200]!r}. "
                                      f"To actually route work here: assign_task + move_task→todo.")
                        else:
                            # Same shared ladder as task.mentioned above — this
                            # is the fallback wake for the same event, so it
                            # must draw on the same budget or it becomes the
                            # ungated Nth path all over again (#3788c8f0).
                            _ok, _count, _token = _respawn_budget(
                                name, task_id, "task.commented")
                            if not _ok:
                                continue
                            log(name, f"task.commented @-mention by {_who} in {task_id} — waking up ({_why})")
                            dispatch_claude(name, agent_key, workspace, model, task_id, task_title,
                                            api_url, env_file, repos, mention_context=mention_context,
                                            claude_env_file=claude_env_file, budget_token=_token)
                elif event_type == "task.status_changed":
                    # Deploy-verify backstop (task 50540452): a deploy-titled task
                    # changed status — fire the checker, which warns if it's a
                    # done ship-task with no prod-verification marker (§1f). Does
                    # NOT respawn the agent (status changes are not work triggers).
                    # status_changed carries no title → the hook resolves it.
                    _deploy_verify_on_status(name, agent_key, api_url, task_id)
                    # Reset the shared respawn ladder (#3788c8f0 AC2). The
                    # ceiling is a threshold on a counter that otherwise only
                    # grows, so without this a card that was legitimately
                    # closed and reopened — or moved between statuses by a
                    # human — would come back with its ladder already spent and
                    # be refused every re-entry, silently. A status change is
                    # the signal that the card's situation actually changed,
                    # which is precisely what the ladder is counting the
                    # absence of.
                    _respawn_budget_reset(task_id, f"status_changed ({name})")
                else:
                    log(name, "Info event, no action")

                current_id = None

        except Exception as e:
            log(name, f"SSE error: {e}")

        # When PAUSE_ALL OR PAUSE_<agent> is set, slow reconnect attempts
        # way down to stop token burn + log spam.
        per_agent_pause = PAUSE_DIR / f"PAUSE_{name}"
        if PAUSE_GLOBAL_FILE.exists():
            log(name, "PAUSE_ALL active — slow-reconnect 5min instead of 10s")
            time.sleep(300)
        elif per_agent_pause.exists():
            log(name, f"PAUSE_{name} active — slow-reconnect 5min instead of 10s")
            time.sleep(300)
        else:
            log(name, "Reconnecting in 10s...")
            time.sleep(10)


def fetch_review_tasks(agent_key: str, api_url: str, agent_name: str = ""):
    """List one agent's tasks currently in `review`.

    Returns None on FAILURE and [] when the agent genuinely has none (task
    5815feef): the caller prunes re-verify gate state for any task missing from
    the collected set, so a failed read used to look exactly like "the task left
    review" and silently dropped a pending the operator escalation. Both existing
    external callers coerce with `or []`, so None is safe for them.
    """
    try:
        url = f"{api_url}/api/v1/agents/me/tasks?status_category=review&limit=200"
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        return data.get("tasks") or []
    except Exception as e:
        who = f" for {agent_name}" if agent_name else ""
        key_hint = f" (key …{agent_key[-6:]})" if agent_key else ""
        # Classify: auth failure vs Mesh-side failure so the reader knows where to look.
        err_str = str(e)
        if "401" in err_str or "403" in err_str:
            kind = "auth — check agent_key"
        elif any(c in err_str for c in ("500", "502", "503", "504")):
            kind = "Mesh/gateway down — agent_key not the issue"
        else:
            kind = "network/timeout"
        log("review-sweep", f"list review failed{who}{key_hint}: {e} [{kind}]")
        return None


def _pavel_ask_state(t: dict, agent_key: str, api_url: str) -> str:
    """TRI-STATE: is this review task awaiting PAVEL? "ask" / "clear" / "unknown".

    FAIL-CLOSED (task 5815feef, 2026-07-30). The predecessor returned a plain
    bool, and False meant BOTH "the ask was positively withdrawn" and "I could
    not see an ask". `run_review_sweep` read every False as *self-healed* — it
    dropped the card into `close_bucket` AND pruned its re-verify gate state, so
    the escalation the driver had already promised in the thread never happened
    and never retried (one-shot: the sweep runs every 6h and the 4h hold is
    decided at a single sample point).

    THE GATE WAS NOT INERT — it mis-CLEARED. Recounted 2026-07-30 03:51 MSK
    (task 23ed94a2) over `~/logs/mesh-dispatcher.log`, matching the strings this
    function's callers emit under the `[escalation-reverify]` tag; the tag's
    first line is 2026-06-13, so "all-time" starts there, not at the file head:

        window              pinged   escalated   escalated (unique cards)
        07-01…07-30           42        29                26
        all-time 06-13…07-30  66        46                43
        06-01…06-30           24        17                17

    Counted instead on the launchd stdout sink `~/logs/mesh-dispatcher-stdout.log`
    the July pair is 39/26. The 3-event delta is not drift: it is three manual
    `run_review_sweep` invocations against #b2e6578a at 03:05:45 / 03:06:40 /
    03:07:01 while this fix was being probed — `log()` writes them to the file
    sink, launchd's capture never sees them. DAEMON-ONLY July is 39 pinged /
    26 escalated. Re-derive before citing: the log grows, and a prior revision
    of this docstring asserted "52 pinged / 41 escalated", which reproduces on
    no window of either file.

    What the gate actually got wrong: 9 `pavel-ask cleared` events on 8 cards,
    all in July (June: zero), identical in both sinks. That log line only fires
    when gate state EXISTED, so every one aborted a live hold. Two of the nine
    are proven false clears — after the 03:02:57 restart onto the fixed
    predicate the very next sweep re-detected and escalated them:
      #b2e6578a  cleared 07-22 05:41:50 -> escalated 07-30 03:12:53 at 195.5h
                 since its ping (money-path, Stripe)
      #6008f68d  cleared 07-29 16:06:11 -> escalated 07-30 03:12:53 at 35.1h
    #f784de0e was listed here as a third and is NOT one: re-detected 03:05:08,
    it cleared AGAIN at 03:12:54 under the fail-closed predicate, i.e. on
    positive evidence — a legitimate self-heal. #b811a2d7 (cleared 07-28) was
    re-detected 03:05:06 with its 4h window still open at measurement time:
    undetermined, deliberately not counted either way. (The earlier "219h / 48h
    / 24h" ages were also wrong; 195.5h and 35.1h are what the escalation lines
    state, and 24h was f784de0e's time-to-clear, not a suppression.)

    Both false-cleared cards still carried server `human_gate=true` AND a
    genuine `❓ Blocking @operator` marker in the thread; the marker simply sat 5
    comments back, outside the 3-comment window the old scan looked at. Two of
    those three slots are consumed by the gate's own machinery:
    `_post_reverify_request` posts a comment (excluded from ask detection since
    #84ab54fd, but it still occupies a slot) and that comment ASKS the owner to
    reply with fresh evidence — so a card whose owner did exactly what the ping
    demanded is *guaranteed* to have its marker evicted. The remediation
    instruction was what made the gate drop the case.

    Discrimination is exact (probe 2026-07-30, all five cards human_gate=true):
    marker at #2/#3 from newest -> True -> escalated + TG-delivered — #27e83fd9,
    #cdeb6fc5, #9a816160, each logged `ask SURVIVED 6.0h`; marker at #5 -> False
    -> suppressed (#b2e6578a, #6008f68d). Distance from the newest comment was
    the ONLY variable that differed.

    So: absence of a visible marker is not evidence the ask is gone.
      "ask"     — structured signal (server human_gate / structural label) or a
                  live non-negated marker anywhere in the thread, with no newer
                  human reply.
      "clear"   — POSITIVE evidence: no marker at all, negated in the marker
                  comment, a later owner comment that withdraws it, or the operator
                  replied after the newest marker (his answer releases it —
                  2026-05-25 bb233804 «я ж ответил там в комментах»).
      "unknown" — the comment read failed. Caller must NOT treat this as clear.

    Scanning the whole thread removes the accidental withdrawal-by-scrolling the
    3-comment window used to provide, so a LATER withdrawal comment now has to be
    honoured explicitly — otherwise an ask becomes un-withdrawable and nags
    forever (the E1 #8594d87d failure mode). That prose path only runs for cards
    WITHOUT the server flag and without a structural label: when the
    authoritative signal says "gated", prose cannot override it.
    """
    # Tier 0: the server's own sticky flag (evc-mesh PR #258, stamped by
    # enforceBlockingTriage on any ❓Blocking comment). Scroll-proof and free —
    # `_has_human_gate_signal` has consulted it since 2026-06-16; the review
    # sweep never did, which is the whole hole. Tier 1: structural labels.
    if t.get("human_gate"):
        return "ask"
    if {str(l).lower() for l in (t.get("labels") or [])} & _HUMAN_GATE_LABELS_DISPATCH:
        return "ask"
    task_id = t.get("id")
    if not task_id:
        return "unknown"
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/comments?limit=50"
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        comments = data.get("comments") or data.get("items") or []
    except Exception:
        return "unknown"      # read failed -> we determined NOTHING (fail closed)
    if not comments:
        return "clear"        # successful read, genuinely no ask in the thread
    comments.sort(key=lambda c: c.get("created_at") or "")
    newest_marker_ts = None
    newest_user_ts = None
    newest_negator_ts = None
    # Scan the WHOLE thread, not the last 3 — the window was the defect.
    for c in comments:
        ts = c.get("created_at") or ""
        if (c.get("author_type") or c.get("author_kind") or "").lower() == "user":
            newest_user_ts = ts   # the operators own comment is not an ask TO the operator
            continue
        body = (c.get("body") or "").lower()
        # A fleet DRIVER comment that quotes the marker is not an ask (#84ab54fd).
        # pr-task-driver's no-stall comment prints the marker as instructions
        # ("добавь `❓ **Blocking @operator**: <вопрос>`"), and `_post_reverify_request`
        # prints it too — so without this filter the gate detects its OWN ping,
        # the ask never clears, and every stale-in-review task escalates to the operator on a
        # blocker that was never raised. This was the phantom on 8 spark-gen batches.
        if _is_automated_comment(body):
            continue
        # One masked view for BOTH sides (#ba5a4f10) — see _has_human_gate_signal.
        # The `last`/`scope` offset trap that #ce053513 had to dodge cannot bite here:
        # these negators are plain whole-body substring tests, never a slice taken after
        # the last marker, so there is no offset to straddle two strings. Rescoping is
        # therefore just "read both from `masked`" — but it must be BOTH, or a quoted
        # negator disarms a live ask (AC3).
        masked = _gate_masked_low(body)
        negated = any(n in masked for n in _PAVEL_ASK_NEGATORS)
        if negated:
            newest_negator_ts = ts
        if any(m in masked for m in _PAVEL_ASK_MARKERS) and not negated:
            newest_marker_ts = ts   # marker+negator in one comment -> withdrawn
    if newest_marker_ts is None:
        return "clear"
    if newest_user_ts and newest_user_ts > newest_marker_ts:
        return "clear"        # the operator answered AFTER the ask -> ball with agent
    if newest_negator_ts and newest_negator_ts > newest_marker_ts:
        return "clear"        # owner withdrew it in a later comment
    return "ask"


def _comments_have_pavel_ask(task_id: str, agent_key: str, api_url: str) -> bool:
    """Back-compat boolean wrapper over `_pavel_ask_state`. Treats "unknown" as
    an ask (fail-closed) — callers that need to distinguish must use the
    tri-state directly."""
    return _pavel_ask_state({"id": task_id}, agent_key, api_url) != "clear"


def _load_reverify_state() -> dict:
    """Load the escalation re-verify gate state. Returns {} on any failure."""
    try:
        if ESCALATION_REVERIFY_FILE.exists():
            data = json.loads(ESCALATION_REVERIFY_FILE.read_text())
            if isinstance(data, dict):
                return data
    except Exception as e:
        log("escalation-reverify", f"state load failed: {e}")
    return {}


def _save_reverify_state(state: dict) -> None:
    """Atomically persist the gate state. Best-effort, never raises."""
    try:
        ESCALATION_REVERIFY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ESCALATION_REVERIFY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False))
        tmp.replace(ESCALATION_REVERIFY_FILE)
    except Exception as e:
        log("escalation-reverify", f"state save failed: {e}")


def _load_gate_scope_shown() -> dict:
    """Load {task_id: last-SENT-in-a-digest ISO ts}. Returns {} on any failure.

    Failing to {} is the safe direction here: an unreadable state file means every
    card reads as never-shown, so the rotation restarts from the beginning of the
    queue. It over-shows, it cannot starve.
    """
    try:
        if GATE_SCOPE_SHOWN_FILE.exists():
            d = json.loads(GATE_SCOPE_SHOWN_FILE.read_text())
            if isinstance(d, dict):
                return d
    except Exception as e:
        log("review-sweep", f"gate-scope shown-state load failed: {e}")
    return {}


def _save_gate_scope_shown(state: dict) -> None:
    """Atomically persist the rotation state. Best-effort, never raises."""
    try:
        GATE_SCOPE_SHOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = GATE_SCOPE_SHOWN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False))
        tmp.replace(GATE_SCOPE_SHOWN_FILE)
    except Exception as e:
        log("review-sweep", f"gate-scope shown-state save failed: {e}")


def _load_ready_close_seen() -> dict:
    """Load the 'ready-to-close already surfaced' state. {} on any failure."""
    try:
        if REVIEW_READY_SEEN_FILE.exists():
            d = json.loads(REVIEW_READY_SEEN_FILE.read_text())
            if isinstance(d, dict):
                return d
    except Exception as e:
        log("review-sweep", f"ready-close state load failed: {e}")
    return {}


def _save_ready_close_seen(state: dict) -> None:
    """Atomically persist the ready-close seen-set. Best-effort, never raises."""
    try:
        REVIEW_READY_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = REVIEW_READY_SEEN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False))
        tmp.replace(REVIEW_READY_SEEN_FILE)
    except Exception as e:
        log("review-sweep", f"ready-close state save failed: {e}")


def _fetch_comments_for_gate(task_id: str, agent_key: str, api_url: str) -> list:
    """Fetch recent comments for gate_status() reclassification. One read, never raises."""
    try:
        url = f"{api_url}/api/v1/tasks/{task_id}/comments?limit=50"
        req = Request(url, headers={"X-Agent-Key": agent_key})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("comments") or data.get("items") or []
    except Exception:
        return []


def _reverify_poster(agents_cfg: list, exclude_name: str):
    """Pick an author (name, key) for the re-verify ping that is NOT the agent
    being asked — otherwise the listener's self-mention loop guard swallows the
    @mention and the agent never wakes. Prefer Orbit (coordinator identity)."""
    excl = (exclude_name or "").lower()
    orbit = next((a for a in agents_cfg
                  if a.get("name", "").lower() == "orbit"
                  and a.get("name", "").lower() != excl
                  and a.get("agent_key")), None)
    if orbit:
        return orbit["name"], orbit["agent_key"]
    for a in agents_cfg:
        if a.get("name", "").lower() != excl and a.get("agent_key"):
            return a["name"], a["agent_key"]
    return None, None


def _post_task_comment(api_url: str, agent_key: str, task_id: str, body: str) -> bool:
    """POST one comment. True on 200/201, False on anything else. Never raises."""
    if not (api_url and agent_key and task_id):
        # Was the ONLY False path that logged nothing, which is how a missing
        # audit trace became unattributable (task 5815feef).
        log("review-sweep",
            f"comment post skipped for {(task_id or '?')[:8]}: missing "
            f"{'api_url' if not api_url else ''}"
            f"{' agent_key' if not agent_key else ''}"
            f"{' task_id' if not task_id else ''}".strip())
        return False
    try:
        req = Request(f"{api_url}/api/v1/tasks/{task_id}/comments",
                      data=json.dumps({"body": body}).encode("utf-8"), method="POST",
                      headers={"X-Agent-Key": agent_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            return resp.status in (200, 201)
    except Exception as e:
        log("review-sweep", f"comment post failed for {task_id[:8]}: {e}")
        return False


def _post_reverify_request(task: dict, poster_key: str, api_url: str) -> bool:
    """Ask the owning agent ONCE to re-verify its `❓Blocking @operator` blocker
    before it reaches the operator. The @mention (posted by a different author) wakes
    the agent via SSE. Returns True if the comment was accepted."""
    tid = task["id"]
    agent_name = task.get("assignee_name") or ""
    if not agent_name or not poster_key:
        return False
    short = tid[:8]
    hours = ESCALATION_REVERIFY_SEC // 3600
    stale_h = REVIEW_STALE_SEC // 3600
    body = (
        f"@{agent_name} \U0001f504 **Авто-перепроверка перед эскалацией the operator** "
        f"(#{short}, висит более {stale_h}ч в review).\n\n"
        f"Блокер с меткой `❓Blocking @operator` ещё НЕ дошёл до the operator. "
        f"Пере-проверь, что он реально актуален — **re-curl / re-check вживую**, "
        f"не по памяти:\n"
        f"• Самоустранился → сними метку `❓Blocking`, закрой/двинь задачу. the operator не дёргаем.\n"
        f"• Всё ещё блокирует → оставь комментарий со **свежим доказательством** "
        f"(вывод команды / результат проверки).\n\n"
        f"Через ~{hours}ч, если метка ещё активна, эскалирую the operator.\n\n"
        f"_Авто-перепроверка (Orbit, task 7472e600): метка `❓Blocking` "
        f"могла устареть — блокер чинится сам, а the operator теряет дни на фантом._"
    )
    try:
        url = f"{api_url}/api/v1/tasks/{tid}/comments"
        data = json.dumps({"body": body}).encode("utf-8")
        req = Request(url, data=data, method="POST",
                      headers={"X-Agent-Key": poster_key,
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            return resp.status in (200, 201)
    except Exception as e:
        log("escalation-reverify", f"re-verify request failed for {tid}: {e}")
        return False


def _reverify_gate(task: dict, state: dict, agents_cfg: list,
                   api_url: str):
    """Decide whether a marker-based pavel-ask may reach the operator yet.

    Returns (decision, changed) where decision is 'escalate' or 'hold' and
    changed flags whether `state` was mutated (caller persists once per sweep).

    Phase 1 (first sighting): ping the owning agent to re-verify, then HOLD.
    Phase 2 (ask survived ESCALATION_REVERIFY_SEC since the ping): escalate.
    The ask only reaches here while it is STILL present — a self-healed blocker
    stops matching `_comments_have_pavel_ask` and is dropped by the caller, so
    a held task that resolves itself simply never escalates."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat() + "Z"
    tid = task["id"]
    rec = state.get(tid)

    if not rec:
        poster_name, poster_key = _reverify_poster(agents_cfg, task.get("assignee_name"))
        requested = _post_reverify_request(task, poster_key, api_url)
        state[tid] = {
            "first_seen": now_iso,
            "reverify_requested_at": now_iso if requested else None,
        }
        log("escalation-reverify",
            f"{tid[:8]} pavel-ask detected — re-verify requested from "
            f"{task.get('assignee_name','?')} (by {poster_name}); "
            f"holding {ESCALATION_REVERIFY_SEC // 3600}h before the operator")
        return ("hold", True)

    req_at = _parse_iso_utc(rec.get("reverify_requested_at"))
    if req_at is None:
        # The Phase-1 ping never landed — retry it, keep holding.
        poster_name, poster_key = _reverify_poster(agents_cfg, task.get("assignee_name"))
        if _post_reverify_request(task, poster_key, api_url):
            rec["reverify_requested_at"] = now_iso
            log("escalation-reverify", f"{tid[:8]} re-verify ping retried (by {poster_name})")
            return ("hold", True)
        return ("hold", False)

    elapsed = (now - req_at).total_seconds()
    if elapsed >= ESCALATION_REVERIFY_SEC:
        first = not rec.get("escalated_at")
        rec["escalated_at"] = rec.get("escalated_at") or now_iso
        if first:
            log("escalation-reverify",
                f"{tid[:8]} ask SURVIVED {elapsed / 3600:.1f}h re-verify window "
                f"— escalating to the operator (genuine blocker)")
        return ("escalate", first)
    log("escalation-reverify",
        f"{tid[:8]} within re-verify window ({elapsed / 3600:.1f}/"
        f"{ESCALATION_REVERIFY_SEC / 3600:.0f}h) — holding from the operator")
    return ("hold", False)


def _fetch_gate_scope_for_sweep(now_utc) -> list:
    """the operator-gated cards OUTSIDE `review` — for the 🔴 digest section ONLY.

    Returns a list of task dicts stamped with `_age_h` (age of the WAIT, not of
    the row) and `_gate_scope_status`, newest-last, capped at GATE_SCOPE_MAX.
    Returns [] on any failure or when the kill-switch is off.

    SCOPE DISCIPLINE — this is the whole reason the function exists separately
    instead of widening `fetch_review_tasks()`:

      * these cards NEVER enter `seen`, so they cannot be auto-closed as
        recurring throwaways (that path MOVES TASKS TO DONE) and they cannot
        perturb the re-verify state prune, which keys off "left review";
      * they NEVER enter `pavel_bucket`, so they cannot reach `_reverify_gate`.
        That gate @mentions the owning agent, and an @mention is an SSE WAKE —
        widening the sweep must not start spawning agents on cards it merely
        learned to see. Incident E1 (#8594d87d) is the standing rule: no gate on
        the wake path;
      * nothing here is fed to `_human_gate_blocks_feed` / `_has_human_gate_signal`,
        the freeze/feed predicates. Those read their own inputs and are untouched.

    In other words the blast radius is exactly one extra section in one Telegram
    message, which is what the task asked for and nothing more.

    `ready-to-close` cards are excluded: that classification means the blocker is
    resolved and only the operators click remains, the daily digest already owns that
    bucket with its own reminder backoff, and the sweep's own ready-bucket state
    is keyed to review membership. Surfacing them here would double-nag exactly
    the population the operator complained about on 2026-06-26.
    """
    if not GATE_SCOPE_SWEEP_ENABLED:
        return []
    if _PD_GATE_SCOPE is None:
        # Loud, not silent: a degraded channel that says nothing is
        # indistinguishable from a channel with nothing to say.
        log("review-sweep",
            f"gate-scope: predicate import unavailable ({_PD_IMPORT_ERR or 'unknown'}) "
            f"— degrading to review-only sweep (pre-#f49ad8ca behaviour)")
        return []
    try:
        rows = _PD_GATE_SCOPE() or []
    except Exception as e:
        log("review-sweep",
            f"gate-scope fetch failed ({type(e).__name__}: {e}) — review-only this pass")
        return []

    fresh = 0
    ready_skipped = 0     # digest owns these (see the docstring)
    no_wait = 0           # unparseable wait clock — dropped silently before #7a489d2f
    out = []
    for t in rows:
        if t.get("_gate_status") == "ready-to-close":
            ready_skipped += 1
            continue
        ws = t.get("_wait_since") or _parse_iso_utc(t.get("updated_at"))
        if ws is None:
            no_wait += 1
            continue
        if getattr(ws, "tzinfo", None) is not None:
            ws = ws.replace(tzinfo=None)
        age_h = (now_utc - ws).total_seconds() / 3600.0
        # Same staleness threshold the review half uses, so the digest header
        # («висят >Nч») stays literally true for every line under it.
        if age_h * 3600 < REVIEW_STALE_SEC:
            fresh += 1
            continue
        t["_age_h"] = age_h
        out.append(t)

    # Rank exactly as the digest does — (waited longest, then priority) — so the two
    # the operator channels order the same population the same way. Found by MEASURING the
    # first cut of this function rather than by reading it: sorting the whole set by
    # age alone filled all ten slots with 45-76-day `backlog` cards and pushed
    # #b0a81e17 (urgent, money-critical, `triage`, 4 days) to rank 47 — the very card
    # this task exists to surface, buried by parked ones. `backlog` means "not now"
    # by definition, so it must not compete with live work for slots; the digest
    # learned this the same way and split it into its own bucket (#2836cd00).
    # Key = (urgent first, then longest wait, then priority). The middle and last
    # terms are the digest's key verbatim (#73a96478: age must lead, or a card
    # sinks below the cap exactly as it becomes urgent — the bug that buried a
    # 42-day Spark gate). The `urgent` float in front of it is this channel's one
    # deliberate deviation, and it is measured, not assumed: 3 of the 44 live
    # gated cards are `urgent`, so the float costs at most 3 slots, while without
    # it #b0a81e17 — urgent, money-critical, waiting 4 days — ranked 47th behind
    # 40-to-59-day medium/high cards and got no detail line. A repeating
    # escalation channel that cannot put a money-critical blocker in front of
    # the operator is not doing the job this task exists to give it.
    _PRIO = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

    def _rank(x):
        return (0 if x.get("priority") == "urgent" else 1,
                -x.get("_age_h", 0) / 24.0,
                _PRIO.get(x.get("priority"), 4))

    live = sorted([t for t in out if t.get("_gate_scope_status") != "backlog"], key=_rank)
    parked = sorted([t for t in out if t.get("_gate_scope_status") == "backlog"], key=_rank)
    # Parked cards get a minority of the slots; live work gets whatever is left,
    # and the reverse (live first) means a quiet week can still show parked ones.
    parked_slots = max(0, min(len(parked), GATE_SCOPE_BACKLOG_MAX,
                              GATE_SCOPE_MAX - min(len(live), GATE_SCOPE_MAX - GATE_SCOPE_BACKLOG_MAX)))
    kept = live[:GATE_SCOPE_MAX - parked_slots] + parked[:parked_slots]
    kept_ids = {t["id"] for t in kept}
    dropped = [t for t in out if t["id"] not in kept_ids]
    dropped.sort(key=_rank)

    # --- #effd0fbb: rotate the tail --------------------------------------------
    # `dropped` is the starving set: 49 of its members went 29 consecutive cycles
    # without ever being shown. Give it its own slots, filled LEAST-RECENTLY-SHOWN
    # first, so membership of the tail is a countdown instead of a life sentence.
    #
    # Key = (last shown, rank). Never-shown sorts first because "" < any ISO string
    # — a brand-new tail card jumps the queue rather than joining its back, which is
    # what makes the bound in `rotate_after` an upper bound and not an average.
    # `_rank` is the tie-break, so within one shown-timestamp the old ordering still
    # decides — the rotation reorders the tail, it does not discard what ranked it.
    # Budget, clamped by the RENDER cap as well as by its own limit. `_fmt` truncates
    # every bucket at REVIEW_DIGEST_MAX_PER_BUCKET, and the two limits are
    # independently tunable: with ROTATE_MAX above it, the surplus cards would be
    # stamped "shown" and sent to the back of the queue by a digest that never
    # carried a line for them — starvation reintroduced by the fix, and invisible
    # because the state file would say they had been shown.
    rot_budget = max(0, min(GATE_SCOPE_ROTATE_MAX, REVIEW_DIGEST_MAX_PER_BUCKET))
    shown_state = _load_gate_scope_shown()

    def _queue(pool):
        # Key = (last shown, rank). Never-shown sorts first because "" < any ISO
        # string — a brand-new tail card jumps the queue rather than joining its
        # back, which is what makes `rotate_after` an upper bound and not an
        # average. `_rank` is the tie-break, so within one shown-timestamp the old
        # ordering still decides: the rotation REORDERS the tail, it does not
        # discard what ranked it.
        return sorted(pool, key=lambda x: (shown_state.get(x["id"], ""), _rank(x)))

    # The parked/live split is applied to the rotation with the SAME budget rule as
    # the ranked cut above, and for the same reason. Caught by the #f49ad8ca
    # regression suite, not by reading: rotating a flat `dropped` list handed every
    # slot to `backlog` cards on that fixture, which is precisely the "parked work
    # buries live work" failure #2836cd00 was filed on — reintroduced one block
    # lower down. Parked cards do still rotate (all 31 of them starve otherwise);
    # they just cannot take more of the rotation than they can take of the cut.
    rot_live = _queue([t for t in dropped if t.get("_gate_scope_status") != "backlog"])
    rot_parked = _queue([t for t in dropped if t.get("_gate_scope_status") == "backlog"])
    # The inner `max(0, …)` is not decoration. The ranked cut above uses this same
    # formula unguarded and is safe only because GATE_SCOPE_MAX (10) exceeds
    # GATE_SCOPE_BACKLOG_MAX (3). `rot_budget` has no such floor — the kill-switch
    # for this feature is REVIEW_SWEEP_GATE_SCOPE_ROTATE_MAX=0 — and at 0 the
    # unguarded form yields `rot_live[:-3]`, a NEGATIVE slice that silently rotates
    # everything except the last three live cards. A kill-switch that turns the
    # feature UP is the worst possible failure mode for a kill-switch.
    rot_parked_slots = max(0, min(len(rot_parked), GATE_SCOPE_BACKLOG_MAX,
                                  rot_budget - min(len(rot_live),
                                                   max(0, rot_budget - GATE_SCOPE_BACKLOG_MAX))))
    rotate = rot_live[:rot_budget - rot_parked_slots] + rot_parked[:rot_parked_slots]
    rotate_ids = {t["id"] for t in rotate}
    for t in rotate:
        t["_gate_scope_rotated"] = True
    # Whatever neither the ranked cut nor the rotation reached is still named by id
    # on the overflow line — #2836cd00's rule survives, it just applies to a smaller
    # remainder now.
    over_cap = [d for d in dropped if d["id"] not in rotate_ids]

    # N for the acceptance criterion, computed rather than asserted. Each pool drains
    # at its own rate, so the bound is the WORSE of the two — quoting the live figure
    # alone would understate how long a parked card waits, and a bound that is only
    # true of the faster pool is not a bound. Logged every cycle so it is auditable
    # against the live population instead of being a number in a docstring that the
    # population has long since outgrown.
    def _drain(pool, slots):
        return (-(-len(pool) // slots)) if slots else (0 if not pool else 10 ** 6)
    rotate_after = max(_drain(rot_live, rot_budget - rot_parked_slots),
                       _drain(rot_parked, rot_parked_slots))

    # --- #7a489d2f: name the classes; do not call all of them "gated" ---------
    # `rows` is a UNION of three different classes and this line used to print one
    # `len()` under the word "gated card(s)". Since #61ad469a (07:34 on 2026-07-31)
    # `fetch_gate_scope_tasks()` also returns cards that are NOT gated at all —
    # non-gated cards parked in `triage` past STALE_TRIAGE_HOURS, stamped
    # `_gate_tier="none"`, `human_gate=false`, `human_gate_info=null` on the live API.
    # Cost of the conflation, measured: the roster went 83 (07:27) → 95 (07:55), and
    # that +12-in-28-minutes read as gates piling up. It was the predicate widening.
    # Every one of the 12 ids that entered in that window was `human_gate=false`.
    # A counter that gets quoted in escalations has to be right about what it counts.
    #
    # The tier comes from the digest's own stamp, NOT re-derived here — re-deriving
    # is the two-engines-two-verdicts divergence the import above exists to prevent.
    # An unrecognised tier is reported as its own component rather than folded into a
    # neighbour, so the printed sum is a real reconciliation and not an arithmetic
    # identity that holds however the classifier drifts.
    tiers = {"hard": 0, "soft": 0, "none": 0}
    unknown: dict = {}
    for t in rows:
        k = t.get("_gate_tier")
        if k in tiers:
            tiers[k] += 1
        else:
            unknown[k] = unknown.get(k, 0) + 1
    # The server flag is the one unambiguous number here: `hard` is the canonical
    # freeze predicate (flag ∪ gating label ∪ blocking marker), so it is legitimately
    # wider than the flag. Printing both stops "N gated" from being read as "N cards
    # the server considers gated" — live today those are 85 and 35.
    stamped = sum(1 for t in rows if t.get("human_gate") is True)
    breakdown = (f"{tiers['hard']} hard-gate + {tiers['soft']} soft-ask "
                 f"+ {tiers['none']} stale-triage(NOT gated)")
    for k in sorted(unknown, key=lambda x: str(x)):
        breakdown += f" + {unknown[k]} tier={k!r}(UNCLASSIFIED)"
    log("review-sweep",
        f"gate-scope: {len(rows)} card(s) outside review = {breakdown}"
        f"; of these {stamped} server-stamped human_gate=true")
    log("review-sweep",
        f"gate-scope: of {len(rows)} — {ready_skipped} ready-to-close (digest owns), "
        f"{no_wait} no wait-clock, {fresh} below the {REVIEW_STALE_SEC // 3600}h "
        f"threshold, {len(out)} in scope ({len(live)} live / {len(parked)} backlog), "
        f"{len(kept)} shown"
        + (f", {len(dropped)} over cap: {', '.join(d['id'][:8] for d in dropped)}"
           if dropped else ""))
    # Rotation audit line. `never` is the number this task exists to drive to zero;
    # printing it next to the bound is what makes the claim checkable from the log
    # alone, the way the starvation itself was only provable because the dropped
    # roster was printed in full.
    if dropped:
        never = sum(1 for d in dropped if d["id"] not in shown_state)
        log("review-sweep",
            f"gate-scope rotation: tail {len(dropped)} ({never} never shown), "
            f"{len(rotate)} rotating slot(s) this pass → any tail card is shown "
            f"within {rotate_after} SENT digest(s); "
            f"rotating now: {', '.join(t['id'][:8] for t in rotate) or '—'}"
            + (f"; still unnamed in detail: {len(over_cap)}" if over_cap else ""))
    for t in kept:
        t["_gate_scope_over_cap"] = [d["id"] for d in over_cap]
        t["_gate_scope_rotate"] = rotate
        # The dedup signature is built from THIS, not from the shown subset — see
        # the signature block in run_review_sweep for why rotation must not be able
        # to trigger a send on its own.
        t["_gate_scope_all_ids"] = [d["id"] for d in out]
    return kept


def run_review_sweep(agents_cfg: list, api_url: str) -> None:
    """One pass: collect review tasks, auto-close recurring-throwaway checks
    (drift/analytics) that rotted in review, bucket the rest (awaiting-human-
    decision vs awaiting-verify+close), and send the operator a dedup'd digest.
    Pure reads + at most a few throwaway closes + one TG message — no respawns,
    no auto-close of anything that needs a human."""
    from datetime import datetime, timezone
    global _REVIEW_DIGEST_SIG, _REVIEW_DIGEST_LAST_SENT
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    # Collect & dedup assigned review tasks across all agents.
    seen: dict = {}
    fetch_failures = 0
    for agent in agents_cfg:
        rows = fetch_review_tasks(agent["agent_key"], api_url,
                                  agent_name=agent.get("name", ""))
        if rows is None:
            fetch_failures += 1      # incomplete pass — see the prune guard below
            continue
        for t in rows:
            tid = t.get("id")
            if tid and tid not in seen:
                t["_probe_key"] = agent["agent_key"]   # any valid key reads comments
                seen[tid] = t

    # Leak #2 (the operator rule 2026-06-06): recurring monitoring checks (kind:drift /
    # kind:analytics + phase:execute) are disposable — any real finding spawns
    # its own child task, the instance itself is just a log. When the agent moves
    # one to `review` for sign-off it auto-reassigns to the creator and ROTS on a
    # human (e.g. [Spark] SEO Drift Check instance #18 landed on the operator). `review`
    # is excluded from the stale-respawn loop, so auto_close_throwaway_task never
    # fires there. Close them here instead — move to done, off the human's queue.
    # The next scheduled instance fires on its own; nothing is lost.
    for tid in list(seen):
        t = seen[tid]
        if _is_recurring_throwaway(t):
            auto_close_review_throwaway(
                api_url, t["_probe_key"], tid,
                t.get("assignee_name") or "—")
            del seen[tid]   # closed → drop from bucketing

    # Keep only those stale (updated_at older than threshold).
    stale = []
    for t in seen.values():
        updated = _parse_iso_utc(t.get("updated_at"))
        if updated is None:
            continue
        age = (now_utc - updated).total_seconds()
        if age >= REVIEW_STALE_SEC:
            t["_age_h"] = age / 3600.0
            stale.append(t)

    # the operator-gated cards outside `review` — the second half of this channel's job
    # (#f49ad8ca). Fetched BEFORE the early return below, because "no stale review
    # task" was previously enough to end the pass, and that is the state in which
    # an out-of-review gate is most likely to be the only thing waiting on the operator.
    gate_bucket = _fetch_gate_scope_for_sweep(now_utc)

    if not stale and not gate_bucket:
        _REVIEW_DIGEST_SIG = ""   # clear so a fresh backlog re-alerts
        log("review-sweep", "no review tasks older than threshold, no gate-scope cards")
        return

    # Bucket: awaiting human decision vs awaiting verify+close.
    # A marker-based `❓Blocking @operator` ask does NOT reach the operator directly — it
    # passes through the re-verify gate first (task 7472e600): the owning agent
    # re-checks the blocker, and a self-healed one is suppressed before the operator.
    pavel_bucket, close_bucket, human_bucket = [], [], []
    comment_budget = REVIEW_SWEEP_MAX_COMMENT_FETCHES
    reverify_state = _load_reverify_state() if ESCALATION_REVERIFY_ENABLED else {}
    state_dirty = False
    held = 0
    for t in sorted(stale, key=lambda x: x.get("_age_h", 0), reverse=True):
        # Leak #1 (the operator rule 2026-06-06): kind:human-verify / host-impossible
        # tasks (e.g. MacBook-only #bff66151) are known human/host chores, NOT
        # decisions. Even when assigned to a user they must NOT land in the
        # "🔴 awaiting your decision" digest — route to a non-decision info
        # bucket so they stay visible in Mesh but never ping the operator as a gate.
        if _is_human_verify(t):
            human_bucket.append(t)
            if reverify_state.pop(t["id"], None) is not None:
                state_dirty = True
            continue
        direct_user = (t.get("assignee_type") == "user")
        if direct_user:
            # the operator owns it directly — no agent to re-curl, no phantom risk.
            pavel_bucket.append(t)
            continue

        # Tri-state, fail-closed (task 5815feef). Budget exhaustion is NOT a
        # "clear": it means we never looked, so fall back to the free structured
        # signals and otherwise report "unknown" rather than silently suppressing.
        if comment_budget > 0:
            comment_budget -= 1
            ask_state = _pavel_ask_state(t, t["_probe_key"], api_url)
        else:
            ask_state = _pavel_ask_state(t, "", "")   # tier-0/1 only, no API read

        if ask_state == "clear":
            close_bucket.append(t)
            # POSITIVE evidence the ask is gone → self-healed; drop gate state.
            if reverify_state.pop(t["id"], None) is not None:
                state_dirty = True
                log("escalation-reverify",
                    f"{t['id'][:8]} pavel-ask cleared (self-healed/withdrawn) "
                    f"— suppressed before the operator")
            continue
        if ask_state == "unknown" and t["id"] not in reverify_state:
            # Never entered the gate and we could not determine anything — there
            # is no promise outstanding on this card, so nothing to fail closed
            # on. Bucket it as agent verify+close, but do NOT log it as cleared.
            close_bucket.append(t)
            log("escalation-reverify",
                f"{t['id'][:8]} ask state UNKNOWN (comment read unavailable) "
                f"and no gate state — not escalating, not marking cleared")
            continue
        if ask_state == "unknown":
            # A hold IS in flight and we cannot confirm withdrawal. Fail closed:
            # keep the state and let the window decide (i.e. escalate). "Не смог
            # определить, дошло ли до the operator" -> считаем, что НЕ дошло.
            log("escalation-reverify",
                f"{t['id'][:8]} ask state UNKNOWN with a live hold — keeping "
                f"gate state (fail-closed: absence of proof ≠ withdrawal)")

        # Marker-based pavel ask on an agent-owned task → re-verify gate.
        if not ESCALATION_REVERIFY_ENABLED:
            pavel_bucket.append(t)
            continue
        decision, changed = _reverify_gate(t, reverify_state, agents_cfg, api_url)
        state_dirty = state_dirty or changed
        if decision == "escalate":
            pavel_bucket.append(t)
        else:
            held += 1

    if ESCALATION_REVERIFY_ENABLED:
        # Prune gate state for tasks that left review entirely (resolved) — this
        # is how SILENT self-heals (no comment, just a status move) get dropped.
        # FAIL-CLOSED (task 5815feef): only when the collection pass was COMPLETE.
        # `fetch_review_tasks` returned [] on error, so one flaky/401 agent read
        # made every task it owns look like it had left review — pruning a pending
        # the operator escalation on a network blip. `~/logs/mesh-dispatcher.log` holds
        # 606 such reads (`list review failed`), 2026-06-15…07-21, 448 of them
        # on 06-30/07-01 alone; the stdout sink shows 145 — cite the file.
        if fetch_failures:
            log("escalation-reverify",
                f"prune skipped — {fetch_failures} agent review-list read(s) "
                f"failed this pass; cannot tell 'left review' from 'not read'")
        else:
            for stale_tid in [k for k in reverify_state if k not in seen]:
                reverify_state.pop(stale_tid, None)
                state_dirty = True
        if state_dirty:
            _save_reverify_state(reverify_state)
        if held:
            log("review-sweep",
                f"{held} pavel-ask(s) held in re-verify gate (not escalated yet)")

    # Resolved human-gate → "ready for your close", surfaced ONCE, NOT daily-nagged
    # (the operator 2026-06-26: Willow billing #d38dbd24/#d63bd8d1 nagged «Ждут твоего решения»
    # 5d though only his manual close remained). gate_status() (canonical predicate,
    # not a copy) reclassifies a pavel-ask whose blocker a later owner comment marked
    # resolved (keys given / tech closed) out of the daily «висят >Nч» bucket.
    ready_bucket = []
    if pavel_bucket:
        gate_budget = REVIEW_SWEEP_MAX_COMMENT_FETCHES
        still_pavel = []
        for t in pavel_bucket:
            cs = []
            if gate_budget > 0:
                gate_budget -= 1
                cs = _fetch_comments_for_gate(t["id"], t["_probe_key"], api_url)
            if cs and _gate_status(t, cs) == "ready-to-close":
                ready_bucket.append(t)
            else:
                still_pavel.append(t)
        pavel_bucket = still_pavel

    ready_seen = _load_ready_close_seen()
    review_ids = set(seen.keys())
    pavel_ids = {t["id"] for t in pavel_bucket}
    # Prune: task left review (closed/resolved) OR is a live pavel-ask again
    # (re-blocked) — so a second resolution can surface once more later.
    for tid in list(ready_seen):
        if tid not in review_ids or tid in pavel_ids:
            del ready_seen[tid]
    fresh_ready = [t for t in ready_bucket if t["id"] not in ready_seen]

    # Dedup: resend only if the stuck-set changed OR the reminder window elapsed
    # (so a persistent backlog still re-pings ~daily, but doesn't spam each pass).
    # A fresh ready-to-close task bypasses the dedup-skip so its one-time surface
    # is never swallowed by an unchanged pavel/close/human signature.
    mono = time.monotonic()
    sig = "|".join(sorted(
        [f"P:{t['id']}" for t in pavel_bucket] +
        [f"C:{t['id']}" for t in close_bucket] +
        [f"H:{t['id']}" for t in human_bucket] +
        # Gate-scope cards join the dedup signature so a NEW out-of-review gate
        # re-pings immediately instead of waiting out the remind window on an
        # otherwise unchanged review backlog.
        #
        # #effd0fbb: the signature is built from the WHOLE gate scope, not from the
        # cards that happen to hold a slot this pass. Rotation changes the shown
        # subset every send BY DESIGN; keyed on the subset, the signature would
        # differ every cycle, every cycle would beat the remind window, and the operator
        # would get 4 digests a day instead of 1 — the rotation would have paid for
        # its own fix in exactly the nagging the operator complained about on 2026-06-26.
        # Keyed on the scope, rotation churn is invisible to the dedup while a NEW
        # gate still re-pings immediately, which is the property this term was
        # added for. `_gate_scope_all_ids` is stamped on every row, so any row does;
        # the `or` fallback keeps the old behaviour if the stamp is ever absent.
        [f"G:{i}" for i in ((gate_bucket[0].get("_gate_scope_all_ids")
                             or [t["id"] for t in gate_bucket]) if gate_bucket else [])]))
    if (sig == _REVIEW_DIGEST_SIG
            and (mono - _REVIEW_DIGEST_LAST_SENT) < REVIEW_DIGEST_REMIND_SEC
            and not fresh_ready):
        _save_ready_close_seen(ready_seen)   # persist any prune
        log("review-sweep",
            f"digest unchanged ({len(stale)} stuck), within remind window — skip")
        return

    def _fmt(rows):
        # Compact clickable <a href>#id</a> per task (HTML parse_mode) so the operator
        # opens/closes in one tap — matches the tg-mesh-linkify style, not a raw
        # full URL. Dynamic text HTML-escaped; anchors stay raw. (the operator 2026-05-25.)
        out = []
        for t in rows[:REVIEW_DIGEST_MAX_PER_BUCKET]:
            who = _html.escape(t.get("assignee_name") or "—")
            title = _html.escape((t.get("title") or "").replace("\n", " ")[:60])
            short = t["id"][:8]
            url = f"https://mesh.example.com/t/{t['id']}"
            # Out-of-review cards name their status: "ждёт решения" reads
            # differently for a card in `review` than for one sitting in
            # `triage`, and the operator acts on it differently too.
            st = t.get("_gate_scope_status")
            where = f" [{_html.escape(str(st))}]" if st else ""
            out.append(f'• {who}, {t["_age_h"]/24:.0f}д{where} — {title} '
                       f'<a href="{url}">#{short}</a>')
        extra = len(rows) - REVIEW_DIGEST_MAX_PER_BUCKET
        if extra > 0:
            out.append(f"  …ещё {extra}")
        return "\n".join(out)

    # the operators digest = ONLY genuine asks awaiting HIS decision. The agent
    # verify+close backlog (close_bucket) is captain/Atlas /sweep territory, not
    # the operators action — surfacing it here made him open 30 tasks that were not his
    # (the operator 2026-06-03). If nothing genuinely awaits the operator, send nothing.
    if not pavel_bucket and not fresh_ready and not gate_bucket:
        _REVIEW_DIGEST_SIG = sig
        _REVIEW_DIGEST_LAST_SENT = mono
        _save_ready_close_seen(ready_seen)
        log("review-sweep",
            f"no pavel-gated asks ({len(close_bucket)} agent verify+close — "
            f"Atlas /sweep territory; {len(human_bucket)} human-verify chores; "
            f"{len(ready_bucket)} ready-to-close already surfaced — not pinging the operator)")
        return
    lines = []
    if pavel_bucket:
        lines.append(
            f"\U0001f534 Ждут твоего решения ({len(pavel_bucket)}) — "
            f"висят &gt;{REVIEW_STALE_SEC // 3600}ч:")
        lines.append(_fmt(pavel_bucket))
    if gate_bucket:
        sep = "\n" if pavel_bucket else ""
        lines.append(
            f"{sep}\U0001f534 Ждут твоего решения — вне review "
            f"({len(gate_bucket)}), висят &gt;{REVIEW_STALE_SEC // 3600}ч:")
        lines.append(_fmt(gate_bucket))
        # #effd0fbb: the rotating slice of the tail goes in its OWN message, built
        # below. It is not appended here — see `tail_lines`.
    if fresh_ready:
        sep = "\n" if pavel_bucket else ""
        lines.append(
            f"{sep}✅ Готово — ждут твоего закрытия "
            f"({len(fresh_ready)}), close в Mesh:")
        lines.append(_fmt(fresh_ready))
    if close_bucket:
        lines.append(
            f"\n\u2139\ufe0f (ещё {len(close_bucket)} в review ждут verify+close "
            f"агентами — это не твоё, Atlas /sweep дожимает)")
    if human_bucket:
        lines.append(
            f"\n\U0001f464 (ещё {len(human_bucket)} human-verify/host-chore "
            f"в review — не решение, ждут интерактивную сессию)")
    _post_tg_nag(TG_NAG_CHAT_ID, "\n".join(lines), parse_mode="HTML")

    # --- #effd0fbb: the tail goes in a SECOND message, not on the end of the first --
    # Telegram hard-rejects a body over 4096 chars with a 400, and `_post_tg_nag` only
    # queues into the outbox — the bridge is what meets the limit, so an over-long
    # digest fails AFTER this function has logged «digest sent» and stamped its state.
    # the operator would receive NOTHING while every local signal said the send succeeded:
    # the exact silent-invisibility failure this card was filed on, re-created by its
    # own fix. Measured on the live population right after deploy: appending the
    # rotation block inline produced 4302 chars against the 4096 limit — 206 over, and
    # only under it at 14:25 because the roster happened to be shorter that minute.
    #
    # Splitting rather than trimming is deliberate: every trim target here is a card
    # somebody is waiting on, and dropping the bare-id roster would give back exactly
    # the "counted, not named" defect of #2836cd00. Two messages of ~2.4k and ~1.9k
    # cost one extra notification and lose nothing.
    tail_lines = []
    rot = gate_bucket[0].get("_gate_scope_rotate") if gate_bucket else None
    rot = rot or []
    if rot:
        tail_lines.append(f"\U0001f501 Давно не показывали ({len(rot)}) — "
                          f"вне review, по очереди:")
        tail_lines.append(_fmt(rot))
    over = (gate_bucket[0].get("_gate_scope_over_cap") or []) if gate_bucket else []
    if over:
        # Name what neither block reached. A bare "+N ещё" is the failure mode
        # #2836cd00 was filed on: the reader cannot act on a number. Still budgeted,
        # because a roster is the one part that grows without bound — truncation is
        # announced with the count it hid, never silent.
        room = max(0, TG_MSG_SAFE_CHARS - sum(len(x) + 1 for x in tail_lines) - 80)
        ids, shown_n = [], 0
        for i in over:
            if sum(len(x) + 2 for x in ids) + 10 > room:
                break
            ids.append(i[:8])
            shown_n += 1
        tail_lines.append("  …ещё " + str(len(over)) + " вне review: "
                          + ", ".join(ids)
                          + (f" (+{len(over) - shown_n} не поместились в сообщение)"
                             if shown_n < len(over) else ""))
    if tail_lines:
        _post_tg_nag(TG_NAG_CHAT_ID, "\n".join(tail_lines), parse_mode="HTML")

    _REVIEW_DIGEST_SIG = sig
    _REVIEW_DIGEST_LAST_SENT = mono
    # Mark freshly-surfaced ready-to-close tasks seen so they never daily-nag again.
    now_iso = now_utc.isoformat()
    for t in fresh_ready:
        ready_seen[t["id"]] = now_iso
    _save_ready_close_seen(ready_seen)
    # #effd0fbb: advance the rotation — HERE, after `_post_tg_nag`, and nowhere else.
    # Every early `return` above this point (unchanged signature inside the remind
    # window, nothing awaiting the operator) leaves the state untouched, so a suppressed
    # digest cannot consume a card's turn. The queue advances once per message
    # the operator actually receives, which is the only event that means "shown".
    #
    # Stamped set = exactly what `_fmt` rendered, both buckets sliced by the same
    # cap it applies, so no card can be recorded as shown without a line in the
    # message that just went out.
    if gate_bucket:
        shown_now = (gate_bucket[:REVIEW_DIGEST_MAX_PER_BUCKET]
                     + (gate_bucket[0].get("_gate_scope_rotate")
                        or [])[:REVIEW_DIGEST_MAX_PER_BUCKET])
        gate_shown = _load_gate_scope_shown()
        for t in shown_now:
            gate_shown[t["id"]] = now_iso
        # Prune anything no longer in scope: an id that left the gate scope has no
        # turn to wait for, and left in place it would keep the file growing without
        # bound and mis-order the queue if the card ever came back — it would return
        # holding an old timestamp instead of jumping in as never-shown.
        scope_ids = set(gate_bucket[0].get("_gate_scope_all_ids") or [])
        if scope_ids:
            for tid in [k for k in gate_shown if k not in scope_ids]:
                del gate_shown[tid]
        _save_gate_scope_shown(gate_shown)
        log("review-sweep",
            f"gate-scope rotation: digest SENT — {len(shown_now)} card(s) stamped "
            f"shown ({len(gate_shown)} tracked in scope)")
    # Audit trace on the CARD, once per escalation (task 5815feef). The re-verify
    # ping promises «через ~4ч эскалирую the operator» in the thread, but the escalation
    # itself was TG-only — nothing on the card ever recorded that it happened. A
    # sweep reading only the thread therefore cannot distinguish "escalated" from
    # "silently dropped", and this task was filed on exactly that ambiguity: 2 of
    # its 4 «не эскалировано» rows HAD been escalated and TG-delivered. One
    # `🤖 auto:` comment per card closes the audit gap. The prefix is in
    # human_gate.AUTO_MARKERS, so it can never be re-read as an ask.
    if ESCALATION_REVERIFY_ENABLED and pavel_bucket:
        traced = False
        for t in pavel_bucket:
            rec = reverify_state.get(t["id"])
            if not rec or not rec.get("escalated_at") or rec.get("tg_notified_at"):
                continue
            body = (
                f"🤖 auto: эскалировано the operatorю в Telegram "
                f"({now_iso}Z) — карточка попала в дайджест «Ждут твоего решения» "
                f"после {t.get('_age_h', 0):.0f}ч в review.\n\n"
                f"_Трейс авто-перепроверки (mesh-dispatcher, #5815feef): "
                f"обещание «эскалирую the operator» теперь фиксируется на карточке, "
                f"а не только в TG — иначе по треду не отличить отправку от потери._"
            )
            if _post_task_comment(api_url, t["_probe_key"], t["id"], body):
                rec["tg_notified_at"] = now_iso + "Z"
                traced = True
            else:
                # Do NOT let the audit trace fail the way the escalation used to.
                # A trace that silently does not land leaves the thread in exactly
                # the state this task was filed on — indistinguishable from "never
                # escalated" — so the miss has to be loud even though the TG send
                # itself already succeeded. Observed on #6008f68d, 2026-07-30
                # 03:12: escalated_at stamped, digest delivered, no trace comment
                # and not one log line explaining why (`_post_task_comment`'s
                # guard clause returns False without logging).
                log("escalation-reverify",
                    f"{t['id'][:8]} ESCALATED to the operator but the audit trace did "
                    f"NOT post (key_present={bool(t.get('_probe_key'))}) — thread "
                    f"cannot evidence the send; state left un-stamped so the next "
                    f"pass retries")
        # Fail-closed audit (task 5815feef): anything carrying escalated_at but no
        # tg_notified_at is an escalation whose proof-of-delivery is missing. It is
        # NOT necessarily a failed post — a card reclassified into ready-to-close
        # after the gate ran leaves pavel_bucket and is never offered a trace at
        # all — and both routes are otherwise unlogged, so name the gap rather
        # than infer the cause.
        gap = [k[:8] for k, v in reverify_state.items()
               if v.get("escalated_at") and not v.get("tg_notified_at")]
        if gap:
            log("escalation-reverify",
                f"audit-trace gap — {len(gap)} escalated card(s) with no "
                f"on-card proof of delivery: {', '.join(sorted(gap))}")
        if traced:
            _save_reverify_state(reverify_state)
    log("review-sweep",
        f"digest sent: {len(pavel_bucket)} pavel-gated, {len(fresh_ready)} "
        f"ready-to-close (once), {len(close_bucket)} verify-close, "
        f"{len(human_bucket)} human-verify, {len(gate_bucket)} gate-scope "
        f"(outside review)")


def review_sweep_loop(agents_cfg: list, api_url: str) -> None:
    """Periodic review-backlog visibility sweep. See run_review_sweep."""
    if not REVIEW_SWEEP_ENABLED:
        log("review-sweep", "disabled via REVIEW_SWEEP_ENABLED=0")
        return
    log("review-sweep",
        f"thread started; interval={REVIEW_SWEEP_INTERVAL_SEC}s "
        f"stale={REVIEW_STALE_SEC}s remind={REVIEW_DIGEST_REMIND_SEC}s")
    time.sleep(120)   # let listeners settle after start
    while True:
        try:
            run_review_sweep(agents_cfg, api_url)
        except Exception as e:
            log("review-sweep", f"loop error: {e}")
        time.sleep(REVIEW_SWEEP_INTERVAL_SEC)


def main():
    config = json.loads(CONFIG.read_text())
    api_url = config["mesh_api_url"]
    # Staged-agent gate (task ae7efdd0): an agent block may be staged with
    # "enabled": false — present in config but not yet live (e.g. Lumen, a
    # comet-runtime tenant awaiting its own dedicated Mesh identity before its
    # SSE listener may safely run on its own key). Filter disabled blocks out
    # entirely so they start no listener / stale-loop / dispatch and never land
    # in _AGENTS_BY_NAME. Absent key = enabled (backward compatible).
    all_agents = config["agents"]
    agents = [a for a in all_agents if a.get("enabled", True)]
    disabled = [a.get("name", "?") for a in all_agents if not a.get("enabled", True)]
    # Populate registries for pull-on-reap helper (added 2026-05-22)
    global _API_URL
    _API_URL = api_url
    for a in agents:
        _AGENTS_BY_NAME[a["name"]] = a

    if disabled:
        log("main", f"staged (enabled:false) agents skipped: {', '.join(disabled)}")
    log("main", f"Starting mesh-dispatcher with {len(agents)} agent(s)")

    # Dedup/lock (task 31bb7aad): recover any child claudes that outlived a
    # launchd reload, then start the reaper that frees slots on child exit.
    _load_live_file()
    _load_counters()  # P2 #7: restore stale-circuit counters across restart
    threading.Thread(target=_reaper_loop, daemon=True).start()
    log("main", "dedup reaper started")

    # Stale in_progress re-dispatch (task 24e33cf2): nobody else re-fires
    # task.assigned when a headless session dies silently — this loop is the
    # safety net.
    threading.Thread(target=stale_redispatcher_loop,
                     args=(agents, api_url), daemon=True).start()
    log("main", "stale-redispatch loop started")

    # Review-backlog visibility sweep (Orbit 2026-05-25): review tasks are
    # excluded from respawn, so done-work rotted unclosed and the operator-gated asks
    # died in Mesh comments. This surfaces a dedup'd digest to the operators TG.
    # Cost-neutral: pure reads + a TG message, no respawns.
    #
    # P0 #4 (2026-06-15): merge fiddler.json fleet keys into the sweep so the
    # full fleet is visible, not just Orbit/Lumen from mesh-agents.json.
    _sweep_by_key: dict = {
        a["agent_key"]: a for a in agents if a.get("agent_key")
    }
    _fiddler_cfg = Path.home() / ".config" / "fiddler" / "fiddler.json"
    if _fiddler_cfg.exists():
        try:
            for _fa in json.loads(_fiddler_cfg.read_text()).get("agents", []):
                if not _fa.get("enabled", True):
                    continue
                _fk = _fa.get("mesh_agent_key", "")
                if _fk and _fk not in _sweep_by_key:
                    _sweep_by_key[_fk] = {"name": _fa.get("name", "?"), "agent_key": _fk}
            log("main", f"review-sweep: merged {len(_sweep_by_key)} agent keys "
                f"(mesh-agents.json + fiddler.json)")
        except Exception as _fe:
            log("main", f"review-sweep: fiddler.json merge failed: {_fe} — dispatcher-only keys")
    else:
        log("main", "review-sweep: fiddler.json not found — dispatcher-only keys")
    threading.Thread(target=review_sweep_loop,
                     args=(list(_sweep_by_key.values()), api_url), daemon=True).start()
    log("main", "review-sweep loop started")

    threads = []
    for agent in agents:
        model = agent.get("model", "sonnet")
        env_file = agent.get("env_file")
        repos = resolve_repos(agent)
        claude_env_file = _resolve_claude_env_file(agent)
        env_note = f" + env_file={env_file}" if env_file else ""
        repo_note = f" + {len(repos)} repo(s)" if repos else ""
        cred_note = f" + claude_env_file={claude_env_file}" if claude_env_file else ""
        log("main", f"Launching listener: {agent['name']} ({model}){env_note}{cred_note}{repo_note} → {agent['workspace']}")
        t = threading.Thread(
            target=listen_agent,
            args=(agent["name"], agent["agent_key"], agent["workspace"], model, api_url, env_file, repos),
            kwargs={"claude_env_file": claude_env_file},
            daemon=True,
        )
        t.start()
        threads.append(t)

    log("main", "All agents launched. Waiting...")
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
