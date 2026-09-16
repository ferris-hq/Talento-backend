import httpx
import pytest

from app.config import Settings
from app.services import cdn


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        cdn.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


async def test_purges_deleted_files(calls: list[httpx.Request]) -> None:
    settings = Settings(cloudflare_zone_id="zone", cloudflare_api_token="tok")
    await cdn.purge(["https://m.test/a.mp4", None], settings)
    (req,) = calls
    assert req.url.path == "/client/v4/zones/zone/purge_cache"
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.read() == b'{"files":["https://m.test/a.mp4"]}'


async def test_skips_when_not_configured(calls: list[httpx.Request]) -> None:
    await cdn.purge(["https://m.test/a.mp4"], Settings())
    assert calls == []
