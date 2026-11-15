"""Run as `python -m app.mcp_server`; all registered tools are preview-only."""
import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .capability_catalog import CapabilityCatalog
from .capability_tools import CapabilityTools, entrypoint_schemas
from .config import Settings
from .store import Store


def make_server(store=None, catalog=None):
    store = store or Store(Settings().db_path)
    catalog = catalog or CapabilityCatalog(store, Settings().catalog_dir, initialize=False)
    previews = CapabilityTools(store, catalog)
    server = Server("vehicle-call-preview")

    @server.list_tools()
    async def list_tools():
        return [Tool(**tool) for tool in entrypoint_schemas()]

    @server.call_tool()
    async def call_tool(name, arguments):
        result = previews.call(name, arguments or {})
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    return server


async def main():
    server = make_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
