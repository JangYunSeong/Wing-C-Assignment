"""공개 사전 로더 (`task_spec/resources/dicts/`, README 참고)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property, lru_cache
from importlib import resources

# 별칭 뒤에 붙어도 되는 조사·접미 (이 외의 한글이 바로 붙으면 다른 단어로 본다 — "카카오톡" ≠ "카카오")
_PARTICLES = (
    "에서의|에서는|에서|으로|에게|께서|과정|출신|졸업|재학|입학|학부|대학원|재직|근무|인턴|본사|"
    "코리아|부문|그룹|은|는|이|가|을|를|에|의|와|과|로|도|만|및"
)
LEFT = r"(?<![가-힣A-Za-z0-9])"
RIGHT = rf"(?=$|[^가-힣A-Za-z0-9]|(?:{_PARTICLES}))"
# 별칭 바로 뒤에 붙어도 같은 실체로 보는 접미 (허용 목록 — "카카오톡" 같은 제품명은 지키도록 좁게, 2026-11-04).
# 학교+생("서울대생"), 회사+조직 약어("삼성전자DS"·"LG전자CTO"), 회사+클론·클라우드("TossClone"·"네이버클라우드").
# 별칭 부분만 치환하고 접미는 남긴다 ("[ORG_1]DS"). 'Clone'·'Cloud' 와 대문자 약어는 대소문자를 구분한다.
SUFFIX_ATTACHED = r"(?=생(?![가-힣])|클론|클라우드|Clone|Cloud|[A-Z]{2,4}(?![a-z]))"
SCHOOL_ONLY_SUFFIX = "생"


@dataclass(frozen=True)
class DictEntry:
    canonical: str
    aliases: tuple[str, ...]
    category: str
    needs_context: bool


@dataclass(frozen=True)
class Dictionaries:
    entries: tuple[DictEntry, ...]
    surnames: frozenset[str]
    tech_terms: frozenset[str]

    @cached_property
    def all_aliases(self) -> frozenset[str]:
        return frozenset(a for e in self.entries for a in e.aliases)

    @cached_property
    def _by_alias(self) -> dict[str, DictEntry]:
        return {a.casefold(): e for e in self.entries for a in e.aliases}

    @cached_property
    def _pattern(self) -> re.Pattern[str]:
        aliases = sorted(self.all_aliases, key=len, reverse=True)
        alt = "|".join(re.escape(a) for a in aliases)
        return re.compile(rf"{LEFT}(?:{alt}){RIGHT}", re.IGNORECASE)

    def pattern(self) -> re.Pattern[str]:
        """모든 별칭을 긴 것부터 하나의 정규식으로 (한 번만 컴파일)."""
        return self._pattern

    @cached_property
    def _attached_pattern(self) -> re.Pattern[str]:
        aliases = sorted(self.all_aliases, key=len, reverse=True)
        alt = "|".join(re.escape(a) for a in aliases)
        return re.compile(rf"{LEFT}(?i:{alt}){SUFFIX_ATTACHED}")

    def attached_pattern(self) -> re.Pattern[str]:
        """별칭 + 허용 접미 (SUFFIX_ATTACHED). 매치는 별칭 부분만."""
        return self._attached_pattern

    def entry_for(self, alias: str) -> DictEntry | None:
        return self._by_alias.get(alias.casefold())


def _read(name: str) -> str:
    return (resources.files("task_spec.resources.dicts") / name).read_text(encoding="utf-8")


def _entries(name: str, category: str) -> list[DictEntry]:
    out = []
    for raw in _read(name).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        needs_context = line.startswith("!")
        parts = tuple(p.strip() for p in line.lstrip("!").split("|") if p.strip())
        out.append(DictEntry(parts[0], parts, category, needs_context))
    return out


def _words(name: str) -> frozenset[str]:
    words: set[str] = set()
    for raw in _read(name).splitlines():
        if raw.strip().startswith("#"):
            continue
        words.update(raw.split())
    return frozenset(words)


@lru_cache(maxsize=1)
def load_dictionaries() -> Dictionaries:
    return Dictionaries(
        entries=tuple(
            _entries("universities.txt", "SCHOOL")
            + _entries("companies.txt", "ORG")
            + _entries("courses.txt", "COURSE")
        ),
        surnames=_words("surnames.txt"),
        tech_terms=_words("tech_terms.txt"),
    )
