# pet-hospital-mcp

一个独立的 MCP 服务，把本仓库的 Go 宠物医院 REST API 暴露给 AI Agent。

**当前只提供一个工具：`list_pets`。** 本次未实现阶段二。

| 项目 | 值 |
| --- | --- |
| 语言 | Python 3.11+ |
| 官方 Python SDK | `mcp==2.0.0`（**精确固定**） |
| MCP 协议版本 | `2026-07-28` |
| 服务端类 | `mcp.server.mcpserver.MCPServer` |
| 传输 | 无状态 Streamable HTTP（`stateless_http=True`） |
| MCP 端点 | `POST http://127.0.0.1:8000/mcp` |
| 健康检查 | `GET http://127.0.0.1:8000/health` |
| 业务后端 | Go 宠物医院 REST API（唯一后端，本服务不修改它） |

## 这是一个什么样的 MCP 服务

- **无状态**：每个请求自带全部信息。没有 `initialize` 握手、没有 `Mcp-Session-Id`、
  没有会话存储、没有会话过期、没有 `max_sessions`、没有 SSE 断线恢复。
  客户端能力（client capabilities）在**每个请求**的 `params._meta` 中声明，
  而不是像旧协议那样在初始化时协商一次。
- **不使用 FastMCP**：不导入 `mcp.server.fastmcp`（SDK 2.x 已移除该模块）。
- **唯一的业务后端**是 Go REST API，本服务只通过 HTTP 调用它。

## 安装与启动

### 1. 先启动 Go 宠物医院服务

**MCP 服务依赖它，必须先跑起来。** 在仓库根目录：

```bash
./pethospital.exe
# 或（Windows PowerShell）
.\pethospital.exe
```

默认监听 `http://127.0.0.1:8080`。确认：

```bash
curl -s 'http://127.0.0.1:8080/api/v1/pets?pageSize=1'
```

返回 `{"code":200,...}` 即为正常。若后端未启动，`list_pets` 会返回
`BACKEND_UNAVAILABLE`。

### 2. 安装 MCP 服务

```bash
cd pet-hospital-mcp          # 项目目录（连字符）
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev]"
```

### 3. 启动 MCP 服务

```bash
python -m pet_hospital_mcp
```

启动后：

```
GET  http://127.0.0.1:8000/health   ->  {"status":"ok",...}
POST http://127.0.0.1:8000/mcp      ->  MCP 端点（无状态 Streamable HTTP）
```

也可以直接用环境变量覆盖后启动：

```bash
MCP_HOST=127.0.0.1 MCP_PORT=9000 PET_HOSPITAL_BASE_URL=http://127.0.0.1:8080 python -m pet_hospital_mcp
```

## 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PET_HOSPITAL_BASE_URL` | `http://127.0.0.1:8080` | Go REST API 地址 |
| `MCP_HOST` | `127.0.0.1` | MCP 监听地址（**默认只监听本机**） |
| `MCP_PORT` | `8000` | MCP 监听端口 |
| `MCP_PATH` | `/mcp` | MCP 端点路径 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `HTTP_TIMEOUT_SECONDS` | `10` | 调用后端的超时（秒） |
| `HTTP_MAX_RETRIES` | `2` | 后端重试次数（仅对 502/503/504 与超时/连接失败重试） |

配置非法（例如 `MCP_PORT=abc`、`PET_HOSPITAL_BASE_URL=ftp://x`）时进程会以退出码 2
启动失败，并打印具体原因。

## 唯一工具：`list_pets`

严格对应 `GET /api/v1/pets`，支持且仅支持后端的 14 个查询参数，未新增任何适配器私有参数：

```
q  name  ownerName  ownerPhone  species  doctor  disease  status
min  max  sortBy  order  page  pageSize
```

- `species`：`犬` `猫` `兔` `鸟` `仓鼠` `爬宠` `其他`
- `status`：`待就诊` `就诊中` `住院中` `已康复` `慢性病随访`
- `sortBy`：`id` `name` `ownerName` `species` `doctor` `disease` `status` `totalCost` `visitCount` `createdAt` `updatedAt`
- `order`：`asc` `desc`
- `page >= 1`，`1 <= pageSize <= 500`，`min`/`max` 非负且 `min <= max`
- 未知字段、NaN、Infinity、类型不正确的输入一律拒绝

> **为什么要做本地严格校验？** Go 服务对非法参数是宽容的：`order=bogus`、`page=0`、
> `sortBy=bogus` 都会静默忽略并返回 200。若不在 MCP 侧拦截，Agent 会把"参数写错了"
> 误读成"确实没有符合条件的数据"。本服务因此在调用后端之前就拒绝这些输入。

