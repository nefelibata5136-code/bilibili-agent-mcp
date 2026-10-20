"""Exercise the installed MCP stdio transport against an empty isolated state."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temporary:
        env = dict(os.environ, BILI_LIFE_ID="fixture-agent", BILI_OWNER_UID="123")
        env.pop("DASHSCOPE_API_KEY", None)
        params = StdioServerParameters(command=sys.executable, args=["-B", str(root / "readonly_server.py"),
            "--state", temporary], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert len(tools.tools) == 35
                names = {tool.name for tool in tools.tools}
                assert "bili_discover" in names and "bili_reply" not in names
                who = await client.call_tool("bili_whoami", {})
                assert '"logged_in": false' in str(who)
                denied = await client.call_tool("bili_comment_send", {
                    "bvid": "BV1xx411c7mD", "text": "fixture", "action_id": "stdio-fixture-001"})
                assert "SOCIAL_PERMISSION_DISABLED" in str(denied)
                print(json.dumps({"stdio": "passed", "tools": len(names), "default_write_gate": "denied",
                                  "real_credentials": False, "paid_model_calls": 0}))


if __name__ == "__main__":
    asyncio.run(main())
