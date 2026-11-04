"""임시 화면 /ui/ — 정해 둔 정적 파일만, UI_ENABLED 로 끄고 켠다. 가상 데이터만."""

from __future__ import annotations

from tests.api.conftest import AppFactory


async def test_ui_serves_only_listed_files(make_client: AppFactory) -> None:
    client = await make_client()
    root = await client.get("/")
    assert root.status_code == 307 and root.headers["location"] == "/ui/"
    assert (await client.get("/ui")).headers["location"] == "/ui/"
    page = await client.get("/ui/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "과제 명세 작업대" in page.text and 'src="app.js"' in page.text
    js = await client.get("/ui/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "/postings/suggest" in js.text and "/approve" in js.text
    assert (await client.get("/ui/app.css")).status_code == 200
    for bad in ("/ui/../config.py", "/ui/%2e%2e%2fconfig.py", "/ui/routers.py", "/ui/static"):
        r = await client.get(bad)
        assert r.status_code == 404, bad


async def test_ui_disabled_returns_404(make_client: AppFactory) -> None:
    client = await make_client(ui_enabled=False)
    for path in ("/", "/ui", "/ui/", "/ui/app.js"):
        r = await client.get(path)
        assert r.status_code == 404, path
        assert r.json()["error"]["code"] == "not_found"
    assert (await client.get("/healthz")).status_code in (200, 503)  # API 는 그대로
