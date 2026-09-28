# 阶段二提示词

把下面整段内容作为下一次任务的提示词使用。它是**增量**的：阶段一已完成的部分
不需要重做，也不应被重新设计。

---

## 任务：为 pet-hospital-mcp 增加阶段二工具

你正在 `pet-hospital-mcp/` 中维护一个已经可用的 MCP 服务。阶段一已交付并验证：

- Python 3.11+，官方 Python SDK `mcp==2.0.0`（精确固定），MCP 协议版本 `2026-07-28`
- 服务端类 `mcp.server.mcpserver.MCPServer`，无状态 Streamable HTTP（`stateless_http=True`）
- 不使用 `mcp.server.fastmcp.FastMCP`；不实现 `initialize`、`Mcp-Session-Id`、
  会话存储、会话过期、`max_sessions`、SSE 恢复
- 唯一工具 `list_pets`（`GET /api/v1/pets`），无状态、纯 HTTP 调用 Go 后端
- `pytest -q` 全绿（阶段一时为 148 passed）

**先读代码再动手。** 至少读完：`README.md`、`src/pet_hospital_mcp/server.py`、
`src/pet_hospital_mcp/rest_client.py`、`src/pet_hospital_mcp/errors.py`、
`src/pet_hospital_mcp/validation.py`、`src/pet_hospital_mcp/tools/list_pets.py`、
以及 `tests/` 下的全部测试。

### 目标

新增以下工具，全部对应现有 Go REST API 的真实端点。**不得修改 Go 服务。**
端点以仓库根目录 `README.md` 的接口表为准，实现前请先实际 `curl` 确认响应结构：

1. `get_pet` —— `GET /api/v1/pets/{id}`：按 ID 查询单只宠物档案
2. `get_pet_summary` —— `GET /api/v1/pets/{id}/summary`：单只宠物费用与就诊汇总
3. `list_records` —— `GET /api/v1/pets/{id}/records`：查询历史病历
4. `list_charges` —— `GET /api/v1/pets/{id}/charges`：查询消费明细
5. `search_pets` —— `GET /api/v1/pets/search?q=`：全文检索（跨字段，空格分词 AND）

若某个端点的真实响应与预期不符，以实现时实际观测到的响应为准，并在 README 中说明。

### 硬性约束（与阶段一一致，不得放宽）

- 工具名一律 `snake_case`；不新增适配器私有业务参数，参数名与后端查询参数逐一对应
- 每个工具的输入、成功输出、错误输出都用 Pydantic 模型定义
- 输入必须严格校验：未知字段、NaN、Infinity、类型不正确一律拒绝；
  数值范围与后端真实允许值一致（先 `curl` 验证后端行为再定约束，不要照抄猜测）
- 错误沿用阶段一的统一信封
  `{"error": {"code": ..., "message": ..., "details": {}}}`，通过
  `CallToolResult(isError=True)` 返回——**继续沿用阶段一的机制**，
  它已按 2026-07-28 的规定实现，不要改成抛异常，也不要改成 JSON-RPC 协议错误
- 后端调用必须有超时与有限重试；不得把 HTTPX / Pydantic / SDK 的堆栈透给客户端
- 不得引入任何会话状态：工具之间不共享状态，每个请求自带全部信息
- 日志保持 JSON 格式与 `timestamp`/`tool_name`/`params`/`status`/`duration_ms` 字段；
  新增的敏感字段（如病历内容、联系方式）必须在 `logging_config.py` 的脱敏集合中登记

### 期望的改动方式

新增工具**只应新增 `src/pet_hospital_mcp/tools/` 下的模块**，并复用既有的
`rest_client`、`errors`、`logging_config` 与 `validation` 约定：

```
src/pet_hospital_mcp/tools/
├── __init__.py        # 已存在：输入模型注册表，无需改动
├── list_pets.py       # 已存在：作为新模块的模板
├── get_pet.py         # 新增
├── ...
```

- 若 `rest_client.PetHospitalClient` 只支持 `GET /api/v1/pets`，请在其中**扩展**
  通用的请求方法（继续集中管理超时、重试、错误映射），不要把 HTTP 细节散落到各工具模块
- 若出现两个以上工具共用的输出模型（例如宠物档案对象），提取到共享模块，
  不要让各工具各自定义一份
- 同步更新 `README.md`（工具清单、参数、调用示例、测试数量）与
  `src/pet_hospital_mcp/server.py` 中 `SERVER_INSTRUCTIONS` 的工具说明
- 为每个新工具补齐测试，沿用 `tests/conftest.py` 的既有夹具
  （`RecordingBackend`、`open_mcp`、`backend_ok` 等）；测试同样**禁止访问真实 Go 服务**

### 完成标准

- `cd pet-hospital-mcp && pytest -q` 全绿，且新增测试覆盖：
  参数正确转发、输入校验失败、后端 4xx/5xx、超时与连接异常、非法 JSON 与模型不符
- 至少针对**一个**新工具，用真实 Go 服务做一次端到端验证，并把实测结果写进 README
- 交付时明确说明：修改的文件列表、测试命令与真实输出、新增工具的发现与调用示例

---

## 备注

阶段一中有两处**有意为之**的实现，容易被误当成 bug 改掉，请保留：

1. `tools/list_pets.py` 中工具函数标注 `-> CallToolResult`，并显式构造结果对象。
   若改成声明 Pydantic 输出模型，SDK 会用该模型校验返回值，错误信封会被判为
   "输出不合法"而抛出，反而泄漏 Pydantic 细节。
2. `server.py` 中的 `PostOnlyMcpEndpoint` 让 `GET`/`DELETE` 返回 405。
   2026-07-28 已移除这两个方法，但 SDK 仍为旧协议保留；无请求体的 `GET`
   会打开旧式 SSE 流并挂起，而不是返回规范要求的 405。
