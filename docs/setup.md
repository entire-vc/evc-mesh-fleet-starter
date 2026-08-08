# Setup — stand up your own fleet (feeder path)

This walks through the feeder (`fiddler`) path, which is the recommended start. The
dispatcher is optional and covered briefly at the end.

## 0. Prerequisites

- macOS with `tmux` (`brew install tmux`).
- Claude Code CLI, logged in: `claude` → `/login` (a Max plan for the $0-per-task feeder).
- Python 3.11+.
- A **Mesh instance** you control, and the **Mesh MCP** binary from your Mesh build
  (agents use it to read/write tasks + memory). Put it somewhere like
  `~/bin/mesh-mcp`.
- For each agent you want, **mint a per-agent API key** on your Mesh (an `agk_...`
  token) and note the agent's Mesh UUID.

Put this kit somewhere stable, e.g. `~/mesh-fleet-starter`, and make the helpers
executable: `chmod +x drivers/*.sh drivers/mcp-wrap`.

## 1. Decide your agents

Pick a small team to start — e.g. an orchestrator + one product lead + one dev.
For each, choose a short lowercase **slug** (used as the tmux session and workspace
name), e.g. `atlas`, `nova`, `kilo`.

## 2. Create each agent's workspace

For every agent:

```bash
mkdir -p ~/ClaudeCowork/<slug>
cp agents/<role>/CLAUDE.md ~/ClaudeCowork/<slug>/CLAUDE.md   # pick the closest role
cp -r agents/_shared ~/ClaudeCowork/<slug>/_shared           # the shared instruction set
```

Edit `~/ClaudeCowork/<slug>/CLAUDE.md`: set the agent's name/identity, and make sure
its `@_shared/CLAUDE-*.md` imports resolve (the `_shared` dir must sit next to the
`CLAUDE.md`, or adjust the import paths). Replace every placeholder
(`example.com`, `/Users/fleet`, routing tables) with your own.

## 3. Configure the MCP registry (the Mesh MCP at minimum)

```bash
mkdir -p ~/.config/mcp-registry ~/.config/agents
cp config/mcp-registry.example.yaml ~/.config/mcp-registry/registry.yaml
cp config/keys.env.example ~/.config/agents/keys.env && chmod 600 ~/.config/agents/keys.env
```

Edit `registry.yaml`: set `MESH_API_URL` to your instance and point `args` at your
`mesh-mcp` binary. If your Mesh sits behind an HTTP basic-auth reverse proxy, add
`MESH_BASIC_AUTH: user:pass` to the env (it rides the `Authorization` header; the app
auth uses `X-Agent-Key`, so they don't collide).

Render each agent's `.mcp.json` from the registry (or write `.mcp.json` by hand):

```bash
python3 - <<'PY'
import sys; sys.path.insert(0, "drivers")
import mcp_registry as m
reg, keys = m.load_registry(), m.load_keys()
for slug, key in [("atlas","agk_..."), ("nova","agk_..."), ("kilo","agk_...")]:
    m.write_for_agent(slug, f"/Users/YOU/ClaudeCowork/{slug}", key, reg, keys)
PY
```

## 4. Configure the feeder

```bash
mkdir -p ~/.config/fiddler ~/.fiddler/logs
cp config/fiddler.example.json ~/.config/fiddler/fiddler.json
```

Edit `fiddler.json`: your `mesh_api_url`, and one `agents[]` entry per agent — its
`name`, `tmux_session` (the slug), `mesh_agent_key`, and `workspace`. Point each
`reauth_recovery.spawn_cmd` at `drivers/fiddler-lane-respawn.sh <slug> sonnet`.

## 5. Start the agent sessions

Each agent needs its persistent TUI session started once:

```bash
drivers/fiddler-lane-respawn.sh atlas sonnet
drivers/fiddler-lane-respawn.sh nova  sonnet
drivers/fiddler-lane-respawn.sh kilo  sonnet
```

Each opens a `tmux` session running `claude` in that agent's workspace. Verify:
`tmux ls` shows them; `tmux attach -t nova` shows the idle Claude prompt.

## 6. Run the feeder

Foreground (to watch it):

```bash
python3 drivers/fiddler.py run
```

Or as a launchd daemon (survives logout):

```bash
# edit the absolute paths inside the plist first
cp config/launchd/com.example.fiddler.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.fiddler.plist
launchctl kickstart -k gui/$(id -u)/com.example.fiddler
```

Useful CLI: `python3 drivers/fiddler.py status` (per-agent state), `… doctor`
(health-check every configured session).

## 7. Feed a task

On your Mesh: create a task in an agent's project, **assign it to that agent**, and
move it to **`todo`**. Within ~30–60s the feeder pastes it into the agent's session
and it starts working. Completion is detected when the agent moves the card to
`review`/`done`.

> Remember: **assigning + `todo` is what wakes an agent.** A comment or `@mention`
> does not wake a feeder agent (see `agents/_shared/CLAUDE-communication.md`).

## 8. (Optional) the dispatcher path

If you want event-driven spawning / `@mention` wakeups for a coordinator:

```bash
cp config/mesh-agents.example.json ~/bin/mesh-agents.json   # edit: your agent(s), keys, env_file
cp config/launchd/com.example.dispatcher.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.dispatcher.plist
```

The dispatcher uses metered API (not the subscription) and spawns a fresh session per
event. It's the larger, more fleet-specific driver — read `drivers/mesh-dispatcher.py`
before relying on it.

## Troubleshooting

- **Agent never picks up a task** → is it assigned to *that* agent and in `todo`? `fiddler.py status` shows what the feeder sees.
- **Session stuck on `/login`** → `fiddler-lane-respawn.sh <slug>` re-spawns it; make sure Claude Code is logged in for that user.
- **MCP tools missing in a session** → check the agent's `.mcp.json` and that `mesh-mcp` authenticates (run it by hand with the agent's `MESH_AGENT_KEY` set; it should print "Authenticated as agent: …").
- **Mesh behind basic-auth returns 401** → set `MESH_BASIC_AUTH` (registry) and `basic_auth` (fiddler.json).
