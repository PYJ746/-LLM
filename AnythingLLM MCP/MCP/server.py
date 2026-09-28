"""AnythingLLM MCP Server —— 向本机 AnythingLLM 的唯一工作区提问。

传输方式：Streamable HTTP（MCP 协议 2026-07-28，由 SDK 自动协商）
"""

import os
import re

import httpx
from mcp.server import MCPServer

BASE = os.environ.get("ANYTHINGLLM_BASE_URL", "http://localhost:3001")
KEY = os.environ.get("ANYTHINGLLM_API_KEY")
if not KEY:
    raise RuntimeError(
        "未设置环境变量 ANYTHINGLLM_API_KEY。\n"
        "在 AnythingLLM「设置 → API Keys」生成后设置到环境变量，例如：\n"
        '  PowerShell: $env:ANYTHINGLLM_API_KEY = "你的key"\n'
        "  CMD:        set ANYTHINGLLM_API_KEY=你的key"
    )
PORT = int(os.environ.get("MCP_PORT", "8765"))  # 3001 已被 AnythingLLM 占用
# 工作区若用推理模型，单次回答实测要 110s+，超时给足。
TIMEOUT = float(os.environ.get("ANYTHINGLLM_TIMEOUT", "300"))

HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

mcp = MCPServer("anythingllm", version="0.1.0")

_slug: str | None = None


async def _workspace_slug(client: httpx.AsyncClient) -> str:
    """取唯一工作区的 slug，首次调用后缓存。"""
    global _slug
    if _slug:
        return _slug
    resp = await client.get("/api/v1/workspaces")
    resp.raise_for_status()
    workspaces = resp.json()["workspaces"]
    if len(workspaces) != 1:
        raise RuntimeError(f"需要恰好 1 个工作区，实际有 {len(workspaces)} 个")
    _slug = workspaces[0]["slug"]
    return _slug


@mcp.tool()
async def ask_workspace(question: str) -> str:
    """向 AnythingLLM 唯一工作区提问，基于该工作区知识库的内容回答。"""
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=TIMEOUT) as client:
        slug = await _workspace_slug(client)
        # 超时异常自带的 str() 是空的，不翻译的话调用方只看到一句没内容的报错。
        try:
            resp = await client.post(
                f"/api/v1/workspace/{slug}/chat",
                json={"message": question, "mode": "query"},
            )
        except httpx.TimeoutException:
            raise RuntimeError(f"AnythingLLM 超过 {TIMEOUT:.0f}s 未响应")

    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"AnythingLLM 返回非 JSON：{resp.text[:200]}")

    # 失败时真正的原因在 body 的 error 字段里（HTTP 500 也带），
    # 只看状态码会把 "xxx is not valid for chat completion" 丢掉。
    if data.get("error"):
        raise RuntimeError(f"AnythingLLM 报错：{data['error']}")
    if not resp.is_success:
        raise RuntimeError(f"AnythingLLM HTTP {resp.status_code}：{resp.text[:200]}")

    text = data.get("textResponse") or "（工作区没有返回内容）"
    # 推理模型会把整段思维链一起吐出来（实测占返回内容的 96%），
    # 剥掉再返回，否则会灌满调用方的上下文。
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip() or text


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=PORT, streamable_http_path="/mcp")
