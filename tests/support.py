"""An MCP client wired to a server in memory, so tests speak the protocol the clients speak."""
from __future__ import annotations

from contextlib import asynccontextmanager

import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from phone_harness_mcp.cloud import Cloud, StaticToken
from phone_harness_mcp.server import build_server


def stdio_server(api, token="pck_test", allow_files=True):
    cloud = Cloud(StaticToken(token), api.url)
    return build_server(cloud_for=lambda ctx: cloud, allow_files=allow_files)


@asynccontextmanager
async def connected(server):
    async with create_client_server_memory_streams() as ((client_read, client_write), (server_read, server_write)):
        async with anyio.create_task_group() as tg:
            low = server._lowlevel_server
            tg.start_soon(low.run, server_read, server_write, low.create_initialization_options())
            async with ClientSession(client_read, client_write) as session:
                await session.initialize()
                yield session
            tg.cancel_scope.cancel()


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")


async def start(session, **args) -> str:
    """Start a phone through the tool and return its device id."""
    r = await session.call_tool("phone_start_device", args)
    assert not r.is_error, text_of(r)
    text = text_of(r)
    return text.split("as device ", 1)[1].split(".", 1)[0]