成功返回值对应 Go 响应中的 `data`：

```json
{
  "items": [ ... ],
  "total": 95, "page": 1, "pageSize": 3,
  "totalPages": 32, "totalCost": 901816.64
}
```

`items[].records` 与 `items[].charges` 兼容 Go 的 `null` 与数组两种表现，
`null` 会被规范化为 `[]`。

## 调用示例

### 方式 A：curl（无需任何客户端库）

MCP 2026-07-28 要求每个请求带上 `MCP-Protocol-Version`、`Mcp-Method` 头，
`tools/call` 还需要 `Mcp-Name`；请求体的 `params._meta` 必须携带协议版本与客户端能力。

先把请求体写成 UTF-8 文件（**Windows 上务必这样做**：Git Bash / cmd 的 `curl -d`
可能把中文转成 GBK，导致 `species` 等枚举校验失败）：

```bash
cat > /tmp/call.json <<'JSON'
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
  "name":"list_pets",
  "arguments":{"species":"犬","min":5000,"sortBy":"totalCost","order":"desc","page":1,"pageSize":3},
  "_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28",
           "io.modelcontextprotocol/clientCapabilities":{}}}}
JSON
```

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/call' \
  -H 'Mcp-Name: list_pets' \
  --data-binary @/tmp/call.json
```

实测返回（与直接调用 Go API 完全一致）：

```json
{"jsonrpc":"2.0","id":1,"result":{
  "content":[{"type":"text","text":"{\"items\": [...]}"}],
  "structuredContent":{"items":[...],"total":95,"page":1,"pageSize":3,
                       "totalPages":32,"totalCost":901816.64},
  "isError":false,"resultType":"complete"}}
```

发现工具与列出工具：

```bash
curl -s -X POST http://127.0.0.1:8000/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

### 方式 B：SDK 2.x 客户端（推荐）

```python
import asyncio
from mcp.client import Client

async def main():
    async with Client("http://127.0.0.1:8000/mcp") as client:
        listing = await client.list_tools()
        print([t.name for t in listing.tools])          # ['list_pets']

        result = await client.call_tool("list_pets", {"species": "犬", "pageSize": 3})
        print(result.is_error)                           # False
        print(result.structured_content["total"])        # 95

asyncio.run(main())
```

SDK 客户端会自动发送 `server/discover` 并处理请求信封，不需要手写上面的头。

### 方式 C：MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

在界面中选择 **Streamable HTTP**，URL 填 `http://127.0.0.1:8000/mcp`，然后 `Connect`。

> ⚠️ Inspector 的协议版本支持取决于其自身版本。若连接失败，请先用上面的
> **方式 A / B** 验证服务本身是正常的——本服务只讲 `2026-07-28`。

## 验证无状态连接

```bash
# 1. 健康检查（不触碰后端）
curl -s http://127.0.0.1:8000/health
# {"status":"ok","service":"pet-hospital-mcp","version":"0.1.0",
#  "protocolVersion":"2026-07-28","upstream":"http://127.0.0.1:8080"}

# 2. 冷启动直接调用，无需先 initialize
#    见上面「方式 A」的 tools/list —— 直接成功即说明无握手依赖

# 3. 确认响应里没有会话 id
curl -s -D - -o /dev/null -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}' \
  | grep -i 'mcp-session-id'
# 无输出 == 服务端不下发会话 id

# 4. GET / DELETE 已在本协议版本中移除 -> 405
curl -s -o /dev/null -w '%{http_code}\n' -X GET http://127.0.0.1:8000/mcp     # 405
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE http://127.0.0.1:8000/mcp  # 405
```

## 错误处理

任何失败都以统一结构返回，并通过 `isError: true` 标记为**工具调用失败**
（`resultType` 仍为 `"complete"`，这是 2026-07-28 的规定：工具执行错误写在结果对象里，
而不是 JSON-RPC 协议错误，这样模型才能看到失败并自我纠正）：

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "输入参数校验失败",
    "details": { "errors": [ { "field": "page", "reason": "...", "type": "greater_than_equal" } ] }
  }
}
```

| code | 含义 |
| --- | --- |
| `VALIDATION_ERROR` | 输入不合法（未知字段、越界、枚举错误、NaN/Infinity 等） |
| `BACKEND_TIMEOUT` | 调用 Go 服务超时 |
| `BACKEND_UNAVAILABLE` | 连不上 Go 服务（连接被拒、连接重置等） |
| `BACKEND_API_ERROR` | Go 服务返回 4xx/5xx，或信封 `code != 200` |
| `BACKEND_INVALID_RESPONSE` | 响应不是合法 JSON，或不符合数据模型 |
| `INTERNAL_ERROR` | 其他未预期异常 |

不会把 HTTPX / Pydantic / SDK 的异常堆栈原样透出：`details.errors` 只包含
字段名、稳定错误类型和一句人类可读的原因，不含 traceback。上游返回的 message
作为**数据**透传，不会被当成本服务自己的措辞。

## 日志

标准库 JSON 日志，每条工具调用一条记录，包含 `timestamp`、`tool_name`、`params`、
`status`、`duration_ms`：

```json
{"timestamp":"2026-09-17T03:07:32.689285+00:00","level":"INFO",
 "logger":"pet_hospital_mcp.tools.list_pets","message":"tool_call",
 "tool_name":"list_pets","params":{"ownerPhone":"***REDACTED***","species":"犬","page":1},
 "status":"ok","duration_ms":0.628}
