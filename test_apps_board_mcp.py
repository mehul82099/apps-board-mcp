#!/usr/bin/env python3
"""Tests for apps-board-mcp (stdlib unittest, no network).

Spawns the server as a subprocess and speaks newline-delimited JSON-RPC
over its stdio, exactly as an MCP client would.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "apps_board_mcp.py")

UI_EXT = "io.modelcontextprotocol/ui"
BOARD_URI = "ui://apps-board/board"
UI_MIME = "text/html;profile=mcp-app"


def meta(version="2026-07-28"):
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities":
            {"extensions": {UI_EXT: {"mimeTypes": [UI_MIME]}}},
    }


def p(version="2026-07-28"):
    return {"_meta": meta(version)}


class Client:
    """A minimal MCP stdio client for tests."""

    def __init__(self, extra_args=None):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER] + (extra_args or []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, cwd=HERE)
        self.next_id = 0

    def call(self, method, params=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + 30
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError("server closed stdout")
            reply = json.loads(line)
            if reply.get("id") == self.next_id:
                return reply
        raise AssertionError("no reply for %s" % method)

    def call_tool(self, name, arguments=None, version="2026-07-28"):
        params = p(version)
        params["name"] = name
        params["arguments"] = arguments or {}
        reply = self.call("tools/call", params)
        self.assert_not_error(reply)
        return reply["result"]

    def assert_not_error(self, reply):
        if "error" in reply:
            raise AssertionError("JSON-RPC error: %s" % reply["error"])

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.stdout.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def board_of(tool_result):
    return tool_result["structuredContent"]["board"]


def card_titles(board, column):
    for col in board["columns"]:
        if col["id"] == column:
            return [c["title"] for c in col["cards"]]
    raise AssertionError("no column %s" % column)


class Base(unittest.TestCase):
    extra_args = None

    def setUp(self):
        self.client = Client(self.extra_args)

    def tearDown(self):
        self.client.close()


class TestDiscovery(Base):
    def test_discover_declares_ui_extension(self):
        reply = self.client.call("server/discover", p())
        result = reply["result"]
        self.assertIn("resources", result["capabilities"])
        self.assertIn("tools", result["capabilities"])
        self.assertIn(UI_EXT, result["capabilities"]["extensions"])
        self.assertIn("2026-07-28", result["supportedVersions"])
        self.assertEqual(result["serverInfo"]["name"], "apps-board-mcp")

    def test_ping_and_unknown_method(self):
        self.assertNotIn("error", self.client.call("ping", p()))
        reply = self.client.call("ui/initialize", p())
        self.assertEqual(reply["error"]["code"], -32601)

    def test_version_negotiation(self):
        reply = self.client.call("tools/list", p("2099-01-01"))
        self.assertEqual(reply["error"]["code"], -32022)
        reply = self.client.call("tools/list", {})
        self.assertEqual(reply["error"]["code"], -32602)

    def test_legacy_initialize(self):
        reply = self.client.call("initialize",
                                 {"protocolVersion": "2025-11-25",
                                  "capabilities": {}})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-11-25")
        result = self.client.call_tool("get_board")
        self.assertIn("Board:", result["content"][0]["text"])


class TestResources(Base):
    def test_resources_list(self):
        reply = self.client.call("resources/list", p())
        result = reply["result"]
        self.assertEqual(result["cacheScope"], "public")
        resources = result["resources"]
        self.assertEqual(len(resources), 1)
        res = resources[0]
        self.assertEqual(res["uri"], BOARD_URI)
        self.assertEqual(res["mimeType"], UI_MIME)
        self.assertIn("name", res)
        self.assertIn("description", res)

    def test_resources_read(self):
        reply = self.client.call("resources/read",
                                 dict(p(), uri=BOARD_URI))
        content = reply["result"]["contents"][0]
        self.assertEqual(content["uri"], BOARD_URI)
        self.assertEqual(content["mimeType"], UI_MIME)
        self.assertTrue(content["text"].lstrip().startswith("<!DOCTYPE html>"))
        self.assertEqual(content["_meta"]["ui"], {"prefersBorder": True})
        self.assertNotIn("blob", content)

    def test_html_is_offline_safe(self):
        reply = self.client.call("resources/read",
                                 dict(p(), uri=BOARD_URI))
        html = reply["result"]["contents"][0]["text"]
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn("<link", html)

    def test_html_implements_bridge(self):
        reply = self.client.call("resources/read",
                                 dict(p(), uri=BOARD_URI))
        html = reply["result"]["contents"][0]["text"]
        for marker in ("ui/initialize", "ui/notifications/initialized",
                       "ui/notifications/tool-input",
                       "ui/notifications/tool-result",
                       "ui/notifications/host-context-changed",
                       "ui/request-display-mode",
                       "ui/update-model-context", "tools/call"):
            self.assertIn(marker, html)

    def test_read_unknown(self):
        for uri in ("ui://apps-board/other", "ui://",
                    "skill://x/SKILL.md", "https://evil.test/x"):
            reply = self.client.call("resources/read", dict(p(), uri=uri))
            self.assertEqual(reply["error"]["code"], -32602, uri)


class TestToolsList(Base):
    def tools_by_name(self):
        reply = self.client.call("tools/list", p())
        return {t["name"]: t for t in reply["result"]["tools"]}

    def test_all_tools_linked_to_ui(self):
        tools = self.tools_by_name()
        self.assertEqual(set(tools), {"get_board", "add_card", "move_card",
                                      "rename_card", "delete_card"})
        for tool in tools.values():
            self.assertEqual(tool["_meta"]["ui"]["resourceUri"], BOARD_URI,
                             tool["name"])

    def test_visibility(self):
        tools = self.tools_by_name()
        self.assertEqual(tools["get_board"]["_meta"]["ui"]["visibility"],
                         ["model", "app"])
        self.assertEqual(tools["add_card"]["_meta"]["ui"]["visibility"],
                         ["model", "app"])
        for name in ("move_card", "rename_card", "delete_card"):
            self.assertEqual(tools[name]["_meta"]["ui"]["visibility"],
                             ["app"], name)

    def test_schemas(self):
        tools = self.tools_by_name()
        self.assertEqual(tools["add_card"]["inputSchema"]["required"],
                         ["title"])
        self.assertEqual(tools["move_card"]["inputSchema"]["required"],
                         ["id", "column"])
        self.assertEqual(tools["delete_card"]["inputSchema"]["required"],
                         ["id"])
        enum = tools["add_card"]["inputSchema"]["properties"]["column"]["enum"]
        self.assertEqual(enum, ["todo", "doing", "done"])


class TestBoardFlow(Base):
    def test_empty_board(self):
        result = self.client.call_tool("get_board")
        self.assertIn("empty board", result["content"][0]["text"])
        board = board_of(result)
        self.assertEqual([c["id"] for c in board["columns"]],
                         ["todo", "doing", "done"])
        for col in board["columns"]:
            self.assertEqual(col["cards"], [])

    def test_add_and_read_back(self):
        result = self.client.call_tool(
            "add_card", {"title": "Fix login bug"})
        card = result["structuredContent"]["card"]
        self.assertTrue(card["id"].startswith("c-"))
        self.assertEqual(card["column"], "todo")
        board = board_of(self.client.call_tool("get_board"))
        self.assertEqual(card_titles(board, "todo"), ["Fix login bug"])
        self.assertIn(card["id"], self.client.call_tool("get_board")
                      ["content"][0]["text"])

    def test_add_column_and_trim(self):
        self.client.call_tool("add_card",
                              {"title": "  spaced  ", "column": "doing"})
        board = board_of(self.client.call_tool("get_board"))
        self.assertEqual(card_titles(board, "doing"), ["spaced"])

    def test_add_validation(self):
        for bad in ({}, {"title": ""}, {"title": "   "},
                    {"title": "x" * 201}, {"title": 42}):
            result = self.client.call_tool("add_card", bad)
            self.assertTrue(result.get("isError"), bad)
        result = self.client.call_tool("add_card",
                                       {"title": "ok", "column": "nope"})
        self.assertTrue(result.get("isError"))

    def test_move_flow(self):
        card = self.client.call_tool("add_card", {"title": "task"})
        cid = card["structuredContent"]["card"]["id"]
        result = self.client.call_tool("move_card",
                                       {"id": cid, "column": "doing"})
        self.assertNotIn("isError", result)
        board = board_of(result)
        self.assertEqual(card_titles(board, "doing"), ["task"])
        self.assertEqual(card_titles(board, "todo"), [])

    def test_move_position_ordering(self):
        for title in ("a", "b", "c"):
            self.client.call_tool("add_card", {"title": title})
        board = board_of(self.client.call_tool("get_board"))
        ids = {c["title"]: c["id"] for c in board["columns"][0]["cards"]}
        self.client.call_tool("move_card",
                              {"id": ids["c"], "column": "todo",
                               "position": 0})
        board = board_of(self.client.call_tool("get_board"))
        self.assertEqual(card_titles(board, "todo"), ["c", "a", "b"])

    def test_move_validation(self):
        result = self.client.call_tool("move_card",
                                       {"id": "c-nope", "column": "done"})
        self.assertTrue(result.get("isError"))
        card = self.client.call_tool("add_card", {"title": "x"})
        cid = card["structuredContent"]["card"]["id"]
        result = self.client.call_tool("move_card",
                                       {"id": cid, "column": "nope"})
        self.assertTrue(result.get("isError"))
        result = self.client.call_tool(
            "move_card", {"id": cid, "column": "done", "position": "high"})
        self.assertTrue(result.get("isError"))

    def test_rename_flow(self):
        card = self.client.call_tool("add_card", {"title": "old"})
        cid = card["structuredContent"]["card"]["id"]
        result = self.client.call_tool("rename_card",
                                       {"id": cid, "title": "new"})
        self.assertNotIn("isError", result)
        board = board_of(self.client.call_tool("get_board"))
        self.assertEqual(card_titles(board, "todo"), ["new"])
        result = self.client.call_tool("rename_card",
                                       {"id": "c-nope", "title": "x"})
        self.assertTrue(result.get("isError"))
        result = self.client.call_tool("rename_card",
                                       {"id": cid, "title": ""})
        self.assertTrue(result.get("isError"))

    def test_delete_flow(self):
        card = self.client.call_tool("add_card", {"title": "gone"})
        cid = card["structuredContent"]["card"]["id"]
        result = self.client.call_tool("delete_card", {"id": cid})
        self.assertNotIn("isError", result)
        board = board_of(self.client.call_tool("get_board"))
        self.assertEqual(card_titles(board, "todo"), [])
        result = self.client.call_tool("delete_card", {"id": cid})
        self.assertTrue(result.get("isError"))

    def test_unknown_tool(self):
        reply = self.client.call("tools/call",
                                 dict(p(), name="hack", arguments={}))
        self.assertEqual(reply["error"]["code"], -32602)


class TestPersistence(unittest.TestCase):
    def test_state_file_roundtrip(self):
        tmp = tempfile.TemporaryDirectory()
        state = os.path.join(tmp.name, "board.json")
        with Client(["--state-file", state]) as client:
            client.call_tool("add_card", {"title": "persist me",
                                          "column": "done"})
        self.assertTrue(os.path.exists(state))
        with Client(["--state-file", state]) as client:
            board = board_of(client.call_tool("get_board"))
            self.assertEqual(card_titles(board, "done"), ["persist me"])
        tmp.cleanup()

    def test_corrupt_state_file_ignored(self):
        tmp = tempfile.TemporaryDirectory()
        state = os.path.join(tmp.name, "board.json")
        with open(state, "w", encoding="utf-8", newline="") as fh:
            fh.write("{not json")
        with Client(["--state-file", state]) as client:
            result = client.call_tool("get_board")
            self.assertIn("empty board", result["content"][0]["text"])
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
