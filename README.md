# apps-board-mcp

A working MCP App: an interactive kanban board served over MCP, built
with zero dependencies. A single-file Python MCP server (stdio)
implementing the server side of MCP Apps
(`io.modelcontextprotocol/ui`, [SEP-1865](https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp),
spec version 2026-01-26) on the stateless 2026-07-28 core protocol,
plus a self-contained HTML view that speaks the view-side bridge
protocol with no SDK.

MCP Apps is the standard way for MCP servers to ship interactive user
interfaces - the pattern the MCP-UI community proved out with hosts
and providers like Postman, HuggingFace, Shopify and Goose, now a
Final SEP. Tooling so far centers on TypeScript: the official
[ext-apps SDK](https://github.com/modelcontextprotocol/ext-apps),
frameworks like [skybridge](https://github.com/alpic-ai/skybridge) and
[mcp-use](https://github.com/mcp-use/mcp-use), and the
[MCPJam inspector](https://github.com/MCPJam/inspector) for testing.
apps-board-mcp is the whole pattern - server, UI resource, tool
linkage, bridge - in one Python file and one HTML file you can read
end to end. (GitHub search, 2026-11-04: no other zero-dependency
Python MCP Apps server found; the space is TS-framework-led.)

## What you get

Five tools and one UI resource (`ui://apps-board/board`):

| Tool          | Visibility        | Purpose                                |
| ------------- | ----------------- | -------------------------------------- |
| `get_board`   | model + app       | Read the board (text + structured)     |
| `add_card`    | model + app       | Add a card to a column                 |
| `move_card`   | app only          | Drag a card to a column/position       |
| `rename_card` | app only          | Rename a card                          |
| `delete_card` | app only          | Delete a card                          |

The agent reads and adds cards like any tool call. On a host that
supports MCP Apps, the tool result also renders as the interactive
board: drag cards between columns, double-click to rename, delete with
a click - those actions reach the server through the app-only tools,
proxied by the host. Every mutation returns the fresh board as
`structuredContent` plus a text fallback, and the view pushes a board
summary back to the model with `ui/update-model-context`, so the agent
always knows what the user changed by hand.

## Run it

```sh
python3 apps_board_mcp.py                  # in-memory board
python3 apps_board_mcp.py --state-file board.json   # persist across restarts
```

Client configuration (stdio):

```json
{
  "mcpServers": {
    "board": {
      "command": "python3",
      "args": ["/path/to/apps_board_mcp.py", "--state-file", "/path/to/board.json"]
    }
  }
}
```

The view (`apps/board.html`, next to the script by default; override
with `--apps-dir`) is served as a `ui://` resource with the
`text/html;profile=mcp-app` MIME type. Opened directly in a browser it
renders a standalone preview with a banner, so you can look at it
without a host.

## Protocol notes

- Server side, per SEP-1865: `ui://` resources with
  `text/html;profile=mcp-app`, tools linked via
  `_meta.ui.resourceUri`, `visibility` arrays, CSP metadata in
  `_meta.ui` (this app is fully offline, so it declares no domains and
  gets the spec's restrictive default policy), extension declared as
  `io.modelcontextprotocol/ui` in `server/discover` capabilities.
- View side: `board.html` implements the bridge from the spec itself
  (JSON-RPC 2.0 over `postMessage`): `ui/initialize` handshake with
  `appCapabilities`, `ui/notifications/initialized`, then
  `tools/call` for user actions and `ui/notifications/tool-input` /
  `tool-result` / `host-context-changed` (theme switching) /
  `tool-cancelled` for host pushes, plus `ui/request-display-mode`
  (fullscreen button) and `ui/update-model-context`. No SDK, no build
  step, no external requests.
- Core protocol: stateless 2026-07-28 (`server/discover`, per-request
  `_meta` version) with a legacy `initialize` handshake for
  pre-2026-07-28 clients, which get the same tools as plain text.
- Per the spec, `visibility` enforcement is the host's job: the server
  advertises which tools are app-only; hosts hide them from the model
  and reject cross-server app calls.
- State: in-memory by default; `--state-file` persists with atomic
  replace. Card titles capped at 200 characters; unknown ids, bad
  columns and bad titles return tool-level `isError` results, not
  protocol errors.

## Security

The demo app makes no network requests and loads no external
resources, so the host's default CSP (`connect-src 'none'`,
`default-src 'none'` with inline script/style exceptions per the spec)
fully contains it. Sandboxing itself (iframe isolation, sandbox proxy,
CSP enforcement) is host-side per SEP-1865; this server's duties are
accurate metadata and safe content, and it does both: exact CSP
declarations, valid HTML5, no injection surface from card content
(the view builds the DOM with `textContent`, never `innerHTML`, for
card titles).

## Tests

24 stdlib `unittest` tests drive the real server over stdio as an MCP
client: capability discovery, resource listing and reads (MIME type,
`_meta.ui` metadata, HTML validity markers), offline-safety of the
view (no external URLs), bridge protocol markers, tool metadata
(linkage, visibility, schemas), the full board flow (add/move/
rename/delete, ordering, validation errors), state-file persistence
and corrupt-file recovery, version negotiation and the legacy
handshake. CI runs them on Ubuntu and Windows, Python 3.9 and 3.13.
The view was also rendered standalone in a real browser and
screenshot-checked.

```sh
python3 -m unittest -v
```

Not done (honest limits): no interop test against a real MCP Apps host
(Claude, ChatGPT, MCPJam) - the protocol surface is tested
end-to-end, and the bridge follows the spec's own minimal pattern, but
host behavior (sandbox proxy, display modes) is theirs to implement.
The spec's desktop-only and streaming paths
(`ui/notifications/tool-input-partial`) are handled as optional per
the spec.

## Sources

- [SEP-1865: MCP Apps](https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp) and the [ext-apps specification](https://github.com/modelcontextprotocol/ext-apps/tree/main/specification) (version 2026-01-26, fetched 2026-11-04)
- [SEP-2133: Extensions](https://modelcontextprotocol.io/seps/2133-extensions), [SEP-2575: Stateless MCP](https://modelcontextprotocol.io/seps/2575-stateless-mcp), [MCP specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)
- [ext-apps SDK](https://github.com/modelcontextprotocol/ext-apps), [skybridge](https://github.com/alpic-ai/skybridge), [mcp-use](https://github.com/mcp-use/mcp-use), [MCPJam inspector](https://github.com/MCPJam/inspector)

## License

MIT
