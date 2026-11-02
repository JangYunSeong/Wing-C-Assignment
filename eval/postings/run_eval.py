"""새 공고(경로 2) 평가 — 온톨로지 공고를 '새 공고처럼' 넣고 검수 결과와 비교.

입력: 온톨로지 레포 클론의 공고 원본(data/ontology/src/data/postings/parsed/*.json, git 밖) — 시범 직군 239 포지션.
원문 재구성: 절 제목 표기 4종(마크다운 제목 / [대괄호] / '라벨:' 번호 목록 / 제목 없음 15%) — 시드 고정.
정답: 절 = 원본 절별 문장, 직군·업종 = position, 역량 = line_competency (검수 결과).

지표
- section_acc       원본 문장 중 같은 절로 분리된 비율 (재구성 표기라 낙관적)
- sub_top1/top3     직군 추천 (직무명 비교에서 평가 대상 자신 제외 — 정답 누설 방지)
- industry_known    등록 회사 → 업종
- industry_unknown  회사를 모르는 척(빈 회사명) → LLM 업종 (--unknown-sample 건만, 비용)
- comp_*_gold       정답 직군으로 매칭한 역량 집합 vs 검수 결과 (절 분리 + 매처)
- comp_*_top1       추천 1위 직군으로 매칭 (끝에서 끝)

사용: uv run python -m eval.postings.run_eval --split dev --llm --unknown-sample 20 --show-misses
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import hashlib
import json
import random
from collections import Counter
from datetime import date
from pathlib import Path

from task_spec.config import Settings
from task_spec.llm.factory import build_llm_client
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.sections import segment
from task_spec.postings.suggest import analyze, load_lists, suggest
from task_spec.prompting import PromptRegistry

HERE = Path(__file__).parent
PARSED = HERE.parents[1] / "data/ontology/src/data/postings/parsed"
PROMPTS = PromptRegistry(HERE.parents[1] / "prompts")
SECTIONS = ("duties", "required", "preferred")
STYLES = {
    "markdown": {
        "duties": "### 담당업무",
        "required": "### 자격요건",
        "preferred": "### 우대사항",
        "bullet": "- ",
    },
    "bracket": {
        "duties": "[주요 업무]",
        "required": "[지원 자격]",
        "preferred": "[우대 사항]",
        "bullet": "• ",
    },
    "colon": {
        "duties": "업무내용:",
        "required": "지원자격:",
        "preferred": "우대사항:",
        "bullet": "{n}. ",
    },
}


def _split_of(pid: str) -> str:
    return "dev" if int(hashlib.sha1(pid.encode()).hexdigest(), 16) % 2 == 0 else "test"


def load_records(split: str, pilot_ids: set[str]) -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(PARSED / "*.json"))):
        if f.endswith(".proposals.json"):
            continue
        j = json.load(open(f, encoding="utf-8"))
        for r in j if isinstance(j, list) else (j.get("positions") or j.get("records") or []):
            if r["id"] in pilot_ids and _split_of(r["id"]) == split and r.get("duties"):
                out.append(r)
    return out


def render(rec: dict, rnd: random.Random) -> tuple[str, str]:
    style = "none" if rnd.random() < 0.15 else rnd.choice(list(STYLES))
    parts = [f"{rec.get('company') or ''} {rec.get('position') or rec.get('job') or ''} 채용", ""]
    for sec in SECTIONS:
        items = rec.get(sec) or []
        if not items:
            continue
        if style == "none":
            parts += items
        else:
            st = STYLES[style]
            parts.append(st[sec])
            parts += [st["bullet"].format(n=i) + t for i, t in enumerate(items, 1)]
        parts.append("")
    return "\n".join(parts), style


def _norm(t: str) -> str:
    return " ".join(t.split()).casefold()


async def evaluate(
    split: str,
    use_llm: bool,
    unknown_sample: int,
    limit: int | None,
    suggest_version: int | None = None,
) -> dict:
    s = Settings()  # type: ignore[call-arg]
    repo = await OntologyRepository.open(s.database_url.get_secret_value(), s.ontology_version)
    llm = build_llm_client(s) if use_llm else None
    try:
        lists = await load_lists(repo)
        pos = {
            r["id"]: r
            for r in await repo.fetch(
                "SELECT id, company, job, position_name, sub, industry_id, in_pilot FROM position"
            )
        }
        gold_comps: dict[str, set[str]] = {}
        for r in await repo.fetch(
            "SELECT l.position_id, lc.competency_id FROM posting_line l JOIN line_competency lc ON lc.line_id = l.id"
        ):
            gold_comps.setdefault(r["position_id"], set()).add(r["competency_id"])
        recs = load_records(split, {i for i, p in pos.items() if p["in_pilot"]})
        if limit:
            recs = recs[:limit]
        rnd = random.Random({"dev": 41, "test": 42}[split])
        unknown_ids = (
            {r["id"] for r in rnd.sample(recs, min(unknown_sample, len(recs)))}
            if use_llm
            else set()
        )

        c: Counter[str] = Counter()
        style_n: Counter[str] = Counter()
        misses: list[dict] = []
        for rec in recs:
            pid = rec["id"]
            gold = pos[pid]
            text, style = render(rec, rnd)
            style_n[style] += 1
            seg = await segment(text, llm, PROMPTS if llm else None)
            c[f"seg_{seg.method}"] += 1
            got = {(_norm(x.text), x.section) for x in seg.lines}
            for sec in SECTIONS:
                for t in rec.get(sec) or []:
                    c["sec_total"] += 1
                    c["sec_ok"] += (_norm(t), sec) in got

            title = rec.get("job") or rec.get("position") or ""
            subs, ind, meta = await suggest(
                lists,
                company=gold["company"],
                title=title,
                text=text,
                lines=seg.lines,
                llm=llm,
                prompts=PROMPTS if llm else None,
                exclude_positions=frozenset({pid}),
                prompt_version=suggest_version,
            )
            ids = [x.id for x in subs]
            c["sug_llm"] += bool(meta["llm_used"])
            c["n"] += 1
            c["sub_top1"] += bool(ids) and ids[0] == gold["sub"]
            c["sub_top3"] += gold["sub"] in ids
            c["ind_known"] += bool(ind) and ind[0].id == gold["industry_id"]
            if ids and ids[0] != gold["sub"] and len(misses) < 40:
                misses.append(
                    {"id": pid, "title": title[:30], "gold": gold["sub"], "pred": ids[:3]}
                )

            if pid in unknown_ids:
                _, ind_u, meta_u = await suggest(
                    lists,
                    company="",
                    title=title,
                    text=text,
                    lines=seg.lines,
                    llm=llm,
                    prompts=PROMPTS,
                    exclude_positions=frozenset({pid}),
                    prompt_version=suggest_version,
                )
                c["unk_n"] += 1
                c["unk_ok"] += bool(ind_u) and ind_u[0].id == gold["industry_id"]
                c["llm_err"] += "llm_error" in meta_u
                c["unk_top3"] += gold["industry_id"] in [x.id for x in ind_u[:3]]

            want = gold_comps.get(pid, set())
            for tag, sub in (("gold", gold["sub"]), ("top1", ids[0] if ids else gold["sub"])):
                found = {
                    x for line in analyze(lists, seg.lines, sub, id_prefix=pid) for x in line.comps
                }
                c[f"tp_{tag}"] += len(found & want)
                c[f"fp_{tag}"] += len(found - want)
                c[f"fn_{tag}"] += len(want - found)
    finally:
        await repo.close()

    def r(a: str, b: str) -> float | None:
        return round(c[a] / c[b], 3) if c[b] else None

    def pr(tag: str) -> tuple[float | None, float | None]:
        tp, fp, fn = c[f"tp_{tag}"], c[f"fp_{tag}"], c[f"fn_{tag}"]
        return (
            round(tp / (tp + fn), 3) if tp + fn else None,
            round(tp / (tp + fp), 3) if tp + fp else None,
        )

    rg, pg = pr("gold")
    rt, pt = pr("top1")
    return {
        "split": split,
        "suggest_prompt": suggest_version,
        "positions": c["n"],
        "styles": dict(style_n),
        "segment_methods": {k[4:]: v for k, v in c.items() if k.startswith("seg_")},
        "suggest_llm_used": c["sug_llm"],
        "section_acc": r("sec_ok", "sec_total"),
        "sub_top1": r("sub_top1", "n"),
        "sub_top3": r("sub_top3", "n"),
        "industry_known": r("ind_known", "n"),
        "industry_unknown_llm": r("unk_ok", "unk_n"),
        "industry_unknown_top3": r("unk_top3", "unk_n"),
        "industry_unknown_n": c["unk_n"],
        "suggest_llm_errors": c["llm_err"],
        "comp_recall_gold": rg,
        "comp_precision_gold": pg,
        "comp_recall_top1": rt,
        "comp_precision_top1": pt,
        "misses": misses,
    }


def render_md(res: dict, show: bool) -> str:
    keys = [
        "positions",
        "styles",
        "segment_methods",
        "section_acc",
        "sub_top1",
        "sub_top3",
        "industry_known",
        "industry_unknown_llm",
        "industry_unknown_top3",
        "industry_unknown_n",
        "suggest_llm_errors",
        "comp_recall_gold",
        "comp_precision_gold",
        "comp_recall_top1",
        "comp_precision_top1",
    ]
    lines = [
        f"# 새 공고 평가 — {res['split']} ({date.today().isoformat()})",
        "",
        "| 지표 | 값 |",
        "|---|---|",
    ]
    lines += [f"| {k} | {res[k]} |" for k in keys]
    if show:
        lines += ["", "## 직군 1위 오답 (dev)", ""] + [f"- {m}" for m in res["misses"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], required=True)
    ap.add_argument(
        "--llm", action="store_true", help="제목 없는 공고의 절 분리·모르는 회사 업종에 LLM (비용)"
    )
    ap.add_argument("--unknown-sample", type=int, default=0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--suggest-version",
        type=int,
        default=None,
        help="suggest_field 프롬프트 버전 (기본 CURRENT)",
    )
    ap.add_argument("--show-misses", action="store_true")
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    if a.split == "test" and a.show_misses:
        raise SystemExit("test 세트의 오답 사례는 보지 않는다 (숨겨 둔 세트 유지)")
    res = asyncio.run(evaluate(a.split, a.llm, a.unknown_sample, a.limit, a.suggest_version))
    md = render_md(res, a.show_misses)
    print(md)
    if a.save:
        out = HERE / "reports"
        out.mkdir(exist_ok=True)
        tag = f"suggest-v{a.suggest_version}" if a.suggest_version else "suggest-current"
        stem = out / f"{date.today().isoformat()}_{a.split}_{tag}"
        stem.with_suffix(".md").write_text(md, encoding="utf-8")
        if a.split == "test":
            res.pop("misses")
        stem.with_suffix(".json").write_text(
            json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
