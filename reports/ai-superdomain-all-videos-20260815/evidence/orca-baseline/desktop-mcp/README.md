# desktop-mcp

`desktop-mcp` is a local, screenshot-free macOS desktop-control bridge shared by Codex and Claude Code. It has three cooperating processes:

1. Hammerspoon owns the macOS **Accessibility** permission and receives fixed, validated input actions over an owner-only Unix domain socket.
2. `broker.mjs` is a singleton local broker. It holds one persistent Hammerspoon connection and serializes calls from all MCP clients, avoiding per-frame connection setup and cross-client response ambiguity.
3. `server.mjs` exposes those actions as a STDIO MCP server. It cannot run shell commands, AppleScript, files, or arbitrary Lua.

The intended flow is:

```text
Codex / Claude Code -> STDIO MCP -> singleton Unix-socket broker -> Hammerspoon -> CGEvent + Accessibility
```

## Exposed tools

- `desktop_state` — screens, pointer, frontmost app, focused window/control, and optional bounded AX hit-tests; no screenshot. When Accessibility is enabled it also returns a five-second, single-use safety lease bound to that exact observed state.
- `mouse_move`, `mouse_click`, `mouse_drag`, `mouse_scroll` — `mouse_move` accepts an optional `duration_ms` (up to 2000) to visibly traverse its path; omit it for the lowest-latency direct move.
- `key_tap`, `type_text`
- `input_batch` — an ordered short sequence of the same validated actions.

No arbitrary code execution is exposed. Treat keyboard input, text entry, and clicks as sensitive: a model must still obtain approval before sends, purchases, account changes, destructive actions, or entering secrets.

Every input call must immediately follow `desktop_state` and copy its
`safety_lease.token`, `expected_bundle_id`, `expected_pid`, and
`expected_window_id`. Clicks must bind their coordinate and drags must bind
both their starting and destination coordinates through `desktop_state`'s
`x`/`y` or `points` input. The Hammerspoon driver
consumes the token once, rejects it after five seconds, and rechecks the
frontmost app, PID, focused-window id/frame/modal state, focused control, and
coordinate hit target before input. It rejects disabled/hidden targets,
protected text controls, token replay, focus drift, and app/window drift, then
rechecks state after the action. A safety lease is not approval for an
irreversible action.

## Install and configure

1. Hammerspoon is installed as an application. Grant **Hammerspoon** Accessibility access in **System Settings → Privacy & Security → Accessibility**.
2. Create the private runtime directory:

   ```sh
   mkdir -p -m 700 "$HOME/.local/state/desktop-mcp"
   ```

3. Add this exact load statement to `~/.hammerspoon/init.lua` (adjust the absolute path if this project moves):

   ```lua
   dofile("/absolute/path/to/desktop-mcp/hammerspoon/desktop_mcp.lua")
   ```

4. Reload Hammerspoon configuration. It creates `~/.local/state/desktop-mcp/hammerspoon.sock`.
5. Register the same server with Codex and Claude Code:

   ```sh
   codex mcp add desktop-control -- node /absolute/path/to/desktop-mcp/server.mjs
   claude mcp add --scope user desktop-control -- node /absolute/path/to/desktop-mcp/server.mjs
   ```

The first live MCP call starts `broker.mjs` automatically; it stays local to the current macOS login. `server.mjs` has no environment-selectable direct-driver mode and always goes through the broker. The implementation deliberately uses Unix domain sockets inside an owner-only directory instead of a TCP listener or a reusable shared token. Requests from the two MCP clients are serialized before entering Hammerspoon, so a socket response cannot cross between Codex and Claude calls. Owner-only socket permissions do not authenticate one same-user process from another; production use remains blocked until the Hammerspoon-facing endpoint can verify a trusted broker peer (for example, a code-signed XPC service with audit-token validation).

## Test

```sh
npm test
```

The unit tests validate the STDIO MCP protocol, static driver safety contract, and broker fault isolation without touching the desktop. A live `desktop_state` MCP call verifies the Hammerspoon driver and reports `accessibility_enabled`; it can safely run before authorization, but all mouse and keyboard actions are denied until that field is `true` and a fresh observed-state lease is supplied. Production use still requires a separately authorized Hammerspoon reload and a real no-effect state/move E2E before any click or typing.

## Uninstall

Remove the `dofile(...)` line from `~/.hammerspoon/init.lua`, reload or quit Hammerspoon, and remove the `desktop-control` MCP entry from Codex and Claude Code. The driver has no network listener, login item, shell tool, AppleScript tool, or screenshot tool.
