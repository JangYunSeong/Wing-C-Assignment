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


def test_ui_shows_task_kind_and_sends_kind_switch() -> None:
    """승인 화면: 과제 유형 표시·기능형 두 단계 표시·수정 요청의 유형 바꾸기 (정적 파일 내용 확인)."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "src/task_spec/api/static/app.js").read_text(
        "utf-8"
    )
    assert 'const KIND_KO = { incident: "이슈 해결형", feature: "기능 개발형" };' in js
    assert 'if (t.task_kind === "feature") return renderFeatureTask(t);' in js
    assert "2단계 디버깅 — 1단계 제출 뒤 공개" in js
    assert "if (kind) body.task_kind = kind;" in js
    # 서버와 같은 규칙: 유형을 바꾸면 경험 검증만 다시 만들 수는 없다
    assert 'kind && body.revise_target === "verification"' in js


def test_ui_shows_two_tier_clarification_answers() -> None:
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "src/task_spec/api/static/app.js").read_text(
        "utf-8"
    )
    assert "일반 질문 → ${esc(x.answer_hint)}" in js
    assert '짚은 질문(${esc((x.match_terms || []).join(", "))}) → ${esc(x.answer)}' in js
