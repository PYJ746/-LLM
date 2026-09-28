"""The `list_pets` tool: filter/sort/paginate the Pet Hospital registry.

Mirrors `GET /api/v1/pets` on the Go service exactly — the 14 query parameters
below are the complete set the backend accepts, and no adapter-private
parameter is added.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..errors import (
    ErrorCode,
    ToolCallError,
    error_result,
    internal_error_result,
    success_result,
    validation_error_from,
)
from ..logging_config import get_logger, log_tool_call
from ..rest_client import PetHospitalClient
from . import register_input_model

logger = get_logger(__name__)

TOOL_NAME = "list_pets"

# --- Enum values, taken verbatim from the running backend's `/api/v1/meta` ---
Species = Literal["犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"]
Status = Literal["待就诊", "就诊中", "住院中", "已康复", "慢性病随访"]
SortBy = Literal[
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
]
Order = Literal["asc", "desc"]

TOOL_DESCRIPTION = """\
查询宠物医院档案列表（GET /api/v1/pets），支持关键词检索、多字段过滤、排序与分页。

用途：按条件筛选宠物档案并分页浏览，用于回答"有哪些…的宠物""某位主人的宠物"
"某医生接诊的病例""花费超过 N 元的宠物"等问题。

参数（全部可选，不传即不过滤）：
  q          全文关键词，跨字段检索（含病历全文），空格分词后按 AND 匹配
  name       宠物姓名（模糊匹配）
  ownerName  主人姓名（模糊匹配）
  ownerPhone 主人电话（模糊匹配）
  species    种类，枚举：犬/猫/兔/鸟/仓鼠/爬宠/其他
  doctor     主治医生姓名
  disease    疾病或诊断（模糊匹配）
  status     就诊状态，枚举：待就诊/就诊中/住院中/已康复/慢性病随访
  min, max   按总花费区间过滤（单位：元，非负，min 不得大于 max）
  sortBy     排序字段，枚举：id/name/ownerName/species/doctor/disease/status/totalCost/visitCount/createdAt/updatedAt
  order      排序方向，枚举：asc（升序）/desc（降序）
  page       页码，从 1 开始
  pageSize   每页条数，1..500

返回值：items（宠物档案数组，含 records 历史病历与 charges 消费明细，二者可能为 null）、
total（符合条件的总数）、page（当前页）、pageSize（每页条数）、totalPages（总页数）、
totalCost（符合条件的宠物花费合计）。

注意：min/max 过滤的是「在医院总花费」totalCost，不是单次收费。
"""


class ListPetsInput(BaseModel):
    """Strict input model for `list_pets`.

    Field names are exactly the backend's query-parameter names, so no private
    adapter naming is introduced. `strict` plus `extra="forbid"` and
    `allow_inf_nan=False` reject wrong types, unknown fields, NaN and Infinity.
    """

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    q: Annotated[str | None, Field(description="全文关键词，跨字段检索（含病历全文），空格分词 AND")] = None
    name: Annotated[str | None, Field(description="宠物姓名，模糊匹配")] = None
    ownerName: Annotated[str | None, Field(description="主人姓名，模糊匹配")] = None
    ownerPhone: Annotated[str | None, Field(description="主人电话，模糊匹配")] = None
    species: Annotated[Species | None, Field(description="种类")] = None
    doctor: Annotated[str | None, Field(description="主治医生姓名")] = None
    disease: Annotated[str | None, Field(description="疾病或诊断，模糊匹配")] = None
    status: Annotated[Status | None, Field(description="就诊状态")] = None
    min: Annotated[
        float | None, Field(ge=0, description="总花费下限（元），与 max 构成闭区间")
    ] = None
    max: Annotated[
        float | None, Field(ge=0, description="总花费上限（元），与 min 构成闭区间")
    ] = None
    sortBy: Annotated[SortBy | None, Field(description="排序字段")] = None
    order: Annotated[Order | None, Field(description="排序方向")] = None
    page: Annotated[int, Field(ge=1, description="页码，从 1 开始")] = 1
    pageSize: Annotated[int, Field(ge=1, le=500, description="每页条数，1..500")] = 20

    @model_validator(mode="after")
    def _check_cost_range(self) -> ListPetsInput:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min 不得大于 max")
        return self

    def to_query_params(self) -> dict[str, Any]:
        """Return only the parameters that were actually supplied.

        `page`/`pageSize` always have concrete defaults, so they are always sent.
        """
        return {
            key: value for key, value in self.model_dump().items() if value is not None
        }


# --- Output models (the Go `data` object) ---------------------------------


class PetRecord(BaseModel):
    """One historical medical record (`records[]`)."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    visitDate: str | None = None
    doctor: str | None = None
    diagnosis: str | None = None
    symptoms: str | None = None
    treatment: str | None = None
    prescription: list[str] | None = None
    weightKg: float | None = None
    temperature: float | None = None
    followUp: str | None = None
    charge: float | None = None
    createdAt: str | None = None


