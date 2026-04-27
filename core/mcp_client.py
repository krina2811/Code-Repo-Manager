"""
MCP stdio client.

Spawns mcp_server/server.py as a subprocess and communicates via
stdin/stdout using the MCP JSON-RPC protocol.

Usage in agents:
    from core.mcp_client import mcp_client
    result = mcp_client.call_tool("analyze_imports", {"file_path": path})

The subprocess is started once (at FastAPI startup) and reused for
every tool call — no per-call subprocess overhead.
"""

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from core.logger import get_logger

logger = get_logger(__name__)

_SERVER_PATH = str(Path(__file__).parent.parent / "mcp_server" / "server.py")


class MCPClient:
    """
    Synchronous MCP stdio client.

    Sends JSON-RPC requests to the MCP server subprocess over stdin,
    reads responses from stdout. Thread-safe via a lock so multiple
    agents can share one client instance.
    """

    def __init__(self):
        self._proc:   Optional[subprocess.Popen] = None
        self._lock    = threading.Lock()
        self._req_id  = 0

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self):
        """Spawn the MCP server subprocess. Called once at app startup."""
        if self._proc and self._proc.poll() is None:
            logger.debug("MCP server already running (pid=%d)", self._proc.pid)
            return

        logger.info("Starting MCP server subprocess: %s", _SERVER_PATH)
        import os
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"   # prevent stdout buffering in subprocess

        self._proc = subprocess.Popen(
            [sys.executable, _SERVER_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,          # line-buffered
            env=env,
        )
        logger.info("MCP server started (pid=%d)", self._proc.pid)

        # Send MCP initialize handshake
        self._initialize()

    def stop(self):
        """Terminate the MCP server subprocess. Called at app shutdown."""
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            logger.info("MCP server stopped")
        self._proc = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── MCP protocol ──────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _send(self, method: str, params: Dict) -> Any:
        """Send one JSON-RPC request and return the result."""
        if not self.running:
            raise RuntimeError("MCP server is not running — call start() first")

        req = {
            "jsonrpc": "2.0",
            "id":      self._next_id(),
            "method":  method,
            "params":  params,
        }

        with self._lock:
            line = json.dumps(req) + "\n"
            self._proc.stdin.write(line)
            self._proc.stdin.flush()

            raw = self._proc.stdout.readline()
            if not raw:
                raise RuntimeError("MCP server closed stdout unexpectedly")

        resp = json.loads(raw)

        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']}")

        return resp.get("result")

    def _initialize(self):
        """Send MCP initialize + initialized notification."""
        try:
            self._send("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities":    {},
                "clientInfo":      {"name": "code-repo-manager", "version": "1.0"},
            })
            # initialized is a notification (no id, no response expected)
            notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            with self._lock:
                self._proc.stdin.write(notif)
                self._proc.stdin.flush()
            logger.info("MCP handshake complete")
        except Exception as e:
            logger.warning("MCP initialize failed: %s — falling back to direct calls", e)

    # ── Public API ────────────────────────────────────────────────────

    def list_tools(self) -> list:
        """Return list of available tools from the MCP server."""
        result = self._send("tools/list", {})
        return result.get("tools", []) if result else []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """
        Call a tool by name and return its result.

        Falls back to direct CodeAnalysisTools call if the MCP server
        is not running (e.g. during local dev without subprocess).
        """
        if not self.running:
            logger.debug("MCP server not running — using direct fallback for '%s'", name)
            return self._direct_fallback(name, arguments)

        try:
            result = self._send("tools/call", {"name": name, "arguments": arguments})
            # MCP returns content as list of {type, text} objects
            if result and isinstance(result.get("content"), list):
                text = result["content"][0].get("text", "{}")
                return json.loads(text)
            return result
        except Exception as e:
            logger.warning("MCP call_tool '%s' failed: %s — using direct fallback", name, e)
            return self._direct_fallback(name, arguments)

    def _direct_fallback(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Call CodeAnalysisTools directly when MCP server is unavailable."""
        from mcp_server.tools.code_analysis import TOOLS
        if name not in TOOLS:
            return {"error": f"Unknown tool: {name}"}
        try:
            return TOOLS[name](**arguments)
        except Exception as e:
            return {"error": str(e)}


    # ── Convenience methods matching CodeAnalysisTools interface ─────────
    # Agents call self.tools.analyze_imports(path) — these proxy through
    # call_tool() so the MCP protocol is used transparently.

    def get_python_files(self, repo_path: str, exclude_patterns=None) -> list:
        args = {"repo_path": repo_path}
        if exclude_patterns:
            args["exclude_patterns"] = exclude_patterns
        return self.call_tool("get_python_files", args)

    def analyze_imports(self, file_path: str) -> dict:
        return self.call_tool("analyze_imports", {"file_path": file_path})

    def analyze_functions(self, file_path: str) -> dict:
        return self.call_tool("analyze_functions", {"file_path": file_path})

    def analyze_security(self, file_path: str) -> dict:
        return self.call_tool("analyze_security", {"file_path": file_path})

    def analyze_documentation(self, file_path: str) -> dict:
        return self.call_tool("analyze_documentation", {"file_path": file_path})

    def get_file_metrics(self, file_path: str) -> dict:
        return self.call_tool("get_file_metrics", {"file_path": file_path})


# ── Module-level singleton ────────────────────────────────────────────────────

mcp_client = MCPClient()
