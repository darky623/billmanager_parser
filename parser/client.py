"""HTTP client for BILLmanager provider API.

Handles auth, retries, and raw JSON retrieval.
"""

import structlog
from httpx import AsyncClient, HTTPStatusError, Response
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = structlog.get_logger(__name__)

_PRICELIST_FUNC = "v2.vds.order.pricelist"
_OS_PARAM_FUNC = "v2.vds.order.param"


class BillManagerClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._authinfo = f"{username}:{password}"
        self._timeout = timeout
        self._client: AsyncClient | None = None

    async def __aenter__(self) -> "BillManagerClient":
        self._client = AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()

    @retry(
        retry=retry_if_exception_type(HTTPStatusError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def fetch_pricelist(self, datacenter_id: int | None = None) -> bytes:
        """GET pricelist — all VDS plans for a given datacenter (or default if None)."""
        url = f"{self._base_url}/billmgr"
        params: dict = {
            "func": _PRICELIST_FUNC,
            "out": "xjson",
            "sfrom": "ajax",
            "authinfo": self._authinfo,
        }
        if datacenter_id is not None:
            params["datacenter"] = str(datacenter_id)
        log.debug("Fetching pricelist", url=url)
        response = await self._get(url, params)
        return response.content

    @retry(
        retry=retry_if_exception_type(HTTPStatusError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def fetch_os_list(self, plan_external_id: int, datacenter_id: int, period: int = 1) -> bytes:
        """GET OS list for a specific plan/datacenter combination."""
        url = f"{self._base_url}/billmgr"
        keyvalue = f"{plan_external_id}_{datacenter_id}"
        params = {
            f"period_{plan_external_id}": str(period),
            "datacenter": str(datacenter_id),
            "fperiod": "null",
            "hide_fperiod": "off",
            "hide_flabel": "on",
            "keyvalue": keyvalue,
            "func": _OS_PARAM_FUNC,
            "sfrom": "ajax",
            "out": "xjson",
            "authinfo": self._authinfo,
        }
        log.debug("Fetching OS list", plan_id=plan_external_id, datacenter_id=datacenter_id)
        response = await self._get(url, params)
        return response.content

    async def _get(self, url: str, params: dict) -> Response:
        assert self._client is not None, "Use as async context manager"
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return response
