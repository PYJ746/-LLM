"""HTTP client for the Go Pet Hospital REST API.

The go service is the only business backend; this module is the only place that
speaks to it. It translates every transport and protocol failure into a
`ToolCallError` so that no `httpx` detail ever reaches an MCP client.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

import httpx

from .errors import ErrorCode, ToolCallError
from .logging_config import get_logger

logger = get_logger(__name__)

# Transient upstream conditions worth a bounded retry. 5xx other than these
# (e.g. 501) are stable answers, so retrying them only wastes the deadline.
RETRYABLE_STATUS = frozenset({502, 503, 504})

# The Go envelope always carries a numeric `code` mirroring the HTTP status.
EXPECTED_SUCCESS_CODE = 200


class PetHospitalClient:
    """Async client for `GET /api/v1/pets` (and future endpoints)."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(0, max_retries)
        self._backoff_seconds = backoff_seconds
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PetHospitalClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    @property
    def pets_endpoint(self) -> str:
        return f"{self._base_url}/api/v1/pets"

    async def fetch_pets(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """Call `GET /api/v1/pets` and return the raw `data` object.

        Raises:
            ToolCallError: on timeout, connection failure, a non-2xx status, a
                non-`200` envelope code, or a body that is not the expected
                JSON object shape.
        """
        response = await self._request_pets(query)

        try:
            body = response.json()
        except ValueError as exc:
            raise ToolCallError(
                ErrorCode.BACKEND_INVALID_RESPONSE,
                "后端返回的不是合法 JSON",
                {"status": response.status_code, "endpoint": "/api/v1/pets"},
            ) from exc

        if not isinstance(body, dict):
            raise ToolCallError(
                ErrorCode.BACKEND_INVALID_RESPONSE,
                "后端响应不是 JSON 对象",
                {"status": response.status_code, "endpoint": "/api/v1/pets"},
            )

        envelope_code = body.get("code")
        if envelope_code != EXPECTED_SUCCESS_CODE:
            raise ToolCallError(
                ErrorCode.BACKEND_API_ERROR,
                str(body.get("message") or "后端返回了错误"),
                {
                    "status": response.status_code,
                    "upstream_code": envelope_code,
                    "upstream_message": body.get("message"),
                    "endpoint": "/api/v1/pets",
                },
            )

        data = body.get("data")
        if not isinstance(data, dict):
            raise ToolCallError(
                ErrorCode.BACKEND_INVALID_RESPONSE,
                "后端响应缺少 data 对象",
                {"status": response.status_code, "endpoint": "/api/v1/pets"},
            )
        return data

    async def _request_pets(self, query: Mapping[str, Any]) -> httpx.Response:
        """Issue the request with a bounded number of retries."""
        params = {key: value for key, value in query.items() if value is not None}
        last_error: ToolCallError | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get("/api/v1/pets", params=params)
            except httpx.TimeoutException as exc:
                last_error = ToolCallError(
                    ErrorCode.BACKEND_TIMEOUT,
                    "调用后端超时",
                    {"endpoint": "/api/v1/pets", "attempts": attempt + 1},
                )
                cause: Exception = exc
            except httpx.TransportError as exc:
                last_error = ToolCallError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    "无法连接后端服务",
                    {"endpoint": "/api/v1/pets", "attempts": attempt + 1},
                )
                cause = exc
            else:
                if response.status_code in RETRYABLE_STATUS:
                    last_error = ToolCallError(
                        ErrorCode.BACKEND_API_ERROR,
                        f"后端暂时不可用（HTTP {response.status_code}）",
                        {
                            "status": response.status_code,
                            "endpoint": "/api/v1/pets",
                            "attempts": attempt + 1,
                        },
                    )
                    cause = None
                elif response.status_code >= 400:
                    # A stable 4xx/5xx is the backend's final answer: do not retry.
                    raise self._http_status_error(response)
                else:
                    return response

            if attempt < self._max_retries:
                logger.debug(
                    "retrying pets request",
                    extra={"event": {"attempt": attempt + 1, "code": last_error.code.value}},
                    exc_info=cause,
                )
                await asyncio.sleep(self._backoff_seconds * (2**attempt))

        assert last_error is not None  # loop always sets it before exiting
        raise last_error

    def _http_status_error(self, response: httpx.Response) -> ToolCallError:
        """Map a stable 4xx/5xx from the Go service onto an API error."""
        message = f"后端返回 HTTP {response.status_code}"
        upstream_code: Any = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            if body.get("message"):
                message = str(body["message"])
            upstream_code = body.get("code")

        details: dict[str, Any] = {
            "status": response.status_code,
            "endpoint": "/api/v1/pets",
        }
        if upstream_code is not None:
            details["upstream_code"] = upstream_code
        return ToolCallError(ErrorCode.BACKEND_API_ERROR, message, details)
