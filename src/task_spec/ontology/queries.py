"""① 공고 프로필·③ 평가 근거 질의 (결정적, 읽기 전용 — ontology-artifact 스킬).

- 공고 역량 주/보조 [확정: 2026-11-02]: 담당업무(duties)·자격요건(required)에 나오면 주, 우대(preferred)에만 나오면 보조.
- NCS 범위는 온톨로지 뷰 `v_position_competency_ncs` 와 같은 규칙: 셀(직군|업종)의 cell_ncs 에 세분류가 있으면
  primary, 시범 직군이면서 보조 직군의 sub_ncs 에 있으면 secondary, 둘 다 아니면 제외. 새 공고(position 없음)에도
  쓰도록 (역량, 직군, 업종) 을 직접 받는다.
- 셀에 NCS 목록(cell_ncs)이 하나도 없으면 직군 단위 sub_ncs 로 대체 — scope=sub_fallback [확정: 2026-11-02].
  온톨로지 뷰에는 없는 규칙이다 (뷰는 이 경우 근거 없음). 경고 posting_cell_unseen 과 함께.
- ESCO 는 대표(representative) 연결만.
"""

from __future__ import annotations

from collections import defaultdict

from task_spec.ontology.repository import OntologyRepository
from task_spec.schemas import (
    CompetencyEvidence,
    EscoRef,
    NcsCriterion,
    NcsKsa,
    NcsUnitRef,
    PostingCompetency,
    PostingProfile,
    PostingTool,
)

PRIMARY_SECTIONS = frozenset({"duties", "required"})
_SECTION_ORDER = {"duties": 0, "required": 1, "preferred": 2}


def _sections(values: list[str] | None) -> list[str]:
    return sorted({v for v in values or [] if v in _SECTION_ORDER}, key=_SECTION_ORDER.__getitem__)


async def posting_exists(repo: OntologyRepository, posting_id: str) -> bool:
    rows = await repo.fetch("SELECT 1 AS ok FROM position WHERE id = %s", (posting_id,))
    return bool(rows)


async def posting_profile(repo: OntologyRepository, posting_id: str) -> PostingProfile | None:
    pos = await repo.fetch(
        "SELECT id, company, position_name, sub, secondary_sub, industry_id, in_pilot FROM position WHERE id = %s",
        (posting_id,),
    )
    if not pos:
        return None
    p = pos[0]
    comps = await repo.fetch(
        """
        SELECT lc.competency_id, c.label,
               array_agg(DISTINCT l.section) AS sections,
               array_remove(array_agg(DISTINCT l.level), NULL) AS levels,
               array_agg(DISTINCT l.id) AS line_ids
          FROM posting_line l
          JOIN line_competency lc ON lc.line_id = l.id
          JOIN competency c ON c.id = lc.competency_id
         WHERE l.position_id = %s
         GROUP BY lc.competency_id, c.label
         ORDER BY lc.competency_id
        """,
        (posting_id,),
    )
    tools = await repo.fetch(
        """
        SELECT lt.tool_id, t.label, t.kind, t.family_id, array_agg(DISTINCT l.section) AS sections
          FROM posting_line l
          JOIN line_tool lt ON lt.line_id = l.id
          JOIN tool t ON t.id = lt.tool_id
         WHERE l.position_id = %s
         GROUP BY lt.tool_id, t.label, t.kind, t.family_id
         ORDER BY lt.tool_id
        """,
        (posting_id,),
    )
    return PostingProfile(
        posting_id=p["id"],
        ontology_version=repo.version.version,
        source="ontology",
        company=p["company"],
        position_name=p["position_name"],
        sub=p["sub"],
        secondary_sub=p["secondary_sub"],
        industry_id=p["industry_id"],
        in_pilot=bool(p["in_pilot"]),
        competencies=[
            PostingCompetency(
                competency_id=r["competency_id"],
                label=r["label"],
                sections=_sections(r["sections"]),  # type: ignore[arg-type]
                primary=bool(PRIMARY_SECTIONS & set(r["sections"] or [])),
                levels=sorted(r["levels"] or []),
                line_ids=sorted(r["line_ids"] or []),
            )
            for r in comps
        ],
        tools=[
            PostingTool(
                tool_id=r["tool_id"],
                label=r["label"],
                kind=r["kind"],
                family_id=r["family_id"],
                sections=_sections(r["sections"]),  # type: ignore[arg-type]
            )
            for r in tools
        ],
    )


