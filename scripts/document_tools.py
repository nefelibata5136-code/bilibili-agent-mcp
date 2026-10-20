"""Generate the actual registered tool directory without credentials/network."""
import asyncio
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from readonly_server import create_server


async def main():
    with tempfile.TemporaryDirectory() as temporary:
        server, _ = create_server(ROOT, Path(temporary))
        tools = await server.list_tools()
        lines = ["# MCP 工具目录", "", "以下是实际注册的35项业务工具。列出工具不等于允许发送；写权限由代码与本地policy控制。", ""]
        for tool in tools:
            parameters = ", ".join(tool.inputSchema.get("properties", {}))
            parameter_text = f"参数：`{parameters}`。" if parameters else "无参数。"
            lines += [f"## `{tool.name}`", "", tool.description or "", "", parameter_text, ""]
        target = ROOT / "docs" / "tools.md"
        target.parent.mkdir(exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
        print("Documented tools:", len(tools))


if __name__ == "__main__":
    asyncio.run(main())
