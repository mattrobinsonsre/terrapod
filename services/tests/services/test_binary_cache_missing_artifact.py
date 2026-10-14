"""A deterministic upstream 404 must not look like a transport failure (#2066).

`_fetch_and_store_binary` calls `raise_for_status()`, so before this every
upstream status reached the routers' bare `except Exception` and became a **502**
— and the runner's download helper retries a 502, because a gateway error is
exactly the shape of a transient fault. So an artifact that does not exist and
never will was fetched three times and then reported as a gateway error that
named neither the tool nor the platform.

That is not hypothetical: Checkov stopped publishing `checkov_linux_arm64.zip`
at 3.3.21 while the platform table went on claiming it, and from v1.9.0 every
security scan on an arm64 agent pool recorded `errored` — logged as
"transient failure — will retry".

Both directions are asserted, because the careless fix is to classify
everything as final: a 404 must become `UpstreamArtifactMissing` (a ValueError,
so the routers answer 4xx and the runner stops), and a 5xx must keep
propagating as the retryable thing it is.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from terrapod.services.binary_cache_service import get_or_cache_binary
from terrapod.services.cache_errors import UpstreamArtifactMissing


def _status_error(code: int, url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=request, response=httpx.Response(code, request=request)
    )


def _cold_cache_db() -> AsyncMock:
    """A db whose lookup misses, so the fetch path is the one under test."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.first.return_value = None
    db.execute.return_value = result
    return db


class TestAnUpstream404IsFinal:
    @pytest.mark.asyncio
    @patch("terrapod.services.binary_cache_service._fetch_and_store_binary")
    @patch("terrapod.services.binary_cache_service._get_cached", new_callable=AsyncMock)
    async def test_a_404_raises_upstream_artifact_missing(
        self, mock_cached: AsyncMock, mock_fetch: MagicMock
    ) -> None:
        mock_cached.return_value = None
        url = (
            "https://github.com/mattrobinsonsre/terrapod/releases/download/"
            "3.3.26/checkov_linux_arm64.zip"
        )
        mock_fetch.side_effect = _status_error(404, url)

        with pytest.raises(UpstreamArtifactMissing) as excinfo:
            await get_or_cache_binary(
                _cold_cache_db(), AsyncMock(), "checkov", "3.3.26", "linux", "arm64"
            )

        # The operator's next move is to pin a different version or publish the
        # asset, so the message has to carry all three coordinates. A bare
        # "not found" sends them to the logs instead.
        message = str(excinfo.value)
        assert "checkov" in message
        assert "3.3.26" in message
        assert "linux/arm64" in message

    @pytest.mark.asyncio
    @patch("terrapod.services.binary_cache_service._fetch_and_store_binary")
    @patch("terrapod.services.binary_cache_service._get_cached", new_callable=AsyncMock)
    async def test_it_is_a_value_error_so_the_routers_answer_4xx(
        self, mock_cached: AsyncMock, mock_fetch: MagicMock
    ) -> None:
        """The routers map ValueError to 400 and everything else to 502.

        This is the whole mechanism — there is no new router branch — so if
        `UpstreamArtifactMissing` ever stops being a ValueError the fix silently
        reverts to a retried 502.
        """
        mock_cached.return_value = None
        mock_fetch.side_effect = _status_error(404, "https://example.invalid/x.zip")

        with pytest.raises(ValueError):
            await get_or_cache_binary(
                _cold_cache_db(), AsyncMock(), "checkov", "3.3.26", "linux", "arm64"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    @patch("terrapod.services.binary_cache_service._fetch_and_store_binary")
    @patch("terrapod.services.binary_cache_service._get_cached", new_callable=AsyncMock)
    async def test_a_5xx_still_propagates_as_retryable(
        self, mock_cached: AsyncMock, mock_fetch: MagicMock, code: int
    ) -> None:
        """The other direction, which is the one a careless fix breaks.

        Classifying every upstream status as final would turn a mirror's brief
        outage into a failed run that no retry could rescue.
        """
        mock_cached.return_value = None
        mock_fetch.side_effect = _status_error(code, "https://example.invalid/x.zip")

        with pytest.raises(httpx.HTTPStatusError):
            await get_or_cache_binary(
                _cold_cache_db(), AsyncMock(), "checkov", "3.3.26", "linux", "arm64"
            )