async def evidence(
    repo: OntologyRepository,
    competency_ids: list[str],
    *,
    sub: str,
    industry_id: str,
    secondary_sub: str | None = None,
    in_pilot: bool = False,
) -> list[CompetencyEvidence]:
    if not competency_ids:
        return []
    cell_id = f"{sub}|{industry_id}"
    # 건수는 능력단위별로 한 번에 묶어 센다 (행마다 하위 질의를 돌리면 역량이 많은 공고에서 수 초 걸린다)
    ncs = await repo.fetch(
        """
        WITH cu AS (
            SELECT cn.competency_id, cn.unit_cl_cd, cn.subd_code, cn.rel
              FROM competency_ncs cn
             WHERE cn.competency_id = ANY(%(ids)s)
        ), units AS (SELECT DISTINCT unit_cl_cd FROM cu),
        el AS (
            SELECT e.unit_cl_cd, e.id FROM ncs_element e JOIN units USING (unit_cl_cd)
        ),
        n_el AS (SELECT unit_cl_cd, count(*) AS n FROM el GROUP BY unit_cl_cd),
        n_cr AS (
            SELECT el.unit_cl_cd, count(*) AS n FROM el JOIN ncs_criterion cr ON cr.element_id = el.id GROUP BY el.unit_cl_cd
        ),
        n_ksa AS (
            SELECT el.unit_cl_cd, count(*) AS n FROM el JOIN ncs_ksa k ON k.element_id = el.id GROUP BY el.unit_cl_cd
        )
        SELECT cu.competency_id, cu.unit_cl_cd, cu.subd_code, u.name, u.level, cu.rel,
               CASE WHEN EXISTS (SELECT 1 FROM cell_ncs cs WHERE cs.cell_id = %(cell)s AND cs.subd_code = cu.subd_code)
                         THEN 'primary'
                    WHEN %(pilot)s AND EXISTS (SELECT 1 FROM sub_ncs sn WHERE sn.sub = %(sec)s AND sn.subd_code = cu.subd_code)
                         THEN 'secondary'
                    WHEN NOT EXISTS (SELECT 1 FROM cell_ncs cs WHERE cs.cell_id = %(cell)s)
                         AND EXISTS (SELECT 1 FROM sub_ncs sn WHERE sn.sub = %(sub)s AND sn.subd_code = cu.subd_code)
                         THEN 'sub_fallback'
               END AS scope,
               coalesce(n_el.n, 0) AS n_elements, coalesce(n_cr.n, 0) AS n_criteria, coalesce(n_ksa.n, 0) AS n_ksa
          FROM cu
          JOIN ncs_unit u ON u.cl_cd = cu.unit_cl_cd
          LEFT JOIN n_el ON n_el.unit_cl_cd = cu.unit_cl_cd
          LEFT JOIN n_cr ON n_cr.unit_cl_cd = cu.unit_cl_cd
          LEFT JOIN n_ksa ON n_ksa.unit_cl_cd = cu.unit_cl_cd
         ORDER BY cu.competency_id, cu.unit_cl_cd
        """,
        {
            "cell": cell_id,
            "pilot": in_pilot,
            "sec": secondary_sub,
            "sub": sub,
            "ids": competency_ids,
        },  # type: ignore[arg-type]
    )
    esco = await repo.fetch(
        """
        SELECT competency_id, esco_uri, esco_label, rel FROM competency_esco
         WHERE competency_id = ANY(%s) AND representative
         ORDER BY competency_id, esco_uri
        """,
        (competency_ids,),
    )
    units: dict[str, list[NcsUnitRef]] = defaultdict(list)
    for r in ncs:
        if r["scope"] is None:  # 셀 밖 — 근거로 쓰지 않는다
            continue
        units[r["competency_id"]].append(
            NcsUnitRef(
                unit_cl_cd=r["unit_cl_cd"],
                subd_code=r["subd_code"],
                name=r["name"],
                level=r["level"],
                scope=r["scope"],
                rel=r["rel"],
                n_elements=r["n_elements"],
                n_criteria=r["n_criteria"],
                n_ksa=r["n_ksa"],
            )
        )
    escos: dict[str, list[EscoRef]] = defaultdict(list)
    for r in esco:
        escos[r["competency_id"]].append(
            EscoRef(uri=r["esco_uri"], label=r["esco_label"], rel=r["rel"])
        )
    return [
        CompetencyEvidence(competency_id=c, ncs_units=units.get(c, []), esco=escos.get(c, []))
        for c in competency_ids
    ]


