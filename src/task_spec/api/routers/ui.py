"""임시 화면 (과제 명세 작업대) — GET /ui/ [제안: 2026-11-04 사용자 요청, 내부 시험용].

정적 파일 세 개(index.html·app.css·app.js)만 내준다. 화면은 기존 API(/postings·/runs)만 부르고 새 데이터 경로를 만들지 않는다.
경로 조작을 막으려고 디렉터리를 통째로 마운트하지 않고 정해 둔 파일만 돌려준다. UI_ENABLED=false 면 404.
인증은 아직 없다 — 운영에서는 nginx Basic 인증 뒤에 둔다 (docs/deploy.md) [TODO(auth)].
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

from task_spec.api.deps import SettingsDep
from task_spec.api.errors import UiDisabled

router = APIRouter(include_in_schema=False)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
FILES: dict[str, str] = {
    "": "text/html; charset=utf-8",
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}


@router.get("/")
async def root(settings: SettingsDep) -> RedirectResponse:
    if not settings.ui_enabled:
        raise UiDisabled()
    return RedirectResponse("/ui/")


@router.get("/ui")
async def ui_slash(settings: SettingsDep) -> RedirectResponse:
    if not settings.ui_enabled:
        raise UiDisabled()
    return RedirectResponse("/ui/")  # 상대 경로(app.css·app.js)가 /ui/ 아래를 가리키게


@router.get("/ui/{name}")
@router.get("/ui/", name="ui_index")
async def ui_file(settings: SettingsDep, name: str = "") -> FileResponse:
    if not settings.ui_enabled or name not in FILES:
        raise UiDisabled()
    return FileResponse(
        STATIC_DIR / (name or "index.html"),
        media_type=FILES[name],
        headers={"Cache-Control": "no-cache"},  # 임시 화면 — 고친 즉시 보이게
    )
