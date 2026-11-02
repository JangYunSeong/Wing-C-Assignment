"""공고 밖 역량의 직무 연계 판정 (결정적, 온톨로지 읽기 전용) [확정: 2026-11-02].

신호
① 공고 역량과 관계: competency_near(근접) · competency_builds_on(기반) — 양방향
② 같은 직군 공고 비율: 시범 직군 포지션 중 이 역량을 요구한 비율 (line_competency)
③ 범위 명시: competency_scope 에 지원 직군이 들어 있음 (범위 제한 없는 역량은 해당 없음)

등급: 강 = ① 또는 ② ≥ 10% / 중 = ② ≥ 2% 또는 ③ / 약 = 그 밖 (검증 후보에서 제외).
②는 공고 표본 편향을 물려받으므로 ①·③과 함께 본다 (#68).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.matcher import in_scope

Level = Literal["strong", "medium", "weak"]
STRONG_SHARE = 0.10
MEDIUM_SHARE = 0.02


@dataclass
class Relatedness:
    pairs: dict[str, set[str]] = field(default_factory=dict)  # 역량 → 근접·기반 관계 역량 (양방향)
    share: dict[str, dict[str, float]] = field(default_factory=dict)  # 직군 → 역량 → 공고 비율
    scopes: dict[str, list[str]] = field(default_factory=dict)  # 범위가 명시된 역량만

    def level(
        self, competency_id: str, sub: str | None, posting_ids: Iterable[str]
    ) -> tuple[Level, str]:
        linked = sorted(self.pairs.get(competency_id, set()) & set(posting_ids))
        if linked:
            return "strong", "공고 역량과 관계: " + ", ".join(linked)
        share = self.share.get(sub or "", {}).get(competency_id, 0.0)
        if share >= STRONG_SHARE:
            return "strong", f"같은 직군 공고의 {share:.0%} 가 요구"
        if share >= MEDIUM_SHARE:
            return "medium", f"같은 직군 공고의 {share:.0%} 가 요구"
        subs = self.scopes.get(competency_id)
        if sub and subs and in_scope(sub, subs):
            return "medium", "역량 범위에 이 직군이 명시됨"
        return "weak", "직무 연계 약함"


async def load_relatedness(repo: OntologyRepository) -> Relatedness:
    pairs: dict[str, set[str]] = defaultdict(set)
    for r in await repo.fetch("SELECT a_id, b_id FROM competency_near"):
        pairs[r["a_id"]].add(r["b_id"])
        pairs[r["b_id"]].add(r["a_id"])
    for r in await repo.fetch("SELECT competency_id, base_id FROM competency_builds_on"):
        pairs[r["competency_id"]].add(r["base_id"])
        pairs[r["base_id"]].add(r["competency_id"])
    share: dict[str, dict[str, float]] = defaultdict(dict)
    for r in await repo.fetch(
        """
        WITH pos AS (SELECT sub, count(*) AS n FROM position WHERE in_pilot GROUP BY sub)
        SELECT p.sub, lc.competency_id, count(DISTINCT p.id)::float / max(pos.n) AS share
          FROM line_competency lc
          JOIN posting_line l ON l.id = lc.line_id
          JOIN position p ON p.id = l.position_id
          JOIN pos ON pos.sub = p.sub
         WHERE p.in_pilot
         GROUP BY 1, 2
        """
    ):
        share[r["sub"]][r["competency_id"]] = float(r["share"])
    scopes: dict[str, list[str]] = defaultdict(list)
    for r in await repo.fetch("SELECT competency_id, sub_pattern FROM competency_scope"):
        scopes[r["competency_id"]].append(r["sub_pattern"])
    return Relatedness(pairs=dict(pairs), share=dict(share), scopes=dict(scopes))