class PetCharge(BaseModel):
    """One billing line (`charges[]`)."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    item: str | None = None
    category: str | None = None
    amount: float | None = None
    doctor: str | None = None
    date: str | None = None


class Pet(BaseModel):
    """A pet档案.

    Only `id` and `name` are required (the Go model's other fields may legally be
    empty/omitted). `records` and `charges` are nullable because the backend can
    serialize them as `null` as well as `[]`; both are normalised to a list.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    species: str | None = None
    breed: str | None = None
    gender: str | None = None
    ageMonths: int | None = None
    color: str | None = None
    chipNo: str | None = None
    ownerName: str | None = None
    ownerPhone: str | None = None
    ownerAddr: str | None = None
    doctor: str | None = None
    disease: str | None = None
    status: str | None = None
    allergy: str | None = None
    note: str | None = None
    records: list[PetRecord] | None = None
    charges: list[PetCharge] | None = None
    totalCost: float | None = None
    visitCount: int | None = None
    createdAt: str | None = None
    updatedAt: str | None = None

    @model_validator(mode="after")
    def _normalise_null_collections(self) -> Pet:
        # A null collection is a legal wire representation of "nothing here".
        if self.records is None:
            self.records = []
        if self.charges is None:
            self.charges = []
        return self


class PetsPage(BaseModel):
    """The `data` object of a successful `GET /api/v1/pets` response."""

    model_config = ConfigDict(extra="allow")

    items: list[Pet]
    total: int
    page: int
    pageSize: int
    totalPages: int
    totalCost: float


def parse_page(data: dict[str, Any]) -> PetsPage:
    """Validate the backend's `data` object, mapping failures to our envelope."""
    try:
        return PetsPage.model_validate(data)
    except ValidationError as exc:
        raise ToolCallError(
            ErrorCode.BACKEND_INVALID_RESPONSE,
            "后端响应不符合预期数据模型",
            {
                "endpoint": "/api/v1/pets",
                "errors": [
                    {"loc": list(err.get("loc", ())), "type": err.get("type")}
                    for err in exc.errors()[:10]
                ],
            },
        ) from exc


async def run_list_pets(
    client: PetHospitalClient, arguments: dict[str, Any]
) -> CallToolResult:
    """Validate `arguments`, call the backend and build the tool result.

    Kept separate from the MCP-registered function so it can be unit-tested
    without standing up a server.
    """
    import time

    started = time.perf_counter()
    try:
        try:
            params = ListPetsInput.model_validate(arguments)
        except ValidationError as exc:
            raise validation_error_from(exc) from exc

        data = await client.fetch_pets(params.to_query_params())
        page = parse_page(data)
    except ToolCallError as err:
        log_tool_call(
            logger,
            tool_name=TOOL_NAME,
            params=arguments,
            status=err.code.value,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return error_result(err)
    except Exception:
        # Never surface an unexpected exception (or its traceback) to the client.
        logger.exception("unexpected failure in %s", TOOL_NAME)
        log_tool_call(
            logger,
            tool_name=TOOL_NAME,
            params=arguments,
            status=ErrorCode.INTERNAL_ERROR.value,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return internal_error_result()

    log_tool_call(
        logger,
        tool_name=TOOL_NAME,
        params=arguments,
        status="ok",
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return success_result(page)


def register(mcp: MCPServer, client: PetHospitalClient) -> None:
    """Register the `list_pets` tool on `mcp`, bound to `client`."""
    register_input_model(TOOL_NAME, ListPetsInput)

    @mcp.tool(name=TOOL_NAME, description=TOOL_DESCRIPTION)
    async def list_pets(
        q: Annotated[str | None, Field(description="全文关键词，跨字段检索（含病历全文），空格分词 AND")] = None,
        name: Annotated[str | None, Field(description="宠物姓名，模糊匹配")] = None,
        ownerName: Annotated[str | None, Field(description="主人姓名，模糊匹配")] = None,
        ownerPhone: Annotated[str | None, Field(description="主人电话，模糊匹配")] = None,
        species: Annotated[Species | None, Field(description="种类")] = None,
        doctor: Annotated[str | None, Field(description="主治医生姓名")] = None,
        disease: Annotated[str | None, Field(description="疾病或诊断，模糊匹配")] = None,
        status: Annotated[Status | None, Field(description="就诊状态")] = None,
        min: Annotated[float | None, Field(ge=0, description="总花费下限（元）")] = None,
        max: Annotated[float | None, Field(ge=0, description="总花费上限（元）")] = None,
        sortBy: Annotated[SortBy | None, Field(description="排序字段")] = None,
        order: Annotated[Order | None, Field(description="排序方向 asc/desc")] = None,
        page: Annotated[int, Field(ge=1, description="页码，从 1 开始")] = 1,
        pageSize: Annotated[int, Field(ge=1, le=500, description="每页条数，1..500")] = 20,
    ) -> CallToolResult:
        # The return annotation is `CallToolResult` on purpose: it disables the
        # SDK's output-model inference, so our success *and* error envelopes pass
        # through unmodified.
        return await run_list_pets(
            client,
            {
                "q": q,
                "name": name,
                "ownerName": ownerName,
                "ownerPhone": ownerPhone,
                "species": species,
                "doctor": doctor,
                "disease": disease,
                "status": status,
                "min": min,
                "max": max,
                "sortBy": sortBy,
                "order": order,
                "page": page,
                "pageSize": pageSize,
            },
        )
