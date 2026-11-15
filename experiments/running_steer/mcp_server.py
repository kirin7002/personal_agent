"""Production CapabilityTools with barriers immediately before MCP dispatch."""
import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from app.capability_catalog import CapabilityCatalog
from app.capability_tools import CapabilityTools, entrypoint_schemas
from app.config import Settings
from app.store import Store
from .barriers import before_tool, setup


async def main():
    settings = Settings()
    store = Store(settings.db_path)
    setup(store)
    service = CapabilityTools(store, CapabilityCatalog(store, settings.catalog_dir, initialize=False))
    server = Server("vehicle-call-preview")

    @server.list_tools()
    async def listed():
        return [Tool(**entry) for entry in entrypoint_schemas()]

    @server.call_tool()
    async def called(name, arguments):
        arguments = arguments or {}
        await before_tool(store, name, arguments)
        result = service.call(name, arguments)
        # A compact audit records outcomes, never provider headers or configuration.
        tid = arguments.get("task_id")
        if tid:
            try:
                store.event(tid, "validation.tool", {
                    "name": name, "task_revision": arguments.get("task_revision"),
                    "error_code": (result.get("error") or {}).get("code"),
                    "status": result.get("status"),
                })
            except KeyError:
                pass
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
