"""Cloudsell API client for the parser service.

Sends parsed and mapped data to the cloudsell_api:
- POST /v1/pricing-plans/sync — upsert plans for a provider (includes OS upsert)
"""

import httpx
import structlog

log = structlog.get_logger(__name__)


class CloudsellApiError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class CloudsellClient:
    def __init__(self, base_url: str, service_key: str, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {
            "X-Service-Key": service_key,
            "Content-Type": "application/json",
        }
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "CloudsellClient":
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=httpx.Timeout(connect=10.0, read=self._timeout, write=self._timeout, pool=10.0),
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Pricing plans sync
    # ------------------------------------------------------------------

    async def sync_plans(
        self,
        provider_id: str,
        active_external_ids: list[int],
        plans: list[dict],
    ) -> dict:
        """
        POST /v1/pricing-plans/sync
        Sends full provider state: active_external_ids + changed/new plans with OS definitions.
        Returns {"created": int, "deactivated": int}.
        """
        payload = {
            "provider_id": provider_id,
            "active_external_ids": active_external_ids,
            "plans": plans,
        }
        response = await self._post("/api/v1/pricing-plans/sync", payload)
        if response.status_code != 200:
            raise CloudsellApiError(response.status_code, response.text)
        result = response.json()
        log.info(
            "Plans synced",
            provider_id=provider_id,
            created=result.get("created"),
            deactivated=result.get("deactivated"),
        )
        return result

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _post(self, path: str, payload: dict) -> httpx.Response:
        assert self._client is not None
        return await self._client.post(f"{self._base}{path}", json=payload)
