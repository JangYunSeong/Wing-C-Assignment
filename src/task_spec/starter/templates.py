"""시작 코드 템플릿 레지스트리 — 레포 `templates/<직군>/<스택>/vN/` + `CURRENT` (starter-templates.md §3·§6.1, #55).

LLM·네트워크 없음. 하는 일
- 템플릿 읽기·자체 점검 (`load_template`, `check_template`): manifest 와 폴더가 맞는지, 잠긴 파일·하네스가 있는지,
  이미지 층 규칙(템플릿·응시용 이미지에는 project/ 만)을 manifest 가 지키는지.
- 오버레이 경로 허용 판정 (`slot_allows`): kind 별 `fill_slots` 안 · `locked` 밖 · hidden 이 아니면 `reserved` 밖.
  경로 모양(상대 경로·`..`)은 `schemas.build.GeneratedFile` 이 먼저 본다.
- 응시용 이미지 레이어 검사의 금지 경로 (`candidate_layer_rules`, `forbidden_in`) [확정: 2026-11-07 사용자]:
  템플릿 hidden/(하네스·자체 점검)·manifest.yaml·채점 조립 위치·하네스 전용 의존성은 응시용 이미지 어느 층에도 없어야 한다.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from task_spec.schemas.template import SlotKind, TemplateManifest

# 레포의 templates/ — 패키지로 설치한 이미지(builder #129)에서는 TEMPLATES_DIR 로 준다
TEMPLATES_ROOT = Path(
    os.environ.get("TEMPLATES_DIR") or Path(__file__).resolve().parents[3] / "templates"
)
MANIFEST = "manifest.yaml"


@lru_cache(maxsize=256)
def _glob_rx(pattern: str) -> re.Pattern[str]:
    """`**/` = 0개 이상 폴더, `**` = 아무 경로, `*` = 폴더 안 아무 이름, `?` = 한 글자 (슬래시 제외)."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def glob_match(path: str, patterns: list[str]) -> bool:
    """path(프로젝트 기준 상대 경로, `/` 구분)가 glob 목록 중 하나에 맞는지. 폴더 패턴(`x/`)은 그 아래 전부."""
    for p in patterns:
        if p.endswith("/"):
            p = p + "**"
        if _glob_rx(p).match(path):
            return True
    return False


@dataclass(frozen=True)
class Template:
    root: Path  # templates/<직군>/<스택>/vN
    manifest: TemplateManifest

    @property
    def project(self) -> Path:
        return self.root / "project"

    @property
    def hidden(self) -> Path:
        return self.root / "hidden"

    def files(self, sub: str) -> list[str]:
        """sub(project · hidden/harness · hidden/selftest …) 아래 파일 — sub 기준 상대 경로, 정렬."""
        base = self.root / sub
        if not base.is_dir():
            return []
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())


def restrict_reasons(template: Template, codes: Collection[str]) -> Template:
    """실패 사유 목록을 codes 로 좁힌 템플릿 (codes 가 비면 그대로). 몇 번 불러도 같다.

    재료가 숨은 요구의 사유(FeatureSeed.reasons)를 적으면 계약 입력·검사만 그 목록을 보고, 숨은 테스트 생성기 입력과 정적 검사는
    템플릿 목록 전체를 봤다 — 2026-11-13 갱신 손실 알파(kakaobank_2026#003): 숨은 테스트가 '시간 초과·5xx 수' 확인에 재료에 없는
    pool_acquire_timeout 을 붙여, 충돌을 500 으로 돌려준 구현이 '풀 대기 초과'로 채점됐다 (재시도 중복 알파도 같은 표시)."""
    if not codes:
        return template
    allowed = set(codes)
    kept = [r for r in template.manifest.failure_reasons if r.code in allowed]
    if len(kept) == len(template.manifest.failure_reasons):
        return template
    return replace(
        template, manifest=template.manifest.model_copy(update={"failure_reasons": kept})
    )


def current_version(template_id: str, root: Path = TEMPLATES_ROOT) -> int:
    text = (root / template_id / "CURRENT").read_text("utf-8").strip()
    m = re.fullmatch(r"v(\d+)", text)
    if not m:
        raise ValueError(f"{template_id}/CURRENT 형식이 vN 이 아님: {text!r}")
    return int(m.group(1))


