"""
MCP Server for Code Repository Manager

This server exposes code analysis tools that can be called by LangGraph agents.
"""

import asyncio
import json
from typing import Any, Dict
from mcp.server import Server
from mcp.types import Tool, TextContent
import sys
from pathlib import Path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from mcp_server.tools.code_analysis import TOOLS, CodeAnalysisTools


# Create MCP server instance
mcp_server = Server("code-analysis-server")


# Register tools
@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available tools."""
    return [
        Tool(
            name="analyze_imports",
            description="Analyze imports in a Python file to find unused ones. Returns used and unused imports.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the Python file to analyze"
                    }
                },
                "required": ["file_path"]
            }
        ),
        Tool(
            name="analyze_functions",
            description="Analyze functions to find unused ones and complexity issues. Returns unused functions and complex functions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the Python file to analyze"
                    }
                },
                "required": ["file_path"]
            }
        ),
        Tool(
            name="analyze_security",
            description="Analyze file for security issues like hardcoded secrets. Returns security findings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the Python file to analyze"
                    }
                },
                "required": ["file_path"]
            }
        ),
        Tool(
            name="analyze_documentation",
            description="Analyze documentation quality, finding missing docstrings. Returns documentation issues.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the Python file to analyze"
                    }
                },
                "required": ["file_path"]
            }
        ),
        Tool(
            name="get_file_metrics",
            description="Get basic code metrics (LOC, comments, etc.) for a file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the Python file"
                    }
                },
                "required": ["file_path"]
            }
        ),
        Tool(
            name="get_python_files",
            description="Get all Python files in a repository, excluding common patterns.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Path to the repository"
                    },
                    "exclude_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of patterns to exclude"
                    }
                },
                "required": ["repo_path"]
            }
        ),
    ]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> list[TextContent]:
    """Execute a tool and return results."""
    
    if name not in TOOLS:
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"Unknown tool: {name}"})
        )]
    
    try:
        # Call the tool function
        result = TOOLS[name](**arguments)
        
        return [TextContent(
            type="text",
            text=json.dumps(result, indent=2)
        )]
    
    except Exception as e:
        return [TextContent(
            type="text",
            text=json.dumps({"error": str(e)})
        )]


async def run_server():
    """Run the MCP server."""
    from mcp.server.stdio import stdio_server
    
    async with stdio_server() as (read_stream, write_stream):
        await mcp_server.run(
            read_stream,
            write_stream,
            mcp_server.create_initialization_options()
        )


if __name__ == "__main__":
    # Force unbuffered stdout so responses reach the client immediately
    # when running as a subprocess (default line-buffering causes hangs)
    import os
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    asyncio.run(run_server())