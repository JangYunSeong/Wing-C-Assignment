"""역량 카드 (retrieve T1 검색 대상) — 온톨로지(PG, 버전 고정)에서 읽어 메모리에 만든다.

카드 본문 = 라벨 · 매처 패턴 어휘 · ESCO 라벨 · NCS 능력단위명 · 도메인 · 같이 나온 도구(별칭 포함).
예문 = 검수된 공고 문장(line_competency) — 문장 하나가 문서 하나 (예문 검색 채널).
범위 = 매처 규칙의 subs (= competency_scope). 범위 판정은 매처와 같은 in_scope 를 쓴다.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.matcher import Matcher, in_scope
from task_spec.retrieve.text import pattern_terms


@dataclass
class CompetencyCard:
    id: str
    label: str
    area: str
    subs: list[str] | None  # None = 범위 제한 없음
    status: str | None = None
    terms: list[str] = field(default_factory=list)
    esco: list[str] = field(default_factory=list)
    ncs: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    def in_scope(self, sub: str | None) -> bool:
        return True if not sub else in_scope(sub, self.subs)

    def doc(self) -> str:
        return " ".join(
            [self.label, " ".join(self.terms), " ".join(self.esco), " ".join(self.ncs)]
            + [" ".join(self.domains), " ".join(self.tools)]
        )


@dataclass(frozen=True)
class ExampleLine:
    line_id: str
    position_id: str
    sub: str | None
    text: str
    comps: tuple[str, ...]


@dataclass
class CardSet:
    version: str
    cards: dict[str, CompetencyCard]
    examples: list[ExampleLine]
    sub_labels: dict[str, str] = field(default_factory=dict)


async def load_cards(repo: OntologyRepository, matcher: Matcher, *, top_tools: int = 8) -> CardSet:
    status = {r["id"]: r["status"] for r in await repo.fetch("SELECT id, status FROM competency")}
    cards = {
        c["id"]: CompetencyCard(
            id=c["id"],
            label=c["label"],
            area=c["area"],
            subs=c.get("subs"),
            status=status.get(c["id"]),
            terms=pattern_terms(c.get("obj")) + pattern_terms(c.get("act")),
        )
        for c in matcher.comps
    }
    for r in await repo.fetch("SELECT competency_id, esco_label FROM competency_esco"):
        if r["competency_id"] in cards and r["esco_label"]:
            cards[r["competency_id"]].esco.append(r["esco_label"])
    for r in await repo.fetch(
        "SELECT DISTINCT cn.competency_id, u.name FROM competency_ncs cn "
        "JOIN ncs_unit u ON u.cl_cd = cn.unit_cl_cd"
    ):
        if r["competency_id"] in cards:
            cards[r["competency_id"]].ncs.append(r["name"])
    for r in await repo.fetch("SELECT competency_id, domain FROM competency_domain"):
        if r["competency_id"] in cards:
            cards[r["competency_id"]].domains.append(r["domain"])

    co: dict[str, Counter[str]] = defaultdict(Counter)
    for r in await repo.fetch(
        "SELECT lc.competency_id, lt.tool_id, count(*) AS n FROM line_competency lc "
        "JOIN line_tool lt ON lt.line_id = lc.line_id GROUP BY 1, 2"
    ):
        co[r["competency_id"]][r["tool_id"]] += r["n"]
    for cid, cnt in co.items():
        if cid not in cards:
            continue
        for tid, _ in cnt.most_common(top_tools):
            node = matcher.tech.get(tid, {})
            cards[cid].tools.extend([node.get("label", tid), *(node.get("aliases") or [])])

    rows = await repo.fetch(
        "SELECT l.id, l.position_id, p.sub, l.text, array_agg(lc.competency_id) AS comps "
        "FROM posting_line l JOIN line_competency lc ON lc.line_id = l.id "
        "JOIN position p ON p.id = l.position_id GROUP BY 1, 2, 3, 4 ORDER BY 1"
    )
    examples = [
        ExampleLine(r["id"], r["position_id"], r["sub"], r["text"], tuple(sorted(r["comps"])))
        for r in rows
    ]
    subs = {r["id"]: r["label"] for r in await repo.fetch("SELECT id, label FROM sub_family")}
    return CardSet(version=repo.version.version, cards=cards, examples=examples, sub_labels=subs)