def available(root: Path = TEMPLATES_ROOT) -> list[str]:
    """CURRENT 가 있는 템플릿 id (예: backend/java-spring-boot3-jpa)."""
    return sorted(p.parent.relative_to(root).as_posix() for p in root.glob("*/*/CURRENT"))


# 직군(sub) → 첫 템플릿 (starter-templates §2). spec-to-code ② ① select_template — 결정적. 없는 직군은 None (문서 과제).
TEMPLATE_FOR_SUB: dict[str, str] = {
    "sw.backend": "backend/java-spring-boot3-jpa",
    "sw.frontend": "frontend/react-vite-ts",
    "data.eng": "data/python-duckdb",
}


def select_template(sub: str, root: Path = TEMPLATES_ROOT) -> Template | None:
    """직군에 맞는 템플릿 (CURRENT 판). 템플릿이 없는 직군이면 None."""
    template_id = TEMPLATE_FOR_SUB.get(sub)
    return load_template(template_id, root=root) if template_id else None


def is_java(m: TemplateManifest) -> bool:
    """Java 전용 산출물·검사(JPA 저장 확인, 클래스 상속 검사 등)를 쓸 템플릿인가."""
    return m.stack is not None and m.stack.language.lower() == "java"


def is_python(m: TemplateManifest) -> bool:
    """Python 템플릿(데이터 엔지니어 — contract.py·Python 검증기·Python 이미지)인가."""
    return m.stack is not None and m.stack.language.lower() == "python"


def load_template(
    template_id: str, version: int | None = None, root: Path = TEMPLATES_ROOT
) -> Template:
    v = version if version is not None else current_version(template_id, root)
    vdir = root / template_id / f"v{v}"
    data = yaml.safe_load((vdir / MANIFEST).read_text("utf-8"))
    return Template(root=vdir, manifest=TemplateManifest.model_validate(data))


def check_template(t: Template) -> list[str]:
    """템플릿 자체 점검 — 비면 통과."""
    m = t.manifest
    problems: list[str] = []
    expect_id = t.root.parent.relative_to(t.root.parents[2]).as_posix()
    if m.template_id != expect_id:
        problems.append(f"manifest template_id {m.template_id} 가 폴더 {expect_id} 와 다름")
    if t.root.name != f"v{m.version}":
        problems.append(f"manifest version {m.version} 가 폴더 {t.root.name} 와 다름")
    project = set(t.files("project"))
    if not project:
        problems.append("project/ 가 비어 있음")
    for path in m.locked:
        if path not in project:
            problems.append(f"잠긴 파일이 project/ 에 없음: {path}")
    if not t.files("hidden/harness"):
        problems.append("hidden/harness/ 가 비어 있음 — 하네스는 템플릿에 잠긴 코드로 둔다")
    if not t.files("hidden/selftest"):
        problems.append(
            "hidden/selftest/ 가 비어 있음 — 하네스 자체 점검이 없으면 HARNESS-ERR 를 가를 수 없다"
        )
    if m.commands is None or m.hidden_layout is None or m.limits is None:
        problems.append("manifest 에 commands · hidden_layout · limits 가 모두 있어야 함")
    if m.hidden_layout is not None:
        hl = m.hidden_layout
        for name, path in (("harness", hl.harness), ("selftest", hl.selftest), ("cases", hl.cases)):
            if not glob_match(path + "/x", m.reserved):
                problems.append(
                    f"hidden_layout.{name} ({path}) 가 reserved 밖 — 지원자 오버레이가 쓸 수 있음"
                )
        if not glob_match(hl.params, m.reserved):
            problems.append(f"hidden_layout.params ({hl.params}) 가 reserved 밖")
        for path in t.files("project"):
            if glob_match(path, m.reserved):
                problems.append(f"project/ 에 채점 자리 파일이 있음: {path}")
    # 이미지 층 규칙 (§6.1) — 템플릿·응시용 이미지에는 project/ 만, hidden/·manifest 는 채점용에서만
    if m.image.template_layer != ["project/"]:
        problems.append(f"image.template_layer 는 project/ 만이어야 함: {m.image.template_layer}")
    for must in ("hidden/", MANIFEST):
        if must not in m.image.grader_only:
            problems.append(f"image.grader_only 에 {must} 가 없음 — 응시용 이미지로 샐 수 있음")
    # 잠긴 파일은 slot_allows 가 늘 막으므로 slot 과 겹쳐도 된다. 채점 자리는 hidden 만 쓴다
    for kind, slots in m.fill_slots.items():
        for slot in slots:
            inside = glob_match(_example(slot), m.reserved)
            if kind == "hidden" and not inside:
                problems.append(
                    f"fill_slots.hidden 의 {slot} 가 reserved 밖 — 채점 자리 밖에 숨은 테스트를 쓰게 됨"
                )
            if kind != "hidden" and inside:
                problems.append(f"fill_slots.{kind} 의 {slot} 가 채점 자리(reserved) 안")
    return problems


