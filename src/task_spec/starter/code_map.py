"""지원자 README 의 '시작 코드' 절 — 시작 코드 파일에서 이미 있는 이름(파일·화면 경로·내보내는 함수·클래스)을 코드가 뽑는다.

2026-11-11 사용자: 지원자가 보는 글에 구체적인 코드·함수 이름이 있어야 한다 — 다만 코드와 반드시 맞아야 한다.
- 이미 있는 코드만 적는다 (지원자 이미지에 들어가는 바로 그 시작 코드에서 뽑는다 — LLM 이 쓰지 않는다).
- 새 기능의 함수·구조 이름은 정하지 않는다 — 숨은 테스트는 계약(API·화면 test id)으로만 판정하고, 이름을 정하면 설계(숨은 요구의
  해법)를 정하는 셈이다.
- code_map_problems: README 에 적힌 경로·이름·화면 경로가 시작 코드에 실제로 있는지 다시 대조한다 (렌더 버그·오래된 README 를 잡는다).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from task_spec.schemas.build import GeneratedFile, TaskContract
from task_spec.starter.ts_helpers import APP_TYPES

SECTION = "## 시작 코드"


@dataclass(frozen=True)
class Item:
    name: str
    text: str  # README 에 쓰는 꼴 (시그니처 등)
    doc: str = ""


@dataclass
class FileMap:
    path: str
    note: str = ""
    items: list[Item] = field(default_factory=list)


# ── TypeScript ─────────────────────────────────────────────────────

_TS_FUNC = re.compile(
    r"(?:/\*\*(?P<doc>(?:(?!\*/).)*?)\*/\s*)?export\s+(?P<default>default\s+)?(?P<async>async\s+)?function\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*(?P<gen><[^>(]*>)?\s*\((?P<params>[^)]*)\)\s*(?::\s*(?P<ret>[^{]+?))?\s*\{",
    re.S,
)
_TS_CONST = re.compile(
    r"(?:/\*\*(?P<doc>(?:(?!\*/).)*?)\*/\s*)?export\s+const\s+(?P<name>[A-Za-z_$][\w$]*)", re.S
)
_TS_TYPE = re.compile(r"export\s+(?:type|interface)\s+(?P<name>[A-Za-z_$][\w$]*)")
_ROUTE = re.compile(
    r"<Route\s+[^>]*?path=[\"'](?P<path>[^\"']+)[\"'][^>]*?element=\{\s*<(?P<el>[A-Za-z_$][\w$]*)"
)
_SKIP_TS = re.compile(
    r"(\.test\.|\.spec\.|^src/test/|^src/main\.tsx$|\.css$|^src/mocks/(server|browser)\.ts$)"
)


def _one_line(s: str) -> str:
    """주석 한 줄 — TODO 뒤는 뺀다 (시작 코드 주석의 할 일 목록이 숨은 요구의 해법을 적기도 한다: '… 대기열, online 이벤트 동기화')."""
    text = " ".join(s.replace("*", " ").split())
    return re.split(r"\bTODO\b|할 일:", text, maxsplit=1)[0].strip(" -—:")


def ts_items(content: str) -> list[Item]:
    out: list[Item] = []
    for m in _TS_FUNC.finditer(content):
        params = " ".join(m.group("params").split()).rstrip(",")
        ret = f": {' '.join(m.group('ret').split())}" if m.group("ret") else ""
        text = f"{m.group('name')}({params}){ret}"
        out.append(Item(m.group("name"), text, _one_line(m.group("doc") or "")))
    for m in _TS_CONST.finditer(content):
        out.append(Item(m.group("name"), m.group("name"), _one_line(m.group("doc") or "")))
    return out


def ts_routes(app: str) -> list[tuple[str, str]]:
    """App.tsx 의 화면 경로 → 컴포넌트 이름."""
    return [(m.group("path"), m.group("el")) for m in _ROUTE.finditer(app)]


# ── Java ───────────────────────────────────────────────────────────

_JAVA_CLASS = re.compile(
    r"public\s+(?:final\s+|abstract\s+)*(?:class|interface|record|enum)\s+(?P<name>\w+)"
)
_JAVA_METHOD = re.compile(
    r"public\s+(?!class\b|interface\b|record\b|enum\b)(?:static\s+|final\s+|synchronized\s+)*"
    r"(?P<ret>[\w<>\[\],.? ]+?)\s+(?P<name>\w+)\s*\((?P<params>[^)]*)\)"
)


def _is_accessor(name: str, ret: str, params: str) -> bool:
    """getter(인자 없는 get·is) · setter(인자 하나인 void set) — 요청 처리 메서드 getPosts(Long projectId) 는 아니다."""
    if re.fullmatch(r"(?:get|is)[A-Z]\w*", name):
        return not params
    if re.fullmatch(r"set[A-Z]\w*", name):
        return ret.strip() == "void" and params.count(",") == 0 and bool(params)
    return False


def java_items(content: str) -> list[Item]:
    """public 클래스와 public 메서드 — getter·setter 는 세기만 한다 (엔티티 접근자가 README 를 덮는다)."""
    cls = _JAVA_CLASS.search(content)
    if cls is None:
        return []
    accessors = 0
    methods: list[Item] = []
    for m in _JAVA_METHOD.finditer(content):
        if m.group("name") == cls.group("name"):  # 생성자
            continue
        params = " ".join(re.sub(r"@\w+(\([^)]*\))?\s*", "", m.group("params")).split())
        if _is_accessor(m.group("name"), m.group("ret"), params):
            accessors += 1
            continue
        methods.append(
            Item(m.group("name"), f"{' '.join(m.group('ret').split())} {m.group('name')}({params})")
        )
    head = Item(cls.group("name"), cls.group("name"), "getter·setter 생략" if accessors else "")
    return [head, *methods]


# ── 지도 ───────────────────────────────────────────────────────────


def _norm_route(r: str) -> str:
    return re.sub(r"\{(\w+)\}", r":\1", r.rstrip("/") or "/")


def starter_view(template_root: Path, starter: Sequence[GeneratedFile]) -> list[GeneratedFile]:
    """지원자가 받는 모양 — 템플릿 project/ 의 코드 파일 위에 시작 코드 출력 (응시용 이미지와 같은 층)."""
    project = template_root / "project"
    files = {
        str(p.relative_to(project)): p.read_text("utf-8")
        for p in sorted(project.rglob("*"))
        if p.is_file() and p.suffix in (".ts", ".tsx", ".java") and "node_modules" not in p.parts
    }
    files.update({f.path: f.content for f in starter})
    return [GeneratedFile(path=k, content=v) for k, v in files.items()]


def code_map(files: Sequence[GeneratedFile], contract: TaskContract | None = None) -> list[FileMap]:
    """시작 코드 파일 → 파일마다 이미 있는 이름. 테스트·빌드 설정·진입점(main)은 뺀다."""
    by_path = {f.path: f.content for f in files}
    screens = {_norm_route(s.route): s for s in (contract.screens if contract else [])}
    maps: list[FileMap] = []
    for path in sorted(by_path):
        text = by_path[path]
        if path.endswith((".ts", ".tsx")) and path.startswith("src/") and not _SKIP_TS.search(path):
            if path == APP_TYPES:
                names = list(dict.fromkeys(m.group("name") for m in _TS_TYPE.finditer(text)))
                maps.append(
                    FileMap(
                        path,
                        "계약에서 만든 타입 (잠김 — 고치지 않는다). 서버 응답은 `<Api>Ok` 타입으로 읽는다",
                        [Item(n, n) for n in names],
                    )
                )
                continue
            fm = FileMap(path, items=ts_items(text))
            if path == "src/mocks/handlers.ts":
                fm.note = "개발 서버의 가짜 API 응답 (채점에는 쓰이지 않는다)"
                fm.items = []
            routes = ts_routes(text)
            if routes:
                fm.note = "화면 경로: " + " · ".join(
                    f"`{r}` → `{el}`"
                    + (
                        f" ({screens[_norm_route(r)].screen_id})"
                        if _norm_route(r) in screens
                        else ""
                    )
                    for r, el in routes
                )
            if fm.items or fm.note:
                maps.append(fm)
        elif (
            path.endswith(".java")
            and "/src/test/" not in f"/{path}"
            and path.startswith("src/main/")
        ):
            items = java_items(text)
            if items:
                maps.append(FileMap(path, items=items))
    return maps


def code_map_markdown(maps: Sequence[FileMap]) -> str:
    if not maps:
        return ""
    ui = any(fm.path.endswith(".tsx") for fm in maps)
    lines = [
        SECTION,
        "",
        "시작 코드에 이미 있는 것입니다. 새 기능을 어느 "
        + ("함수·컴포넌트" if ui else "클래스·메서드")
        + " 이름으로 짤지는 정해져 있지 않습니다 —",
        "계약("
        + ("API 경로·형식, 화면 경로·test id" if ui else "API 경로·형식")
        + ")만 지키면 됩니다.",
        "",
    ]
    for fm in maps:
        head = f"- `{fm.path}`" + (f" — {fm.note}" if fm.note else "")
        lines.append(head)
        if fm.path == APP_TYPES:
            lines.append("  - " + ", ".join(f"`{i.name}`" for i in fm.items))
            continue
        for it in fm.items:
            lines.append(f"  - `{it.text}`" + (f" — {it.doc}" if it.doc else ""))
    return "\n".join(lines) + "\n"


_TICK = re.compile(r"`([^`]+)`")


def code_map_problems(markdown: str, files: Sequence[GeneratedFile]) -> list[str]:
    """README 의 '시작 코드' 절이 시작 코드와 맞는가 — 적힌 파일이 있고, 이름·화면 경로가 그 파일에 있다."""
    if SECTION not in markdown:
        return []
    body = markdown.split(SECTION, 1)[1].split("\n## ", 1)[0]
    by_path = {f.path: f.content for f in files}
    out: list[str] = []
    current: str | None = None
    for line in body.splitlines():
        ticks = _TICK.findall(line)
        if line.startswith("- `") and ticks:
            current = ticks[0]
            if current not in by_path:
                out.append(f"README 시작 코드 절의 파일이 시작 코드에 없음: {current}")
                current = None
                continue
            for t in ticks[1:]:
                if t.startswith("/") and t not in by_path[current]:
                    out.append(f"README 의 화면 경로 {t} 가 {current} 에 없음")
                elif re.fullmatch(r"[A-Za-z_$][\w$]*", t) and t not in by_path[current]:
                    out.append(f"README 의 이름 {t} 가 {current} 에 없음")
        elif line.startswith("  - ") and current is not None:
            for t in ticks:
                name = re.match(r"(?:[\w<>\[\],.? ]+\s)?([A-Za-z_$][\w$]*)\s*\(", t)
                ident = name.group(1) if name else t
                if re.fullmatch(r"[A-Za-z_$][\w$]*", ident) and not re.search(
                    rf"\b{re.escape(ident)}\b", by_path[current]
                ):
                    out.append(f"README 의 이름 {ident} 가 {current} 에 없음")
    return out
