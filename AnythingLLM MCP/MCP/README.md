# AnythingLLM MCP Server

把问题转发给本机 AnythingLLM 的**唯一工作区**做 RAG 检索并回答。单工具：`ask_workspace`。

传输方式：Streamable HTTP（MCP 协议 `2026-07-28`，由 SDK 自动协商）。

## 一、启动 / 停止

所有命令的工作目录：`C:\Users\HUAWEI\Desktop\LLM\MCP`

### 前台启动（推荐，调试时用）

```bash
cd C:\Users\HUAWEI\Desktop\LLM\MCP
python server.py
```

看到这行就是起来了：

```
INFO:     Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)
```

**停止**：在该窗口按 `Ctrl+C`。

### 后台启动（脱离终端，关掉窗口也不停）

```bash
powershell -Command "Start-Process -FilePath python -ArgumentList 'server.py' -WorkingDirectory 'C:\Users\HUAWEI\Desktop\LLM\MCP' -WindowStyle Hidden"
```

（CMD 里也可用 `start /b python server.py`；Git Bash 里用 `python server.py &`。这两种都跟着当前终端，关了就没。）

**停止**：先查端口占用拿到 PID，再杀掉。

```cmd
netstat -ano | findstr :8765
taskkill /PID <上面查到的PID> /F
```

例如：

```
TCP    127.0.0.1:8765    0.0.0.0:0    LISTENING    28576
```

```cmd
taskkill /PID 28576 /F
```

> PowerShell 用户：`Stop-Process -Id <PID>`，查 PID 用 `Get-NetTCPConnection -LocalPort 8765`。

### 确认服务在跑

```cmd
netstat -ano | findstr :8765
```

有 `LISTENING` 就是活着。没有输出就是没起来。

## 二、客户端配置

项目级配置已写在 `C:\Users\HUAWEI\Desktop\LLM\.mcp.json`：

```json
{
  "mcpServers": {
    "anythingllm": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

### 首次使用：批准一次（必须做，否则工具不生效）

项目级 MCP 默认不自动启用，这是 Claude Code 的安全策略。批准步骤：

1. 在项目目录下启动 Claude Code：

   ```bash
   cd C:\Users\HUAWEI\Desktop\LLM
   claude
   ```

2. 启动时会提示发现了新的 MCP server，**选择批准 / Yes**。
3. 确认生效：会话里输入 `/mcp` 查看，或退出后在项目目录执行：

   ```bash
   claude mcp list
   ```

   看到下面这行就成功了（不再是 `Pending approval`）：

   ```
   anythingllm: http://127.0.0.1:8765/mcp (HTTP) - ✔ Connected
   ```

### 已经批准过了（当前状态）

批准记录在 `C:\Users\HUAWEI\Desktop\LLM\.claude\settings.local.json`：

```json
{ "enabledMcpjsonServers": ["anythingllm"] }
```

**注意这是 `.local.json`**（个人本地设置，不会提交到 git）。改成下面这样就是**取消启用**：

```json
{ "disabledMcpjsonServers": ["anythingllm"] }
```

改完的文件对**新启动**的 Claude Code 生效，当前已开着的会话需要重启。

> 踩过的坑：如果 `~/.claude.json` 里还没有这个项目的条目，光写 settings.local.json 会一直显示 `Pending approval`。先在该目录跑一次 `claude`（让它建出项目条目），之后再配这个文件就正常显示 `✔ Connected` 了。

## 三、环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ANYTHINGLLM_BASE_URL` | `http://localhost:3001` | AnythingLLM 地址 |
| `ANYTHINGLLM_API_KEY` | **无，必须设置** | AnythingLLM API Key |
| `MCP_PORT` | `8765` | 本服务端口（3001 被 AnythingLLM 占用） |
| `ANYTHINGLLM_TIMEOUT` | `300` | 单次提问超时秒数 |

`ANYTHINGLLM_API_KEY` 没有默认值，不设置会直接报错退出。在 AnythingLLM「设置 → API Keys」生成后设置：

```powershell
$env:ANYTHINGLLM_API_KEY = "你的key"   # 当前窗口有效
setx ANYTHINGLLM_API_KEY "你的key"     # 永久（需重开终端）
```

## 四、前置条件

1. **AnythingLLM 必须在运行**（端口 3001），否则工具调用会报连接错误。
2. 必须有**恰好 1 个**工作区。0 个或多个都会直接报错（MVP 不做选择逻辑）。
3. 工作区里的 LLM 建议配**非推理模型**。实测推理模型单次回答要 110s+，换成普通模型后只要 12s。
