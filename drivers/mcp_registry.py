#!/usr/bin/env python3
"""mcp_registry — render per-agent .mcp.json files from a central registry.

Single source of truth:  ~/.config/mcp-registry/registry.yaml
Secrets:                  ~/.config/agents/keys.env       (mode 600)
Per-agent mesh keys:      ~/bin/mesh-agents.json

Used by:
  * mesh-dispatcher  — regenerates an agent's .mcp.json before each spawn
                       (only when the loader flag is enabled; ALWAYS fail-safe:
                       any error leaves the existing .mcp.json untouched).
  * mcp-registry     — CLI for validate / list / diff / apply / enable / disable.

Design invariants (this code runs in the critical dispatch path):
  - import is cheap and stdlib-only except PyYAML, which is imported lazily and
    degrades gracefully (loader disables itself if yaml is unavailable).
  - regenerate_safe() NEVER raises and NEVER deletes a working file. It writes
    only when the freshly-rendered config differs SEMANTICALLY from on-disk,
    and only via temp-file + atomic os.replace, after backing up the old file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

HOME = Path.home()
REGISTRY_PATH = HOME / ".config" / "mcp-registry" / "registry.yaml"
KEYS_PATH = HOME / ".config" / "agents" / "keys.env"
MESH_AGENTS_PATH = HOME / "bin" / "mesh-agents.json"
LOADER_FLAG = HOME / ".config" / "mcp-registry" / "loader.enabled"
MAX_BACKUPS = 3

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def loader_enabled() -> bool:
    """Dispatcher per-spawn regeneration is opt-in via this flag file."""
    return LOADER_FLAG.exists()


def load_keys(path: Path = KEYS_PATH) -> dict:
    """Parse `export KEY="value"` lines into a dict. Returns {} on failure."""
    result: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
            if not m:
                continue
            k, v = m.group(1), m.group(2).strip()
            if (v.startswith('"') and v.endswith('"')) or \
               (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            result[k] = v
    except OSError:
        pass
    return result


def load_mesh_agents(path: Path = MESH_AGENTS_PATH) -> dict:
    """Return {agent_name_lower: {"workspace":..., "agent_key":..., "name":...}}."""
    out: dict[str, dict] = {}
    data = json.loads(path.read_text(encoding="utf-8"))
    for a in data.get("agents", []):
        name = a.get("name", "")
        if not name:
            continue
        out[name.lower()] = {
            "name": name,
            "workspace": a.get("workspace", ""),
            "agent_key": a.get("agent_key", ""),
        }
    return out


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    """Parse + validate the registry. Raises on malformed input."""
    import yaml  # lazy: dispatcher disables loader if this fails
    reg = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate_registry(reg)
    return reg


def validate_registry(reg: dict) -> None:
    if not isinstance(reg, dict):
        raise ValueError("registry root must be a mapping")
    if reg.get("version") != 1:
        raise ValueError(f"unsupported registry version: {reg.get('version')!r}")
    mcps = reg.get("mcps")
    if not isinstance(mcps, dict) or not mcps:
        raise ValueError("registry.mcps must be a non-empty mapping")
    for mid, spec in mcps.items():
        if not isinstance(spec, dict):
            raise ValueError(f"mcp {mid!r}: spec must be a mapping")
        has_cmd = isinstance(spec.get("command"), str) and spec["command"]
        has_url = isinstance(spec.get("url"), str) and spec["url"]
        if not has_cmd and not has_url:
            raise ValueError(f"mcp {mid!r}: needs a `command` or a `url` (HTTP MCP)")
        if not isinstance(spec.get("args", []), list):
            raise ValueError(f"mcp {mid!r}: `args` must be a list")
        if "headers" in spec and not isinstance(spec["headers"], dict):
            raise ValueError(f"mcp {mid!r}: `headers` must be a mapping")
        agents = spec.get("agents")
        if not isinstance(agents, list) or not agents:
            raise ValueError(f"mcp {mid!r}: `agents` must be a non-empty list")
        for fld in ("env", "server_name_per_agent", "env_per_agent", "args_per_agent"):
            if fld in spec and not isinstance(spec[fld], dict):
                raise ValueError(f"mcp {mid!r}: `{fld}` must be a mapping")
        if "args_per_agent" in spec:
            for agent_name, extra_args in spec["args_per_agent"].items():
                if not isinstance(extra_args, list):
                    raise ValueError(f"mcp {mid!r}: args_per_agent[{agent_name!r}] must be a list")


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _resolve(value, keys: dict, mesh_key: str):
    """Substitute ${VAR} in a string. MESH_AGENT_KEY -> per-agent key, else keys.env.

    Returns (resolved_str, ok). ok=False if a ${VAR} resolved to empty (so the
    caller can OMIT the key rather than write a blank secret).
    """
    if not isinstance(value, str):
        return value, True
    ok = True

    def repl(m):
        nonlocal ok
        name = m.group(1)
        val = mesh_key if name == "MESH_AGENT_KEY" else keys.get(name, "")
        if val == "":
            ok = False
        return val

    out = _VAR_RE.sub(repl, value)
    if out == "":
        ok = False
    return out, ok


def _agent_in(agents: list, agent_lc: str) -> bool:
    return "*" in agents or agent_lc in agents


def render_for_agent(agent_lc: str, reg: dict, keys: dict, mesh_key: str) -> dict:
    """Build the {"mcpServers": {...}} dict for one agent from the registry."""
    servers: dict[str, dict] = {}
    for mid, spec in reg["mcps"].items():
        if not _agent_in(spec.get("agents", []), agent_lc):
            continue
        name = (spec.get("server_name_per_agent") or {}).get(agent_lc, mid)

        if spec.get("url"):
            # Remote HTTP (Streamable HTTP) MCP — e.g. routed via MCP Gate.
            url, url_ok = _resolve(spec["url"], keys, mesh_key)
            if not url_ok:
                continue  # fail-safe: don't write a half-resolved endpoint
            entry: dict = {"type": "http", "url": url}
            hdr_out: dict[str, str] = {}
            for k, v in (spec.get("headers") or {}).items():
                resolved, ok = _resolve(v, keys, mesh_key)
                if ok:
                    hdr_out[k] = resolved
                else:
                    # a header (e.g. Authorization) didn't resolve → omit the
                    # whole server rather than ship an unauthenticated endpoint
                    hdr_out = None
                    break
            if hdr_out is None:
                continue
            if hdr_out:
                entry["headers"] = hdr_out
            servers[name] = entry
            continue

        args = list(spec.get("args", []))
        args.extend((spec.get("args_per_agent") or {}).get(agent_lc, []))
        entry = {"command": spec["command"], "args": args}

        env_in = dict(spec.get("env") or {})
        env_in.update((spec.get("env_per_agent") or {}).get(agent_lc, {}))
        env_out: dict[str, str] = {}
        for k, v in env_in.items():
            resolved, ok = _resolve(v, keys, mesh_key)
            if ok:
                env_out[k] = resolved
            # ok=False -> omit (fail-safe: never write a blank secret)
        if env_out:
            entry["env"] = env_out

        servers[name] = entry
    return {"mcpServers": servers}


# --------------------------------------------------------------------------- #
# Comparison + write
# --------------------------------------------------------------------------- #
def read_current(workspace: str) -> dict | None:
    p = Path(workspace) / ".mcp.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _canon(obj):
    """Order-independent canonical form for semantic comparison."""
    if isinstance(obj, dict):
        return {k: _canon(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_canon(x) for x in obj]
    return obj


def semantically_equal(a, b) -> bool:
    return _canon(a) == _canon(b)


def _prune_backups(workspace: str, keep: int = MAX_BACKUPS) -> None:
    d = Path(workspace)
    baks = sorted(d.glob(".mcp.json.bak.*"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    # Never prune the labelled migration backup.
    baks = [p for p in baks if not p.name.endswith("-pre-registry")]
    for p in baks[keep:]:
        try:
            p.unlink()
        except OSError:
            pass


def write_for_agent(agent_lc: str, workspace: str, mesh_key: str,
                    reg: dict, keys: dict, *, backup: bool = True):
    """Render + (atomically) write if changed. Returns (status, desired_dict).

    status: 'unchanged' | 'updated' | 'created'
    """
    desired = render_for_agent(agent_lc, reg, keys, mesh_key)
    current = read_current(workspace)
    if current is not None and semantically_equal(current, desired):
        return "unchanged", desired

    target = Path(workspace) / ".mcp.json"
    if backup and current is not None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            bak = target.with_name(f".mcp.json.bak.{ts}")
            shutil.copy2(target, bak)
            # .bak carries the same shared creds / GOG-pw / mesh agent-key as
            # the live file — keep it owner-only (P2 secret-hygiene #02349021).
            os.chmod(bak, 0o600)
            _prune_backups(workspace)
        except OSError:
            pass

    tmp = target.with_name(f".mcp.json.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(desired, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    # .mcp.json holds the shared in@example.com/Admin2026!! admin creds,
    # GOG_KEYRING_PASSWORD and the per-agent MESH_AGENT_KEY. Default umask
    # (022) leaves it world-readable (644); enforce owner-only so a registry
    # regen can't silently revert the chmod (P2 secret-hygiene #02349021).
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return ("created" if current is None else "updated"), desired


# --------------------------------------------------------------------------- #
# Dispatcher entry point — fail-safe, never raises
# --------------------------------------------------------------------------- #
def regenerate_safe(agent_name: str, workspace: str, mesh_key: str,
                    log_fn=None) -> None:
    """Called by mesh-dispatcher right before spawning claude.

    Regenerates <workspace>/.mcp.json from the registry IFF the loader flag is
    set. Any failure (missing yaml, malformed registry, IO error) is swallowed
    and the existing .mcp.json is left exactly as-is — the fleet keeps running
    on its last-known-good config.
    """
    def _log(msg):
        if log_fn:
            try:
                log_fn(agent_name, f"[mcp-registry] {msg}")
            except Exception:
                pass

    try:
        if not loader_enabled():
            return
        reg = load_registry()
        keys = load_keys()
        status, _ = write_for_agent(agent_name.lower(), workspace, mesh_key,
                                    reg, keys)
        if status != "unchanged":
            _log(f"{status} .mcp.json")
    except Exception as e:  # noqa: BLE001 — must never break dispatch
        _log(f"skip regen ({type(e).__name__}: {e}); kept existing .mcp.json")
