"""지원자 README 의 '시작 코드' 절 — 시작 코드에서 이미 있는 이름을 뽑고, README 와 코드가 반드시 맞는다. 가상 데이터만."""

from __future__ import annotations

import pytest

from task_spec.schemas import GeneratedFile
from task_spec.starter.code_map import SECTION, code_map, code_map_markdown, code_map_problems
from task_spec.starter.render import RenderError, with_code_map

APP = """import NoteFormPage from './pages/NoteFormPage';
/** 앱 화면의 뿌리 */
export default function App() {
  return (<Routes><Route path="/notes/new" element={<NoteFormPage />} /></Routes>);
}
"""
API = """import type { PostApiNotesOk, PostApiNotesBody } from './contract-types';
/** 가상 메모를 서버에 저장한다 */
export async function postNote(
  body: PostApiNotesBody,
): Promise<PostApiNotesOk> {
  return (await fetch('/api/notes', { method: 'POST' })).json();
}
"""
PAGE = """/** 가상 메모 작성 화면 TODO: 대기열에 쌓고 online 이벤트에 다시 보낸다 */
export default function NoteFormPage() { return null; }
"""
TYPES = "export type PostApiNotesOk = { id: string };\nexport type PostApiNotesBody = { text: string };\n"
JAVA = """package com.example.service.note;
public class NoteService {
    public NoteService(NoteRepository repo) {}
    public NoteDto save(@Valid NoteRequest request) { return null; }
    public Long getId() { return 1L; }
    public void setId(Long id) {}
    public List<NoteDto> getNotes(Long projectId) { return null; }
    private void hidden() {}
}
"""
FILES = [
    GeneratedFile(path="src/App.tsx", content=APP),
    GeneratedFile(path="src/api/notes.ts", content=API),
    GeneratedFile(path="src/pages/NoteFormPage.tsx", content=PAGE),
    GeneratedFile(path="src/api/contract-types.ts", content=TYPES),
    GeneratedFile(path="src/pages/NoteFormPage.test.tsx", content="export function helper() {}"),
    GeneratedFile(path="src/main/java/com/example/service/note/NoteService.java", content=JAVA),
]


def test_map_lists_existing_names_from_the_code() -> None:
    md = code_map_markdown(code_map(FILES))
    assert md.startswith(SECTION)
    assert "- `src/App.tsx` — 화면 경로: `/notes/new` → `NoteFormPage`" in md
    # 시그니처만 — 생성 코드의 주석(JSDoc)은 싣지 않는다 (#184)
    assert "  - `postNote(body: PostApiNotesBody): Promise<PostApiNotesOk>`\n" in md
    assert "가상 메모를 서버에 저장한다" not in md
    assert "`PostApiNotesOk`, `PostApiNotesBody`" in md  # 계약 타입
    assert (
        "  - `NoteDto save(NoteRequest request)`" in md and "hidden" not in md
    )  # Java public 만, 생성자·주석 빼고
    assert "helper" not in md  # 테스트 파일은 뺀다
    # getter(인자 없는 get)·setter(인자 하나인 void set)는 세기만, 인자 있는 get 은 요청 처리 메서드라 남긴다
    assert (
        "  - `NoteService` — getter·setter 생략" in md and "getId" not in md and "setId" not in md
    )
    assert "  - `List<NoteDto> getNotes(Long projectId)`" in md


def test_starter_comments_are_not_copied() -> None:
    """시작 코드 주석의 할 일 목록이 숨은 요구의 해법을 적기도 한다 — README 코드 지도에는 이름·시그니처만 (#184: TODO·'할 일:' 뒤만
    자르던 것은 FIXME·'다음:'·첫 문장 안의 해법을 그대로 보냈다)."""
    md = code_map_markdown(code_map(FILES))
    assert "  - `NoteFormPage()`\n" in md + "\n"
    assert "대기열" not in md and "online" not in md and "가상 메모 작성 화면" not in md
    for doc in (
        "FIXME 대기열에 쌓는다",
        "다음: online 이벤트에 다시 보낸다",
        "대기열에 쌓아 online 때 보내는 화면.",
    ):
        page = GeneratedFile(
            path="src/pages/NoteFormPage.tsx",
            content=f"/** {doc} */\nexport function NoteFormPage() {{ return null; }}\n",
        )
        got = code_map_markdown(code_map([page]))
        assert "대기열" not in got and "online" not in got, (doc, got)


def test_readme_and_code_must_match() -> None:
    readme = "# 가상\n\n## 코드 구조\n\n- 스택\n\n## 실행\n\nnpm test\n"
    merged = with_code_map(readme, FILES)
    assert merged.index("## 코드 구조") < merged.index(SECTION) < merged.index("## 실행")
    assert "\n\n## 실행" in merged  # 다음 절과 빈 줄
    assert code_map_problems(merged, FILES) == []
    # 코드에서 이름·파일·화면 경로가 바뀌면 대조가 잡는다
    renamed = [
        f
        if f.path != "src/api/notes.ts"
        else GeneratedFile(path=f.path, content=API.replace("postNote", "saveNote"))
        for f in FILES
    ]
    assert any("postNote" in p for p in code_map_problems(merged, renamed))
    gone = [f for f in FILES if f.path != "src/pages/NoteFormPage.tsx"]
    assert any("src/pages/NoteFormPage.tsx" in p for p in code_map_problems(merged, gone))
    moved = [
        f
        if f.path != "src/App.tsx"
        else GeneratedFile(path=f.path, content=APP.replace("/notes/new", "/notes/add"))
        for f in FILES
    ]
    assert any("/notes/new" in p for p in code_map_problems(merged, moved))


def test_render_fails_rather_than_publish_a_wrong_map(monkeypatch: pytest.MonkeyPatch) -> None:
    import task_spec.starter.render as render

    monkeypatch.setattr(render, "code_map_problems", lambda md, files: ["가상 불일치"])
    with pytest.raises(RenderError, match="맞지 않음"):
        with_code_map("# 가상\n", FILES)
