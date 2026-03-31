"""Cloudsell API client for the parser service.

Sends parsed and mapped data to the cloudsell_api:
- POST /v1/os/families        — ensure OS family exists
- POST /v1/os                 — ensure OS record exists
- POST /v1/pricing-plans/sync — upsert plans for a provider
"""

from uuid import UUID

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
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # OS families
    # ------------------------------------------------------------------

    async def ensure_os_family(self, family_name: str) -> None:
        """Create OS family if not exists (409 Conflict = already exists → OK)."""
        response = await self._post("/v1/os/families", {"name": family_name})
        if response.status_code not in (201, 409, 200):
            raise CloudsellApiError(response.status_code, response.text)
        log.debug("OS family ensured", family=family_name)

    # ------------------------------------------------------------------
    # Provider operating systems
    # ------------------------------------------------------------------

    async def ensure_os(
        self,
        provider_id: str,
        family: str,
        name: str,
        external_id: str,
        version: str | None = None,
    ) -> UUID | None:
        """
        Create a provider OS entry.
        Returns the UUID on 201, None if already exists (409).
        """
        payload = {
            "provider_id": provider_id,
            "family": family,
            "name": name,
            "external_id": external_id,
            "version": version,
        }
        response = await self._post("/v1/os", payload)
        if response.status_code == 201:
            os_id = UUID(response.json())
            log.debug("OS created", name=name, os_id=str(os_id))
            return os_id
        if response.status_code == 409:
            # Already exists — fetch its ID
            return await self._get_os_id_by_external(provider_id, external_id)
        raise CloudsellApiError(response.status_code, response.text)

    async def _get_os_id_by_external(self, provider_id: str, external_id: str) -> UUID | None:
        """Look up an OS by external_id (provider-specific key)."""
        response = await self._get(f"/v1/os/providers/{provider_id}")
        if response.status_code != 200:
            return None
        for item in response.json():
            if item.get("external_id") == external_id:
                return UUID(item["id"])
        return None

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
        Sends full provider state: what is active + what changed/is new.
        Returns {"created": int, "deactivated": int}.
        """
        payload = {
            "provider_id": provider_id,
            "active_external_ids": active_external_ids,
            "plans": plans,
        }
        response = await self._post("/v1/pricing-plans/sync", payload)
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
    # Providers
    # ------------------------------------------------------------------

    async def get_providers(self) -> list[dict]:
        """GET /v1/providers — returns provider list with IDs."""
        response = await self._get("/v1/providers")
        if response.status_code != 200:
            raise CloudsellApiError(response.status_code, response.text)
        return response.json()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _post(self, path: str, payload: dict) -> httpx.Response:
        assert self._client is not None
        return await self._client.post(f"{self._base}{path}", json=payload)

    async def _get(self, path: str) -> httpx.Response:
        assert self._client is not None
        return await self._client.get(f"{self._base}{path}")
