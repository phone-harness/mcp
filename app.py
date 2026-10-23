"""Vercel entrypoint: the hosted MCP server as one function. See src/phone_harness_mcp/vercel.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from phone_harness_mcp.vercel import app  # noqa: E402,F401