```

- `ownerPhone`、`ownerAddr`、`chipNo` 及其 snake_case / 大小写变体（`owner_phone`、
  `OWNER_PHONE`、`chip_no` …）在日志中**递归脱敏**，包括嵌在 `items[].records` 里的出现。
- `httpx` / `httpcore` 的日志被压到 WARNING：它们在 INFO 级会打印完整请求 URL，
  而 URL 的查询串里就带着 `ownerPhone`——这是本服务的脱敏逻辑看不到的记录。
- `uvicorn` 的日志同样走本服务的 JSON formatter，整个进程只有一种日志格式。

## 单元测试

```bash
cd pet-hospital-mcp
pytest -q
```

预期结果：

```
154 passed
```

测试全部使用 `httpx.MockTransport`，**不会访问真实的 Go 服务**，也不打开任何真实网络
连接。覆盖：参数转发、输入校验、后端 4xx/5xx、超时与连接异常、非法 JSON 与模型不符、
工具注册与 JSON Schema、以及 SDK 2.x 的无状态连接流程（不含 `initialize`、
不下发 `Mcp-Session-Id`、`server/discover`、`/health`、工具可被发现与调用）。

## 目录结构

```
pet-hospital-mcp/                # 项目目录
├── pyproject.toml
├── README.md
├── UPGRADE_PROMPT.md          # 阶段二提示词
├── src/pet_hospital_mcp/
│   ├── __init__.py            # __version__
│   ├── __main__.py            # python -m pet_hospital_mcp
│   ├── config.py              # 环境变量 -> Settings
│   ├── server.py              # MCPServer 装配、/health、POST-only 守卫
│   ├── rest_client.py         # 唯一与 Go 服务对话的地方
│   ├── errors.py              # 统一错误信封
│   ├── logging_config.py      # JSON 日志 + 递归脱敏
│   ├── validation.py          # 严格入参校验中间件
│   └── tools/
│       ├── __init__.py        # 工具入参模型注册表
│       └── list_pets.py       # 本阶段唯一工具
└── tests/
```

## 设计说明（为什么这样做）

- **错误用 `CallToolResult(isError=True)` 返回，而不是抛异常。**
  2026-07-28 明确规定：源自工具的错误应写在结果对象里并置 `isError: true`，
  这样模型能看到并自我纠正。SDK 2.x 仍然采用该机制（并会自动补上必需的
  `resultType` 字段）。若改成抛异常，SDK 会把 `str(exc)` 塞进文本，既丢失结构化信封，
  又会带上 `Error executing tool ...` 前缀和 Pydantic 内部措辞。
- **工具函数返回 `CallToolResult` 而不是 Pydantic 输出模型。**
  若声明输出模型，SDK 会用它校验返回值——错误信封会被当成"输出模型不合法"而抛出，
  反而把 Pydantic 细节泄漏出去。返回 `CallToolResult` 让成功与失败两种信封都原样通过，
  同时仍通过 `structuredContent` 提供结构化结果。
- **严格校验放在 `ServerMiddleware` 里。**
  SDK 从函数签名生成的入参模型不禁止未知字段，未知字段会被静默丢弃；
  且校验失败时返回的是 SDK 默认的 `str(PydanticError)`。中间件在 SDK 之前用严格模型
  校验原始参数，因此未知字段被拒绝，且失败也走统一信封。
- **`GET`/`DELETE` 由 10 行的 `PostOnlyMcpEndpoint` 返回 405。**
  2026-07-28 移除了这两个方法，但 SDK 仍为旧协议版本保留它们：无请求体的 `GET`
  没有版本头可供路由，于是会打开一个旧式 SSE 流并一直等待——那是挂起而不是规范要求的
  405。这是本服务与旧传输唯一的交集：拒绝它们，其余请求原样交给 SDK。
