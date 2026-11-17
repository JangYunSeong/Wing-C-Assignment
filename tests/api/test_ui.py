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


async def test_ui_builds_page_uses_build_apis_only(make_client: AppFactory) -> None:
    client = await make_client()
    page = await client.get("/ui/builds.html")
    assert page.status_code == 200 and 'src="builds.js"' in page.text
    assert 'href="builds.html"' in (await client.get("/ui/")).text  # 작업대에서 오갈 수 있게
    js = (await client.get("/ui/builds.js")).text
    # 진행 막대는 두 화면이 같이 쓰는 stages.js — 빌드의 stage_log(단계·시각·비용)만 읽는다
    stages = await client.get("/ui/stages.js")
    assert stages.status_code == 200 and "javascript" in stages.headers["content-type"]
    assert "stage_log" in stages.text and 'src="stages.js"' in page.text
    assert 'src="stages.js"' in (await client.get("/ui/")).text
    for hidden in ("hidden_plan", "contract.", "content"):
        assert hidden not in stages.text, hidden
    assert "/builds?limit=" in js and "/artifacts" in js and "/delivery-targets" in js
    # 숨은 계획 원문·계약 원문·파일 본문은 API 가 내주지 않는다 — 화면도 그 칸을 기대하지 않는다.
    # 케이스 설명·의도한 실패 사유는 승인자용 /cases 로만 받는다 [확정: 2026-11-11 사용자 — 문제를 쓸지 정하는 사람이 본다]
    assert "/cases" in js and "intended_reason" in js
    # README 본문(/readme)·진행자 자료 본문(/facilitator)은 지원자가 받는 글이다 — 그 두 API 의 content 만 허용.
    # 문제 분석 판의 원래 판(README.full.md)도 /readme 응답의 full 로만 — 승인 화면에서 지원자 README 와 함께 본다 (#151)
    assert (
        'data-t="readme"' in js
        and 'data-t="full"' in js
        and 'data-t="facilitator"' in js
        and "/builds/${encodeURIComponent(id)}/${res}" in js
    )
    rest = js.replace("v.full.content", "").replace("v.content", "").replace("a.content", "")
    for hidden in ("hidden_plan", "contract", "content"):
        assert hidden not in rest, hidden


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
    assert '짚은 질문(${esc(x.match_terms.join(", "))}) → ${esc(x.answer)}' in js
    # 매칭 낱말이 다 지워진 단서 답 (prune_match_terms) — 늘 모호한 답이 나간다는 것을 승인자에게 보인다
    assert "구체 답 조건 없음 — 늘 모호한 답" in js


def test_ui_shows_analysis_stage() -> None:
    """문제 분석 판 (#151): 명세 승인 화면은 지원자가 받는 글·요구가 드러나는 길·사실·흘림 검토 표시를,
    진행자 보기는 지원자가 받은 글·사실·열림 조건을 보인다 — 흘림은 사람이 화면을 보고 거른다 (analysis-stage.md §5.1)."""
    from pathlib import Path

    from task_spec.spec import analysis_request as ar

    static = Path(__file__).resolve().parents[2] / "src/task_spec/api/static"
    js = (static / "app.js").read_text("utf-8")
    for field in (
        "t.background",
        "t.request",
        "t.request_mentions",
        "t.facts",
        "x.req_ids",
        "x.ask_when",
    ):
        assert field in js, field
    # 흘림 검토 상자는 요청 글 단계의 검토 표시를 머리말로 고른다 — 서버 문구의 머리말이 바뀌면 여기서 깨진다
    assert 'const ANALYSIS_FLAG = "요청 글·사실";' in js
    for flag in (
        ar.FLAG_FAILED,
        ar.FLAG_INVALID,
        ar.FLAG_REPAIRED,
        ar.FLAG_OVERLAP,
        ar.FLAG_LEAK,
        ar.FLAG_ARTIFACT_STATES,
        ar.FLAG_NO_MENTION,
        ar.FLAG_ALL_MENTIONED,
    ):
        assert flag.startswith("요청 글·사실"), flag
    builds = (static / "builds.js").read_text("utf-8")
    for field in ("s1.background", "s1.request", "s1.facts", "c.ask_when", "x.ask_when"):
        assert field in builds, field
