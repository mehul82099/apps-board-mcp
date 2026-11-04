#!/usr/bin/env python3
"""apps-board-mcp: an MCP Apps server with a working interactive demo.

Zero-dependency Python MCP server (stdio, newline-delimited JSON-RPC)
implementing the server side of MCP Apps (io.modelcontextprotocol/ui,
SEP-1865, spec version 2026-01-26) on the stateless 2026-07-28 core
protocol (SEP-2575).

The demo app is a kanban board: the model can read and add cards, and
the UI (apps/board.html) lets the user drag cards between columns,
rename and delete them through app-only tools proxied by the host.

Stdlib only. Python 3.9+.
"""

import argparse
import json
import os
import sys
import uuid

PROTOCOL_CURRENT = "2026-07-28"
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18",
                      "2025-03-26", "2024-11-05"]
UI_EXT = "io.modelcontextprotocol/ui"
SERVER_INFO = {"name": "apps-board-mcp", "version": "1.0.0"}
UI_MIME = "text/html;profile=mcp-app"
BOARD_URI = "ui://apps-board/board"

E_PARSE = -32700
E_INVALID_REQUEST = -32600
E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602
E_INTERNAL = -32603
E_MISSING_CLIENT_CAP = -32021  # spec 2026-07-28 error table
E_UNSUPPORTED_VERSION = -32022

COLUMNS = [("todo", "To Do"), ("doing", "Doing"), ("done", "Done")]
MAX_TITLE = 200


# ---------------------------------------------------------------------------
# Board state
# ---------------------------------------------------------------------------

