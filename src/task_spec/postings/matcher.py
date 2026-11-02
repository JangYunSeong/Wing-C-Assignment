"""온톨로지 공고 문장 매처 사본 (#59 §2 (b): 사본 + 산출물 규칙).

원본: JangYunSeong/job-competency-ontology `scripts/common/competency_match.py` @ b69d00f.
**판정 로직은 원본과 같게 유지한다** — 바뀐 것은 규칙을 파일 경로 대신 dict(PG `public.ontology_rules`,
적재 버전과 같은 버전)로 받는 것뿐이다. 원본이 바뀌면 이 파일도 같이 갱신하고 커밋을 고친다.
동일성 검사: tests/postings/test_matcher_parity_pg.py (온톨로지 전 공고 문장 재매칭 = line_competency).

    m = Matcher.from_rules(rules)          # rules: {"data/rules/competencies/competencies.json": {...}, ...}
    m.classify(line, sub) -> dict(kind="comp"|"non"|"unmatched", comps=[ids], non_type=str|None, level=str|None)
    m.tool_ids(line) -> [tool ids]
"""

from __future__ import annotations

import collections
import re
from typing import Any

SOURCE_COMMIT = "b69d00f"
COMP = "data/rules/competencies/"
TOOLS = "data/rules/tools/"
SEED = "data/build/seed/"


def norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip()).lower()


PILOT = ("sw", "data", "infra", "sales.marketing")
SCOPED_ONLY = ("sales.marketing",)


def in_pilot(sub: str | None) -> bool:
    return bool(sub) and (sub in PILOT or sub.split(".")[0] in PILOT)  # type: ignore[union-attr]


def in_scope(sub: str, subs: list[str] | None) -> bool:
    if not subs:
        return sub not in SCOPED_ONLY
    return any(sub == s or (s.endswith(".*") and sub.startswith(s[:-1])) for s in subs)


class RulesError(ValueError):
    pass


class Matcher:
    def __init__(self, rules: dict[str, Any]) -> None:
        def ld(path: str) -> Any:
            if path not in rules:
                raise RulesError(f"규칙 없음: {path}")
            return rules[path]

        C = ld(COMP + "competencies.json")
        self.meta = C["meta"]
        self.comps = [dict(c) for c in C["competencies"]]
        dup = [i for i, n in collections.Counter(c["id"] for c in self.comps).items() if n > 1]
        if dup:
            raise RulesError(f"duplicate competency ids: {dup}")

        def rx(s: str | None) -> re.Pattern[str] | None:
            return re.compile(s, re.I) if s else None

        for c in self.comps:
            c["_obj"], c["_act"], c["_not"] = rx(c["obj"]), rx(c.get("act")), rx(c.get("not"))
        self.nc_ex = {k: rx(v) for k, v in C["non_competency"]["exclusive"].items()}
        self.nc_soft = {k: rx(v) for k, v in C["non_competency"]["soft"].items()}
        self.action = rx(C["non_competency"]["action"])
        nl = rules.get(COMP + "non_lines.json")
        self.non_lines = {norm(x["text"]): x["type"] for x in nl["lines"]} if nl else {}
        self.levels = [(k, rx(v)) for k, v in C["levels"].items()]
        self.tools = ld(TOOLS + "tool_family.json")["tools"]
        self.fams = ld(TOOLS + "families.json")["families"]
        for c in self.comps:
            bad = [f for f in c.get("families", []) if f not in self.fams]
            if bad:
                raise RulesError(f"{c['id']}: unknown families {bad}")
        tech = {n["id"]: n for n in ld(SEED + "tech_seed.json")["nodes"]}
        self.tech = tech
        self.tpat: dict[str, re.Pattern[str]] = {}
        for i, n in tech.items():
            if i not in self.tools:
                continue
            surf = {n["label"].lower()} | {s.lower() for s in n.get("aliases") or []}
            alts = [re.escape(s) for s in sorted(surf, key=len, reverse=True) if s]
            self.tpat[i] = re.compile(
                r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9+#&])", re.I
            )

    @classmethod
    def from_rules(cls, rules: dict[str, Any]) -> Matcher:
        return cls(rules)

    def level(self, lo: str) -> str:
        return next((k for k, r in self.levels if r.search(lo)), "언급")

    def classify(self, line: str, sub: str) -> dict[str, Any]:
        lo = line.lower()
        ex_hit = [k for k, r in self.nc_ex.items() if r.search(lo)]
        if ex_hit and not self.action.search(lo):  # type: ignore[union-attr]
            return dict(kind="non", comps=[], non_type=ex_hit[0], level=None)
        found = [
            c["id"]
            for c in self.comps
            if in_scope(sub, c.get("subs"))
            and c["_obj"].search(lo)
            and (not c["_act"] or c["_act"].search(lo))
            and not (c["_not"] and c["_not"].search(lo))
        ]
        if found:
            return dict(kind="comp", comps=found, non_type=None, level=self.level(lo))
        soft = [k for k, r in self.nc_soft.items() if r.search(lo)] + ex_hit
        if soft:
            return dict(kind="non", comps=[], non_type=soft[0], level=None)
        if norm(line) in self.non_lines:
            return dict(kind="non", comps=[], non_type=self.non_lines[norm(line)], level=None)
        return dict(kind="unmatched", comps=[], non_type=None, level=None)

    def tool_ids(self, line: str) -> list[str]:
        return [i for i, p in self.tpat.items() if p.search(line)]

    def tool_families(self, line: str) -> set[str]:
        return {self.tools[i]["family"] for i in self.tool_ids(line)}
