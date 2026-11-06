"""공통 과제 유형(incident / feature) 정하기 — 결정적, 공고만 본다 (불변식 3) [2026-11-06].

우선순위 (nodes/spec.py 가 적용):
1. 승인자 수정 요청의 task_kind (revise, 공통 과제를 다시 만들 때)
2. 그 공고에서 이미 승인된 공통 과제의 유형 — 같은 공고 지원자는 같은 과제 (비교 가능성)
3. 기본값 규칙 (이 모듈): 직군별 데이터 파일(resources/task_kind/<sub>.json)이 있으면 공고 **주** 역량 중
   진단 역량이 만드는 역량보다 많을 때만 incident, 아니면 feature [확정: 2026-11-06 사용자 — 동률은 feature].
   데이터 파일이 없는 직군은 incident (기능형은 백엔드부터 — 설계 §0).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from task_spec.schemas import PostingProfile, TaskKind

DEFAULT_KIND: TaskKind = "incident"


@dataclass(frozen=True)
class KindLists:
    build: frozenset[str]
    diagnose: frozenset[str]


@dataclass(frozen=True)
class KindChoice:
    kind: TaskKind
    basis: str  # 승인 화면·로그용 근거 (역량 개수만 — 공고 원문 없음)


@lru_cache(maxsize=16)
def kind_lists(sub: str) -> KindLists | None:
    """직군 데이터 파일. 없으면 None (그 직군은 기본값 규칙을 쓰지 않음)."""
    path = resources.files("task_spec.resources.task_kind") / f"{sub}.json"
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    build, diagnose = frozenset(raw["build"]), frozenset(raw["diagnose"])
    if build & diagnose:
        raise ValueError(f"task_kind/{sub}.json: build 와 diagnose 에 같은 역량이 있음")
    return KindLists(build=build, diagnose=diagnose)


def choose_task_kind(profile: PostingProfile) -> KindChoice:
    lists = kind_lists(profile.sub) if profile.sub else None
    if lists is None:
        return KindChoice(DEFAULT_KIND, f"직군 {profile.sub or '미상'} 은 유형 규칙 없음 — 이슈형")
    primary = {c.competency_id for c in profile.competencies if c.primary}
    n_build, n_diag = len(primary & lists.build), len(primary & lists.diagnose)
    kind: TaskKind = "incident" if n_diag > n_build else "feature"
    return KindChoice(kind, f"공고 주 역량 중 만드는 역량 {n_build}개 · 진단 역량 {n_diag}개")