class Board:
    """In-memory kanban board, optionally persisted to a JSON file."""

    def __init__(self, state_file=None):
        self.state_file = state_file
        self.cards = []  # [{"id", "title", "column"}] in display order
        if state_file and os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                for card in data.get("cards", []):
                    if (isinstance(card.get("id"), str)
                            and isinstance(card.get("title"), str)
                            and card.get("column") in self.column_ids()):
                        self.cards.append({"id": card["id"],
                                           "title": card["title"][:MAX_TITLE],
                                           "column": card["column"]})
            except (OSError, ValueError) as exc:
                sys.stderr.write("apps-board-mcp: ignoring state file: %s\n"
                                 % exc)

    @staticmethod
    def column_ids():
        return [cid for cid, _t in COLUMNS]

    def _save(self):
        if not self.state_file:
            return
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cards": self.cards}, fh, indent=2)
        os.replace(tmp, self.state_file)

    def snapshot(self):
        return {"columns": [{"id": cid, "title": title,
                             "cards": [dict(c) for c in self.cards
                                       if c["column"] == cid]}
                            for cid, title in COLUMNS]}

    def summary(self):
        lines = ["Board: " + ", ".join(
            "%s (%d)" % (title, sum(1 for c in self.cards
                                    if c["column"] == cid))
            for cid, title in COLUMNS)]
        for cid, title in COLUMNS:
            for c in self.cards:
                if c["column"] == cid:
                    lines.append("- [%s] %s: %s" % (cid, c["id"], c["title"]))
        if len(lines) == 1:
            lines.append("(empty board - add a card to get started)")
        return "\n".join(lines)

    def find(self, card_id):
        for card in self.cards:
            if card["id"] == card_id:
                return card
        return None

    def add(self, title, column):
        card = {"id": "c-" + uuid.uuid4().hex[:8],
                "title": title, "column": column}
        self.cards.append(card)
        self._save()
        return card

    def move(self, card, column, position):
        self.cards.remove(card)
        card["column"] = column
        siblings = [c for c in self.cards if c["column"] == column]
        if position is None:
            index = len(self.cards)
            for i in range(len(self.cards) - 1, -1, -1):
                if self.cards[i]["column"] == column:
                    index = i + 1
                    break
            self.cards.insert(index, card)
        else:
            position = max(0, min(int(position), len(siblings)))
            seen = 0
            index = len(self.cards)
            for i, c in enumerate(self.cards):
                if c["column"] == column:
                    if seen == position:
                        index = i
                        break
                    seen += 1
            self.cards.insert(index, card)
        self._save()

    def rename(self, card, title):
        card["title"] = title
        self._save()

    def delete(self, card):
        self.cards.remove(card)
        self._save()


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, cfg, board, board_html):
        self.cfg = cfg
        self.board = board
        self.board_html = board_html
        self.legacy_version = None

    # -- helpers -----------------------------------------------------------

    def _meta(self):
        return {"io.modelcontextprotocol/serverInfo": SERVER_INFO}

    def _ok(self, rid, result):
        result = dict(result)
        result["resultType"] = "complete"
        result["_meta"] = self._meta()
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _err(self, rid, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": rid, "error": err}

    def _request_version(self, msg):
        meta = (msg.get("params") or {}).get("_meta") or {}
        return meta.get("io.modelcontextprotocol/protocolVersion")

    def _check_version(self, msg):
        version = self._request_version(msg)
        if version is None:
            if self.legacy_version is not None:
                return None
            if msg.get("method") in ("server/discover", "initialize"):
                return None
            return self._err(msg.get("id"), E_INVALID_PARAMS,
                             "Missing io.modelcontextprotocol/"
                             "protocolVersion in request _meta")
        if version not in SUPPORTED_VERSIONS:
            return self._err(msg.get("id"), E_UNSUPPORTED_VERSION,
                             "Unsupported protocol version",
                             {"supported": SUPPORTED_VERSIONS,
                              "requested": version})
        return None

    def _tool_result(self, text, structured=None, is_error=False):
        result = {"content": [{"type": "text", "text": text}]}
        if structured is not None:
            result["structuredContent"] = structured
        if is_error:
            result["isError"] = True
        return result

    # -- tools -------------------------------------------------------------

    def _tools(self):
        ui = {"resourceUri": BOARD_URI}
        return [
            {"name": "get_board",
             "description": "Read the kanban board: every column and card "
                            "with ids. Interactive board UI available on "
                            "MCP Apps hosts.",
             "inputSchema": {"type": "object", "properties": {},
                             "additionalProperties": False},
             "_meta": {"ui": dict(ui, visibility=["model", "app"])}},
            {"name": "add_card",
             "description": "Add a card to the board. Returns the updated "
                            "board.",
             "inputSchema": {"type": "object",
                             "properties": {
                                 "title": {"type": "string",
                                           "description": "Card title "
                                                          "(1-200 chars)"},
                                 "column": {"type": "string",
                                            "enum": Board.column_ids(),
                                            "description": "Target column "
                                                           "(default: todo)"}},
                             "required": ["title"],
                             "additionalProperties": False},
             "_meta": {"ui": dict(ui, visibility=["model", "app"])}},
            {"name": "move_card",
             "description": "Move a card to a column, optionally at a "
                            "position. App-only: callable from the board UI.",
             "inputSchema": {"type": "object",
                             "properties": {
                                 "id": {"type": "string"},
                                 "column": {"type": "string",
                                            "enum": Board.column_ids()},
                                 "position": {"type": "integer",
                                              "minimum": 0}},
                             "required": ["id", "column"],
                             "additionalProperties": False},
             "_meta": {"ui": dict(ui, visibility=["app"])}},
            {"name": "rename_card",
             "description": "Rename a card. App-only: callable from the "
                            "board UI.",
             "inputSchema": {"type": "object",
                             "properties": {
                                 "id": {"type": "string"},
                                 "title": {"type": "string"}},
                             "required": ["id", "title"],
                             "additionalProperties": False},
             "_meta": {"ui": dict(ui, visibility=["app"])}},
            {"name": "delete_card",
             "description": "Delete a card. App-only: callable from the "
                            "board UI.",
             "inputSchema": {"type": "object",
                             "properties": {"id": {"type": "string"}},
                             "required": ["id"],
                             "additionalProperties": False},
             "_meta": {"ui": dict(ui, visibility=["app"])}},
        ]

    def _call(self, name, arguments):
        board = self.board
        if name == "get_board":
            return self._tool_result(board.summary(),
                                     {"board": board.snapshot()})
        if name == "add_card":
            title = arguments.get("title")
            column = arguments.get("column", "todo")
            if (not isinstance(title, str) or not title.strip()
                    or len(title) > MAX_TITLE):
                return self._tool_result(
                    "title must be a non-empty string of at most %d "
                    "characters" % MAX_TITLE, is_error=True)
            if column not in Board.column_ids():
                return self._tool_result(
                    "unknown column %r (one of %s)"
                    % (column, ", ".join(Board.column_ids())), is_error=True)
            card = board.add(title.strip(), column)
            return self._tool_result(
                "Added %s to %s.\n%s" % (card["id"], column,
                                         board.summary()),
                {"card": dict(card), "board": board.snapshot()})
        if name == "move_card":
            card = board.find(arguments.get("id"))
            column = arguments.get("column")
            if card is None:
                return self._tool_result(
                    "no card with id %r" % (arguments.get("id"),),
                    is_error=True)
            if column not in Board.column_ids():
                return self._tool_result("unknown column %r" % (column,),
                                         is_error=True)
            position = arguments.get("position")
            if position is not None and not isinstance(position, int):
                return self._tool_result("position must be an integer",
                                         is_error=True)
            board.move(card, column, position)
            return self._tool_result(
                "Moved %s to %s." % (card["id"], column),
                {"board": board.snapshot()})
        if name == "rename_card":
            card = board.find(arguments.get("id"))
            title = arguments.get("title")
            if card is None:
                return self._tool_result(
                    "no card with id %r" % (arguments.get("id"),),
                    is_error=True)
            if (not isinstance(title, str) or not title.strip()
                    or len(title) > MAX_TITLE):
                return self._tool_result(
                    "title must be a non-empty string of at most %d "
                    "characters" % MAX_TITLE, is_error=True)
            board.rename(card, title.strip())
            return self._tool_result(
                "Renamed %s." % card["id"],
                {"board": board.snapshot()})
        if name == "delete_card":
            card = board.find(arguments.get("id"))
            if card is None:
                return self._tool_result(
                    "no card with id %r" % (arguments.get("id"),),
                    is_error=True)
            board.delete(card)
            return self._tool_result(
                "Deleted %s." % card["id"],
                {"board": board.snapshot()})
        return None

    # -- methods -----------------------------------------------------------

    def m_discover(self, rid, msg):
        return self._ok(rid, {
            "supportedVersions": SUPPORTED_VERSIONS,
            "capabilities": {
                "resources": {},
                "tools": {},
                "extensions": {UI_EXT: {}},
            },
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Interactive kanban board (MCP Apps demo). get_board and "
                "add_card are model tools; the board UI at %s lets the "
                "user drag, rename and delete cards via app-only tools. "
                "All tools return a text fallback plus structuredContent."
                % BOARD_URI),
        })

    def m_initialize(self, rid, msg):
        """Legacy handshake for pre-2026-07-28 clients."""
        params = msg.get("params") or {}
        requested = params.get("protocolVersion", "2025-11-25")
        negotiated = (requested if requested in SUPPORTED_VERSIONS
                      else "2025-11-25")
        self.legacy_version = negotiated
        return self._ok(rid, {
            "protocolVersion": negotiated,
            "capabilities": {"resources": {}, "tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": "Kanban board server. Hosts without MCP Apps "
                            "support can use every tool as plain text.",
        })

    def m_tools_list(self, rid, msg):
        return self._ok(rid, {"tools": self._tools(),
                              "ttlMs": 60000, "cacheScope": "public"})

    def m_tools_call(self, rid, msg):
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._err(rid, E_INVALID_PARAMS,
                             "arguments must be an object")
        if name not in {t["name"] for t in self._tools()}:
            return self._err(rid, E_INVALID_PARAMS,
                             "Unknown tool: %r" % (name,))
        try:
            result = self._call(name, arguments)
        except Exception as exc:
            return self._ok(rid, self._tool_result(
                "Internal error: %s" % exc, is_error=True))
        return self._ok(rid, result)

    def m_resources_list(self, rid, msg):
        return self._ok(rid, {
            "resources": [{
                "uri": BOARD_URI,
                "name": "Kanban board UI",
                "description": "Interactive board view: drag cards "
                               "between columns, rename, delete.",
                "mimeType": UI_MIME,
            }],
            "ttlMs": 60000,
            "cacheScope": "public",
        })

    def m_resources_read(self, rid, msg):
        params = msg.get("params") or {}
        uri = params.get("uri")
        if uri != BOARD_URI:
            return self._err(rid, E_INVALID_PARAMS,
                             "Unknown resource: %r" % (uri,))
        return self._ok(rid, {
            "contents": [{
                "uri": BOARD_URI,
                "mimeType": UI_MIME,
                "text": self.board_html,
                "_meta": {"ui": {"prefersBorder": True}},
            }],
        })

    # -- dispatch ----------------------------------------------------------

    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._err(None, E_INVALID_REQUEST, "Invalid Request")
        method = msg.get("method")
        rid = msg.get("id")
        if rid is None:  # notification: never respond
            return None
        if not isinstance(method, str):
            return self._err(rid, E_INVALID_REQUEST, "Invalid Request")
        version_error = self._check_version(msg)
        if version_error is not None:
            return version_error
        handlers = {
            "server/discover": self.m_discover,
            "initialize": self.m_initialize,
            "ping": lambda r, m: self._ok(r, {}),
            "tools/list": self.m_tools_list,
            "tools/call": self.m_tools_call,
            "resources/list": self.m_resources_list,
            "resources/read": self.m_resources_read,
        }
        handler = handlers.get(method)
        if handler is None:
            return self._err(rid, E_METHOD_NOT_FOUND,
                             "Method not found: %s" % method)
        try:
            return handler(rid, msg)
        except Exception as exc:  # last-resort protocol error
            return self._err(rid, E_INTERNAL, "Internal error: %s" % exc)

    def serve(self):
        out = sys.stdout
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                response = self._err(None, E_PARSE, "Parse error")
            else:
                response = self.handle(msg)
            if response is not None:
                out.write(json.dumps(response) + "\n")
                out.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="apps-board-mcp",
        description="MCP server (stdio): interactive kanban board "
                    "implementing the server side of MCP Apps "
                    "(io.modelcontextprotocol/ui, SEP-1865).")
    parser.add_argument("--apps-dir",
                        default=os.path.join(os.path.dirname(
                            os.path.abspath(__file__)), "apps"),
                        help="Directory containing board.html "
                             "(default: ./apps next to this script).")
    parser.add_argument("--state-file", default=None,
                        help="Optional JSON file to persist the board "
                             "across restarts.")
    args = parser.parse_args(argv)
    html_path = os.path.join(args.apps_dir, "board.html")
    try:
        with open(html_path, "r", encoding="utf-8") as fh:
            board_html = fh.read()
    except OSError as exc:
        sys.stderr.write("apps-board-mcp: cannot read %s: %s\n"
                         % (html_path, exc))
        sys.exit(2)
    Server(args, Board(args.state_file), board_html).serve()


if __name__ == "__main__":
    main()
