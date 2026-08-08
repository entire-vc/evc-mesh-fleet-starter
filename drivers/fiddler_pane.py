"""Recency-aware classification of the Claude-TUI auth state from a tmux pane.

Shared by BOTH reauth detectors so they can never drift apart:
  - scripts/fiddler.py             → session_health()
  - scripts/fiddler-reauth-watchdog.py (deployed to ~/bin/) → is_reauth()

WHY THIS EXISTS (task #038393a0, 2026-07-21, Orbit)
---------------------------------------------------
Both detectors used to be a bare substring scan over `tmux capture-pane -p`:

    return any(marker in pane for marker in REAUTH_MARKERS)

The Claude TUI renders its whole transcript into the pane, so ONE failed turn —

    ⏺ Please run /login · API Error: 401 OAuth access token has expired.

— stays visible until new output scrolls it away. Measured in production on
2026-07-21: 15-20 minutes per occurrence. During that window both detectors read
a DEAD transcript line as LIVE auth state:

  * fiddler marked the lane UNHEALTHY and refused to feed it, and
  * the watchdog killed + respawned the session every KICK_COOLDOWN (30 min),
    destroying whatever the agent was doing.

The kill was never necessary. The session process was fine — it was sitting at
its idle `❯` prompt, able to accept input. The proven fix (used manually to end
the 2026-07-21 incident) is `/clear`: it drops the transcript, and therefore the
marker, WITHOUT killing the session, after which the very next fed task ran
normally.

THE CLASSIFICATION
------------------
`reauth_scan()` splits the old boolean into three states:

  WALL       the TUI cannot accept input at all — a full-screen login takeover
             ("Select login method" / "Sign in to Claude"), or an auth marker
             with NO idle prompt anywhere in the pane. This is the only state
             that justifies a destructive recovery.

  TRANSCRIPT an auth-error marker is visible BUT the idle `❯` prompt is there,
             i.e. the TUI is alive and accepting input. The marker is a
             historical transcript line of unknown age. Cheap, non-destructive
             remediation (`/clear`) first; escalate to WALL handling only if the
             marker comes BACK after N clear-cycles — that is what distinguishes
             a one-off failed turn from a genuine expiry, and it cannot be read
             off a single pane snapshot.

  NONE       no auth marker.

KNOWN RESIDUAL FALSE-POSITIVE (pre-existing, deliberately not widened here):
an agent whose pane is DISPLAYING these marker strings — reading this file, a
log, or a transcript — scans as TRANSCRIPT. Callers must therefore skip lanes
that are visibly busy (see BUSY_MARKERS) before acting, and the remediation for
TRANSCRIPT is `/clear` (recoverable) rather than a kill. Deterministic
alternatives (a TUI-emitted auth sentinel) are the real fix and are out of scope
for this task.
"""

NONE = "none"
WALL = "wall"
TRANSCRIPT = "transcript"

# The claude TUI draws a "❯" input box when it can accept a prompt. It is NOT
# pinned to the bottom (blank rows follow), so scan the whole pane.
IDLE_PROMPT_MARKER = "❯"

# Full-screen login takeover: no input box, nothing can be pasted.
LOGIN_WALL_MARKERS = (
    "select login method",
    "sign in to claude",
)

# Auth failures the TUI renders as a TRANSCRIPT line and then returns to idle.
# Union of what fiddler.py and the watchdog each matched before this module, so
# neither detector loses coverage. Matched case-insensitively — the two detectors
# previously disagreed on case, which was itself a source of drift.
#
# ⚠ Markers MUST be phrases only the Claude TUI prints — never substrings that can
# appear in code an agent is editing. A bare "/login" false-positived on Ember
# working on auth tests (route paths in the pane).
AUTH_ERROR_MARKERS = (
    "please run /login",
    "invalid api key",
    "oauth token expired",
    "oauth access token has expired",
    "invalid authentication",
    "not logged in",
    "login expired",
)

# TUI is mid-turn / awaiting a decision — callers must not /clear or kill it.
BUSY_MARKERS = (
    "esc to interrupt",
    "do you want to proceed",
)


def reauth_scan(pane: str) -> tuple[str, str]:
    """Classify a `capture-pane -p` snapshot. Returns (state, detail).

    state ∈ {NONE, WALL, TRANSCRIPT}. `detail` is a short human string naming the
    marker that matched, for logs and alerts.
    """
    if not pane:
        return NONE, ""
    low = pane.lower()

    for m in LOGIN_WALL_MARKERS:
        if m in low:
            return WALL, f"login screen: {m!r}"

    hit = next((m for m in AUTH_ERROR_MARKERS if m in low), None)
    if hit is None:
        return NONE, ""

    if IDLE_PROMPT_MARKER not in pane:
        # Marker with no input box: the TUI is not able to take a prompt, so
        # /clear cannot land. Destructive recovery is the only lever.
        return WALL, f"auth marker {hit!r}, no idle prompt"

    return TRANSCRIPT, f"auth marker {hit!r} in transcript (idle prompt present)"


def pane_is_busy(pane: str) -> bool:
    """True if the TUI is mid-turn. Nothing destructive (/clear, kill) may run."""
    if not pane:
        return False
    low = pane.lower()
    return any(m in low for m in BUSY_MARKERS)
