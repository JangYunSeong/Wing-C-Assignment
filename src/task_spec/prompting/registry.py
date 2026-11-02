"""prompts/<task>/vN.md + CURRENT 로더.

- 기존 vN.md 는 고치지 않는다 (훅 protect-prompt-versions 가 막는다). 로더는 읽기만 한다.
- 머리말 스키마·템플릿 변수·입출력 스키마 클래스를 로드 시점에 검증한다.
- 해시는 조립 전 템플릿 파일 바이트 기준 (prompt-registry §3).
"""

from __future__ import annotations

import hashlib
import re
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from task_spec import schemas

_VERSION_FILE = re.compile(r"^v(\d+)\.md$")
_VAR = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
_SECTION = re.compile(r"^## (system|user)\s*$", re.MULTILINE)

# 입력 스키마 필드 외에 조립기가 채우는 변수
RESERVED_VARS = frozenset({"criteria", "examples", "output_schema"})


class PromptError(Exception):
    """프롬프트 파일·머리말·변수 오류."""


class FewshotSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: str
    k: int = Field(ge=0, le=10)
    match: list[str] = Field(default_factory=list)


class FrontMatter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    version: int = Field(ge=1)
    model: str = Field(pattern=r"^[a-z0-9_-]+:.+$")  # <provider>:<model>
    params: dict[str, Any] = Field(default_factory=dict)
    input_schema: str
    output_schema: str
    criteria: list[str] = Field(default_factory=list)
    fewshot: FewshotSpec | None = None
    eval: dict[str, Any] | str = "pending"
    changed: str = Field(min_length=1)


class PromptTemplate(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    meta: FrontMatter
    system: str
    user: str
    sha256: str
    path: Path

    @property
    def ref(self) -> str:
        return f"{self.meta.id}@{self.meta.version}"

    @cached_property
    def input_model(self) -> type[BaseModel]:
        return _schema_class(self.meta.input_schema)

    @cached_property
    def output_model(self) -> type[BaseModel]:
        return _schema_class(self.meta.output_schema)

    def variables(self) -> set[str]:
        return set(_VAR.findall(self.system)) | set(_VAR.findall(self.user))


def _schema_class(name: str) -> type[BaseModel]:
    cls = getattr(schemas, name, None)
    if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise PromptError(f"스키마 클래스를 찾을 수 없습니다: {name}")
    return cls


def parse_template(path: Path) -> PromptTemplate:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if not text.startswith("---\n"):
        raise PromptError(f"{path}: 머리말(---)이 없습니다")
    try:
        _, fm, body = text.split("---\n", 2)
    except ValueError as exc:
        raise PromptError(f"{path}: 머리말이 닫히지 않았습니다") from exc
    try:
        meta = FrontMatter.model_validate(yaml.safe_load(fm))
    except (yaml.YAMLError, ValidationError) as exc:
        raise PromptError(f"{path}: 머리말 오류 — {exc}") from exc

    m = _VERSION_FILE.match(path.name)
    if m is None or int(m.group(1)) != meta.version:
        raise PromptError(f"{path}: 파일명 버전과 머리말 version 이 다릅니다")
    if path.parent.name != meta.id:
        raise PromptError(f"{path}: 디렉토리 이름과 머리말 id 가 다릅니다")

    parts = _SECTION.split(body)
    # ["前文", "system", "...", "user", "..."]
    sections = dict(zip(parts[1::2], parts[2::2], strict=True))
    if set(sections) != {"system", "user"}:
        raise PromptError(f"{path}: '## system' 과 '## user' 절이 각각 하나씩 있어야 합니다")

    tpl = PromptTemplate(
        meta=meta,
        system=sections["system"].strip(),
        user=sections["user"].strip(),
        sha256=hashlib.sha256(raw).hexdigest(),
        path=path,
    )
    expected = set(tpl.input_model.model_fields) | RESERVED_VARS
    used = tpl.variables()
    if unknown := used - expected:
        raise PromptError(f"{path}: 알 수 없는 템플릿 변수 {sorted(unknown)}")
    if missing := set(tpl.input_model.model_fields) - used:
        raise PromptError(f"{path}: 입력 필드가 템플릿에서 빠졌습니다 {sorted(missing)}")
    _ = tpl.output_model
    return tpl


class PromptRegistry:
    """prompts 루트 하나. CURRENT 는 시작 시 전부 검증한다."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[tuple[str, int], PromptTemplate] = {}

    def current_version(self, task: str) -> int:
        f = self.root / task / "CURRENT"
        if not f.is_file():
            raise PromptError(f"{task}: CURRENT 가 없습니다")
        value = f.read_text(encoding="utf-8").strip()
        m = re.fullmatch(r"v(\d+)", value)
        if m is None:
            raise PromptError(f"{task}: CURRENT 형식은 'vN' 입니다")
        return int(m.group(1))

    def get(self, task: str, version: int | None = None) -> PromptTemplate:
        v = version if version is not None else self.current_version(task)
        key = (task, v)
        if key not in self._cache:
            path = self.root / task / f"v{v}.md"
            if not path.is_file():
                raise PromptError(f"{task}: v{v}.md 가 없습니다")
            self._cache[key] = parse_template(path)
        return self._cache[key]

    def validate_all(self) -> list[str]:
        """모든 작업의 CURRENT 를 로드해 검증한다. 검증한 ref 목록을 돌려준다."""
        refs = []
        for d in sorted(p for p in self.root.iterdir() if p.is_dir()):
            refs.append(self.get(d.name).ref)
        return refs