async def criteria_detail(
    repo: OntologyRepository,
    unit_ids: list[str],
    *,
    max_criteria: int = 12,
    max_ksa: int = 10,
) -> tuple[list[NcsCriterion], list[NcsKsa]]:
    """능력단위 → 수행준거·KSA 본문 (명세 근거). id = '요소id#번호'. 단위 순서·요소 순서대로 상한까지."""
    if not unit_ids:
        return [], []
    order = {u: i for i, u in enumerate(unit_ids)}
    rows = await repo.fetch(
        """
        SELECT e.unit_cl_cd, u.name AS unit_name, e.id AS element_id, e.no AS el_no, e.name AS element_name,
               cr.no, cr.text
          FROM ncs_element e
          JOIN ncs_unit u ON u.cl_cd = e.unit_cl_cd
          JOIN ncs_criterion cr ON cr.element_id = e.id
         WHERE e.unit_cl_cd = ANY(%s)
         ORDER BY e.unit_cl_cd, e.no, cr.no
        """,
        (unit_ids,),
    )
    ksa = await repo.fetch(
        """
        SELECT e.unit_cl_cd, k.element_id, k.kind, k.no, k.text
          FROM ncs_element e JOIN ncs_ksa k ON k.element_id = e.id
         WHERE e.unit_cl_cd = ANY(%s)
         ORDER BY e.unit_cl_cd, e.no, k.kind, k.no
        """,
        (unit_ids,),
    )
    rows = sorted(rows, key=lambda r: (order[r["unit_cl_cd"]], r["el_no"], r["no"]))
    crit = [
        NcsCriterion(
            criterion_id=f"{r['element_id']}#{r['no']}",
            unit_cl_cd=r["unit_cl_cd"],
            unit_name=r["unit_name"],
            element_name=r["element_name"],
            text=r["text"],
        )
        for r in _round_robin(rows, "unit_cl_cd", max_criteria)
    ]
    ks = [
        NcsKsa(
            ksa_id=f"{r['element_id']}#{r['kind']}{r['no']}",
            unit_cl_cd=r["unit_cl_cd"],
            kind=r["kind"],
            text=r["text"],
        )
        for r in _round_robin(
            sorted(ksa, key=lambda r: order[r["unit_cl_cd"]]), "unit_cl_cd", max_ksa
        )
    ]
    return crit, ks


def _round_robin(rows: list[dict], key: str, limit: int) -> list[dict]:  # type: ignore[type-arg]
    """단위마다 번갈아 뽑아 상한까지 (한 단위가 근거를 독차지하지 않게). 단위 안 순서는 유지."""
    groups: dict[str, list[dict]] = defaultdict(list)  # type: ignore[type-arg]
    for r in rows:
        groups[r[key]].append(r)
    out: list[dict] = []  # type: ignore[type-arg]
    i = 0
    while len(out) < limit and any(i < len(g) for g in groups.values()):
        for g in groups.values():
            if i < len(g) and len(out) < limit:
                out.append(g[i])
        i += 1
    return out


async def field_labels(
    repo: OntologyRepository, sub: str | None, industry_id: str | None
) -> tuple[str, str]:
    """직군·업종 → 'id: 라벨' (과제 서술 입력). 모르면 id 그대로."""
    subs = await repo.fetch("SELECT id, label FROM sub_family WHERE id = %s", (sub or "",))
    inds = await repo.fetch("SELECT id, label FROM industry WHERE id = %s", (industry_id or "",))
    s = f"{sub}: {subs[0]['label']}" if subs else (sub or "(미상)")
    i = inds[0]["label"] if inds else (industry_id or "(미상)")
    return s, i


async def language_labels(repo: OntologyRepository) -> list[str]:
    """온톨로지의 언어 도구 이름 전체 (과제 본문에 공고 밖 언어가 나오는지 검사)."""
    rows = await repo.fetch("SELECT label FROM tool WHERE kind = 'language' ORDER BY label")
    return [r["label"] for r in rows]