def _example(pattern: str) -> str:
    """glob 에 맞는 예시 경로 하나 (점검용) — `**/` 는 없애고 `*`·`?` 는 x 로."""
    return pattern.replace("**/", "").replace("**", "x").replace("*", "x").replace("?", "x")


Blocked = Literal["locked", "reserved", "outside"]


def blocked_reason(m: TemplateManifest, kind: SlotKind, path: str) -> Blocked | None:
    """오버레이 kind 가 이 경로를 쓸 수 없는 까닭 — 잠긴 파일(locked) · 채점 자리(hidden 밖에서 reserved) · 쓸 수 있는 자리 밖
    (fill_slots 밖). 쓸 수 있으면 None (#178: 정적 검사가 까닭마다 '왜 막혔고 어디에 둘지'를 알린다)."""
    if path in m.locked or glob_match(path, m.locked):
        return "locked"
    if kind != "hidden" and glob_match(path, m.reserved):
        return "reserved"
    return None if glob_match(path, m.fill_slots.get(kind, [])) else "outside"


def slot_allows(m: TemplateManifest, kind: SlotKind, path: str) -> bool:
    """오버레이 kind 가 이 경로를 쓸 수 있나 — fill_slots 안, locked 밖, hidden 이 아니면 reserved 밖."""
    return blocked_reason(m, kind, path) is None


@dataclass(frozen=True)
class LayerRules:
    """응시용 이미지 레이어 검사 규칙 — 어느 층에도 있으면 안 되는 경로 glob 과 의존성 이름."""

    forbidden_globs: tuple[str, ...]
    forbidden_dependencies: tuple[str, ...]


def candidate_layer_rules(t: Template) -> LayerRules:
    """응시용 이미지(작업 경로 기준 + 이미지 어디든)에 있으면 안 되는 것.

    - 채점 자리(reserved)·hidden_layout 의 경로 — 과제 hidden 오버레이·조절값 파일
    - 템플릿 hidden/ 의 파일 이름(하네스·자체 점검) — 다른 곳에 복사돼도 잡도록 이름으로도
    - manifest.yaml · README.tpl.md 등 grader_only, 그리고 hidden/ 폴더 자체
    - 하네스 전용 의존성 (image.grader_only_dependencies)
    """
    m = t.manifest
    globs: list[str] = [*m.reserved]
    if m.hidden_layout is not None:
        hl = m.hidden_layout
        globs += [f"{hl.harness}/**", f"{hl.selftest}/**", f"{hl.cases}/**", hl.params]
    for sub in ("hidden/harness", "hidden/selftest"):
        globs += [f"**/{Path(f).name}" for f in t.files(sub)]
    for item in m.image.grader_only:
        globs.append(f"**/{item}**" if item.endswith("/") else f"**/{item}")
    # 상대 경로 glob 은 이미지 어디(작업 경로 아래 등)에 있어도 걸리게 앞에 **/ 를 붙인다
    globs = [g if g.startswith("**/") else f"**/{g}" for g in globs]
    return LayerRules(
        forbidden_globs=tuple(dict.fromkeys(globs)),
        forbidden_dependencies=tuple(m.image.grader_only_dependencies),
    )


def forbidden_in(paths: list[str], rules: LayerRules) -> list[str]:
    """이미지에서 꺼낸 파일 경로(작업 경로 기준 또는 절대 경로) 중 금지 경로에 걸리는 것."""
    return [p for p in paths if glob_match(p.lstrip("/"), list(rules.forbidden_globs))]
