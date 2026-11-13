"""손 시범 판정 — docs/design/analysis-stage.md §9.1 통과 기준·원인 가르기·판정 표를 기록에서 기계적으로 계산한다.

입력 (모두 git 밖):
- 채점 `data/eval/analysis-pilot/results/grade/<label>.json` (grade.py)
- 진행자 기록 `<RUNS>/facilitator/logs/<label>.jsonl`
- 풀이 기록 `data/eval/analysis-pilot/runs/<label>/transcript.jsonl` (서브에이전트 기록 사본)·`usage.json` (토큰·도구 호출 수)

계산 규칙 (§9.1):
- 통과율은 재료 안에서 테스트 메서드 단위 — ①·② 는 공개 테스트(저장 확인 제외), 숨은 테스트는 숨은 케이스.
- 조건은 재료별 — (라) 판 0 opus ①·② 100%, 관문 판 2 opus ② 평균 100% 면 그 재료는 판정에서 뺌, (나) 자격 = 판 0 ② − 판 2 ② > 0,
  (바) 기준점 = 판 0 ② 가 ⅔ 이상인 opus 밖 모델 가운데 가장 약한 것.
- 효과는 재료별로 구해 같은 무게로 평균 — (나) 판 1 − 판 2, (다) 판 1 opus − 판 3 opus 숨은 포착, (바) "판 1 차이 − 판 3 차이" (②·숨은 따로).
- (마) 적중률은 재료를 합쳐 질문 단위 (반복은 뺌).

  uv run python -m eval.pilot.analyze usage <label> --tokens N --tool-uses N [--ms N]
  uv run python -m eval.pilot.analyze report
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from pydantic import Field

from eval.pilot.facilitator import LOGS, Entry, read_log
from eval.pilot.grade import PERSISTENCE, Graded, SuiteRun
from eval.pilot.model import DATA, MODELS, Material, all_materials, legacy_limits, parse_label
from task_spec.schemas.base import Schema
from task_spec.starter.answer_rule import hit_rate

CEILING_FLOOR = 2 / 3  # (바) 기준점 하한
HIT_RATIO = 0.5  # (마) 판 4 ≤ 판 1 opus × 0.5
BUDGET_RATIO = 2  # 예산 = 판 0 opus 도구 호출의 2배
WEAKEST_FIRST = tuple(reversed(MODELS))  # haiku, sonnet, opus
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_WRITE_SHELL = re.compile(r">|\bsed\s+-i\b|\btee\b|<<|\bmv\b|\bcp\b")
FORBID = r"/ssd/make_task|data/eval|solo_runs/analysis-pilot/(?!ws/{label}\b|gradle-home\b)|/ws/(?!{label}\b)|eval/pilot|answer_rule|facilitator"

Verdict = Literal["통과", "결론 없음", "실패"]
Cause = Literal["막힘", "흔들림", "늦게 받음", "묻지 않음"]


# ── 기록 읽기 ────────────────────────────────────────────────────────────────


class Usage(Schema):
    tokens: int | None = None
    tool_uses: int | None = None
    ms: int | None = None


class RunData(Schema):
    label: str
    mid: str
    arm: int
    model: str
    run: int
    p1: float  # ① 공개 테스트 통과율
    p2: float  # ② 행동 단언 통과율
    hidden: float  # 숨은 케이스 통과율
    setup: list[str]
    made: dict[str, bool] = Field(default_factory=dict)  # r# → 만들었나 (② 메서드 기준)
    entries: list[Entry] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    edits: list[str] = Field(default_factory=list)  # src/main 을 고친 시각들
    forbidden: list[str] = Field(default_factory=list)


def count_tests(root: Path, prefix: str, skip: Sequence[str] = ()) -> int:
    """오버레이의 @Test 메서드 수 — 컴파일이 깨져도 분모가 줄지 않게."""
    n = 0
    for p in root.rglob("*.java"):
        rel = p.relative_to(root).as_posix()
        if prefix in rel and p.stem not in skip:
            n += len(re.findall(r"@Test\b", p.read_text("utf-8", errors="ignore")))
    return n


def rate(run: SuiteRun, group: str, expected: int) -> float:
    ms = [x for x in run.methods if x.group == group]
    denom = max(expected, len(ms))
    return sum(x.ok for x in ms) / denom if denom else 0.0


def pt_to_ac(m: Material) -> dict[str, str]:
    tests = m.contract().get("public_tests", [])
    return {str(t["test_id"]): str(t["ac_id"]) for t in tests} if isinstance(tests, list) else {}


def made_by_requirement(m: Material, run: SuiteRun) -> dict[str, bool]:
    """요구마다 만들었나 — 그 요구에 걸린 공개 수용 기준(req_ids)의 ② 메서드가 하나 이상 있고 모두 통과."""
    ac_of = pt_to_ac(m)
    by_ac: dict[str, list[bool]] = {}
    for x in run.methods:
        if x.group == "public" and x.case in ac_of:
            by_ac.setdefault(ac_of[x.case], []).append(x.ok)
    reqs = {r for rs in m.req_ids.values() for r in rs}
    out: dict[str, bool] = {}
    for r in sorted(reqs):
        acs = [p for p, rs in m.req_ids.items() if r in rs]
        results = [ok for p in acs for ok in by_ac.get(p, [])]
        out[r] = bool(results) and all(results)
    return out


def _ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def transcript_events(path: Path) -> list[tuple[str, str, dict[str, Any]]]:
    """서브에이전트 기록의 도구 호출 — (시각, 도구, 입력)."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    if not path.exists():
        return out
    for line in path.read_text("utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                out.append(
                    (
                        str(ev.get("timestamp", "")),
                        str(c.get("name", "")),
                        dict(c.get("input") or {}),
                    )
                )
    return out


def edit_times(events: Iterable[tuple[str, str, dict[str, Any]]]) -> list[str]:
    """구현 코드(src/main)를 고친 시각 — 편집 도구, 또는 쓰기 꼴 셸 명령."""
    out = []
    for t, name, inp in events:
        if name in EDIT_TOOLS and "/src/main/" in str(inp.get("file_path", "")):
            out.append(t)
        elif (
            name == "Bash"
            and "src/main" in str(inp.get("command", ""))
            and _WRITE_SHELL.search(str(inp.get("command", "")))
        ):
            out.append(t)
    return out


def forbidden_access(label: str, events: Iterable[tuple[str, str, dict[str, Any]]]) -> list[str]:
    rx = re.compile(FORBID.replace("{label}", re.escape(label)))
    hits = []
    for _, name, inp in events:
        s = json.dumps({k: v for k, v in inp.items() if k != "description"}, ensure_ascii=False)
        found = rx.search(s)
        if found:
            hits.append(f"{name}: …{s[max(0, found.start() - 40) : found.end() + 40]}…")
    return hits


def load_run(m: Material, label: str) -> RunData | None:
    gp = DATA / "results" / "grade" / f"{label}.json"
    if not gp.exists():
        return None
    g = Graded.model_validate_json(gp.read_text("utf-8"))
    _, arm, model, run = parse_label(label)
    pub1 = count_tests(
        m.alpha_dir / "overlays" / "public", "/publictests/", (PERSISTENCE, "PublicCase")
    )
    pub2 = count_tests(m.dir / "public-behavior", "/publictests/", (PERSISTENCE, "PublicCase"))
    hid = count_tests(m.alpha_dir / "overlays" / "hidden", "/grading/cases/", ("ContractCase",))
    rd = DATA / "runs" / label
    events = transcript_events(rd / "transcript.jsonl")
    up = rd / "usage.json"
    return RunData(
        label=label,
        mid=m.mid,
        arm=arm,
        model=model,
        run=run,
        p1=rate(g.a, "public", pub1),
        p2=rate(g.b, "public", pub2),
        hidden=rate(g.a, "hidden", hid),
        setup=[g.a.setup, g.b.setup],
        made=made_by_requirement(m, g.b),
        entries=read_log(label, LOGS),
        usage=Usage.model_validate_json(up.read_text("utf-8")) if up.exists() else Usage(),
        edits=edit_times(events),
        forbidden=forbidden_access(label, events),
    )


# ── 판정 (순수 함수) ─────────────────────────────────────────────────────────


def avg(runs: Sequence[RunData], arm: int, model: str, key: str) -> float | None:
    vals = [getattr(r, key) for r in runs if r.arm == arm and r.model == model]
    return mean(vals) if vals else None


class MaterialState(Schema):
    mid: str
    ra: bool | None  # (라) 판 0 opus ①·② 100%
    gate_stop: bool | None  # 관문 — 판 2 opus ② 평균 100%
    eligible: list[str] = Field(default_factory=list)  # (나) 자격 모델
    reference: str | None = None  # (바) 기준점 모델
    info_value: dict[str, float] = Field(default_factory=dict)  # 모델별 판 0 ② − 판 2 ② (진단)


def material_state(mid: str, runs: Sequence[RunData]) -> MaterialState:
    p0 = {md: avg(runs, 0, md, "p2") for md in MODELS}
    p0_1 = avg(runs, 0, "opus", "p1")
    p2 = {md: avg(runs, 2, md, "p2") for md in MODELS}
    ra = None if p0["opus"] is None or p0_1 is None else (p0["opus"] >= 1.0 and p0_1 >= 1.0)
    gate = None if p2["opus"] is None else p2["opus"] >= 1.0
    info = {md: p0[md] - p2[md] for md in MODELS if p0[md] is not None and p2[md] is not None}  # type: ignore[operator]
    eligible = [md for md in MODELS if info.get(md, 0.0) > 0]
    ref = next(
        (md for md in WEAKEST_FIRST if md != "opus" and (p0[md] or 0.0) >= CEILING_FLOOR), None
    )
    return MaterialState(
        mid=mid, ra=ra, gate_stop=gate, eligible=eligible, reference=ref, info_value=info
    )


def verdict_na(
    by_mid: dict[str, list[RunData]], states: dict[str, MaterialState]
) -> tuple[Verdict, dict[str, float]]:
    """(나) 모델마다 자격 있는 재료에서만 판 1 − 판 2 (②) 를 구해 평균. 통과: opus > 0 이고 나머지 자격 모델 ≥ 0, 실패: opus < 0."""
    per: dict[str, list[float]] = {}
    for mid, runs in by_mid.items():
        st = states[mid]
        if st.gate_stop:
            continue
        for md in st.eligible:
            a1, a2 = avg(runs, 1, md, "p2"), avg(runs, 2, md, "p2")
            if a1 is not None and a2 is not None:
                per.setdefault(md, []).append(a1 - a2)
    eff = {md: mean(v) for md, v in per.items()}
    if "opus" not in eff:
        return "결론 없음", eff
    if eff["opus"] < 0:
        return "실패", eff
    if eff["opus"] > 0 and all(v >= 0 for md, v in eff.items() if md != "opus"):
        return "통과", eff
    return "결론 없음", eff


def verdict_da(
    by_mid: dict[str, list[RunData]], states: dict[str, MaterialState]
) -> tuple[Verdict, float | None]:
    """(다) 재료별 판 1 opus − 판 3 opus 숨은 포착을 평균 — ≤ 0 이면 통과 (조치형: 깨지면 원인 가르기)."""
    ds = []
    for mid, runs in by_mid.items():
        if states[mid].gate_stop:
            continue
        a1, a3 = avg(runs, 1, "opus", "hidden"), avg(runs, 3, "opus", "hidden")
        if a1 is not None and a3 is not None:
            ds.append(a1 - a3)
    if not ds:
        return "결론 없음", None
    d = mean(ds)
    return ("통과" if d <= 0 else "실패"), d


def verdict_ba(
    by_mid: dict[str, list[RunData]], states: dict[str, MaterialState]
) -> tuple[Verdict, str, dict[str, float]]:
    """(바) 재료별 기준점으로 "판 1 차이 − 판 3 차이"(차이 = opus − 기준점)를 ②·숨은 따로 구해 평균.
    통과: ② > 0 이고 숨은 ≥ 0, 실패: ② < 0, 그 밖 결론 없음(표본 작음). 기준점 있는 재료가 없으면 결론 없음(기준점 없음)."""
    d2: list[float] = []
    dh: list[float] = []
    for mid, runs in by_mid.items():
        st = states[mid]
        if st.gate_stop or st.reference is None:
            continue
        ref = st.reference
        vals = {
            (arm, md, key): avg(runs, arm, md, key)
            for arm in (1, 3)
            for md in ("opus", ref)
            for key in ("p2", "hidden")
        }
        if any(v is None for v in vals.values()):
            continue
        for key, sink in (("p2", d2), ("hidden", dh)):
            gap1 = vals[(1, "opus", key)] - vals[(1, ref, key)]  # type: ignore[operator]
            gap3 = vals[(3, "opus", key)] - vals[(3, ref, key)]  # type: ignore[operator]
            sink.append(gap1 - gap3)
    if not d2:
        return "결론 없음", "기준점 없음", {}
    eff = {"p2": mean(d2), "hidden": mean(dh)}
    if eff["p2"] < 0:
        return "실패", "", eff
    if eff["p2"] > 0 and eff["hidden"] >= 0:
        return "통과", "", eff
    return "결론 없음", "표본 작음", eff


def verdict_ma(all_runs: Sequence[RunData]) -> tuple[Verdict, float | None, float | None]:
    """(마) 판 4 적중률 ≤ 판 1 opus 적중률 × 0.5 — 재료를 합쳐 질문 단위."""

    def replies(runs: Iterable[RunData]) -> list[Any]:
        return [
            e.reply for r in runs for e in r.entries if e.kind == "question" and e.reply is not None
        ]

    h4 = hit_rate(replies(r for r in all_runs if r.arm == 4))
    h1 = hit_rate(replies(r for r in all_runs if r.arm == 1 and r.model == "opus"))
    if h4 is None or h1 is None:
        return "결론 없음", h4, h1
    return ("통과" if h4 <= h1 * HIT_RATIO else "실패"), h4, h1


# ── 원인 가르기 ─────────────────────────────────────────────────────────────


def covering_facts(m: Material, req: str) -> list[str]:
    acs = {p for p, rs in m.req_ids.items() if req in rs}
    return [f.fact_id for f in m.facts if req in f.covers or acs & set(f.covers)]


def miss_cause(m: Material, r: RunData, req: str) -> tuple[Cause, str | None]:
    """판 1 풀이가 놓친 요구 하나의 원인 (§9.1 원인 표). 두 번째 값 = 사실을 연 시각 (열렸을 때)."""
    facts = set(covering_facts(m, req))
    opened_at, asked = None, False
    for e in r.entries:
        if e.kind != "question" or e.reply is None:
            continue
        rp = e.reply
        if rp.item_id in facts and rp.states.get(rp.item_id) == "open" and opened_at is None:
            opened_at = e.t
        if facts & set(rp.candidates):
            asked = True
    if opened_at is not None:
        t0 = _ts(opened_at)
        later = [t for t in r.edits if (tt := _ts(t)) is not None and t0 is not None and tt > t0]
        return ("흔들림" if later else "늦게 받음"), opened_at
    return ("막힘" if asked else "묻지 않음"), None


def causes(m: Material, runs: Sequence[RunData]) -> list[dict[str, Any]]:
    """판 1 풀이마다 놓친 요구의 원인. "늦게 받음"은 같은 모델의 두 풀이 모두에 있을 때만 — 한 번뿐이면 흔들림."""
    rows = []
    for r in runs:
        if r.arm != 1:
            continue
        for req, ok in r.made.items():
            if not ok:
                cause, t = miss_cause(m, r, req)
                rows.append(
                    {
                        "label": r.label,
                        "model": r.model,
                        "run": r.run,
                        "req": req,
                        "cause": cause,
                        "opened_at": t,
                    }
                )
    for md in MODELS:
        late_runs = {x["run"] for x in rows if x["model"] == md and x["cause"] == "늦게 받음"}
        all_runs = {r.run for r in runs if r.arm == 1 and r.model == md}
        if late_runs != all_runs or len(all_runs) < 2:
            for x in rows:
                if x["model"] == md and x["cause"] == "늦게 받음":
                    x["cause"] = "흔들림"
    return rows


def next_cause_step(rows: Sequence[dict[str, Any]]) -> str | None:
    """원인이 섞이면 막힘 → 흔들림 → 묻지 않음·늦게 받음 순서."""
    have = {x["cause"] for x in rows}
    for step, name in (
        ("막힘", "진행자 쪽 고치기 (한 번) → 판 1·4 다시"),
        ("흔들림", "자격 모델 전부로 판 1·2 한 번씩 더 → 세 번 평균"),
        ("묻지 않음", "보류 → 사람 시범 응시"),
        ("늦게 받음", "보류 → 사람 시범 응시"),
    ):
        if step in have:
            return f"{step}: {name}"
    return None


def no_info_questions(runs: Sequence[RunData]) -> list[dict[str, str]]:
    """정보 없음을 받은 질문 — 목록 밖 표현으로 막혔는지는 사람이 본다 (판단 필요)."""
    return [
        {"label": r.label, "t": e.t, "question": e.arg}
        for r in runs
        if r.arm in (1, 4)
        for e in r.entries
        if e.kind == "question" and e.reply is not None and e.reply.rule == 5
    ]


def limit_replay(m: Material, r: RunData) -> str:
    """(다) 원인 — 판 1 기록에 판 3 한도를 다시 적용해 함정 단서가 남는지. 남아 있음 / 질문 쪽에서 잘림 / 자료 쪽에서 잘림."""
    task = m.task()
    max_q, max_r = legacy_limits(task)
    traps = {t.trap_id for t in task.traps}
    clue_c = {c.clarification_id for c in task.clarifications if traps & set(c.reveals)}
    key_a = {a.artifact_id for a in task.artifacts if a.role == "key"}
    qs = [
        e
        for e in r.entries
        if e.kind == "question"
        and e.reply is not None
        and not str(e.reply.item_id or "").startswith("f")
    ]
    kept_q = qs[: max_q or len(qs)]
    served = list(dict.fromkeys(e.served for e in r.entries if e.kind == "material" and e.served))
    kept_a = served[: max_r or len(served)]

    def got_c(es: Iterable[Entry]) -> bool:
        return any(
            e.reply is not None and e.reply.item_id in clue_c and e.reply.layer == "answer"
            for e in es
        )

    full_c, full_a = got_c(qs), bool(key_a & set(served))
    lim_c, lim_a = got_c(kept_q), bool(key_a & set(kept_a))
    if (full_c or full_a) and not (lim_c or lim_a):
        cut = [name for name, full in (("질문", full_c), ("자료", full_a)) if full]
        return "·".join(cut) + " 쪽에서 잘림"
    return "남아 있음"


# ── 보고 ──────────────────────────────────────────────────────────────────


def budget_flags(runs: Sequence[RunData]) -> list[str]:
    base = next(
        (r.usage.tool_uses for r in runs if r.arm == 0 and r.model == "opus" and r.usage.tool_uses),
        None,
    )
    if base is None:
        return []
    limit = BUDGET_RATIO * base
    return [
        f"{r.label}: 도구 호출 {r.usage.tool_uses} > 예산 {limit}"
        for r in runs
        if r.arm != 0 and (r.usage.tool_uses or 0) > limit
    ]


def report() -> dict[str, Any]:
    by_mid: dict[str, list[RunData]] = {}
    mats = {m.mid: m for m in all_materials()}
    for p in sorted((DATA / "results" / "grade").glob("*.json")):
        mid = parse_label(p.stem)[0]
        if mid in mats and (rd := load_run(mats[mid], p.stem)) is not None:
            by_mid.setdefault(mid, []).append(rd)
    states = {mid: material_state(mid, runs) for mid, runs in by_mid.items()}
    na, na_eff = verdict_na(by_mid, states)
    da, da_eff = verdict_da(by_mid, states)
    ba, ba_why, ba_eff = verdict_ba(by_mid, states)
    all_runs = [r for runs in by_mid.values() for r in runs]
    ma, h4, h1 = verdict_ma(all_runs)
    cause_rows = [x for mid, runs in by_mid.items() for x in causes(mats[mid], runs)]
    out: dict[str, Any] = {
        "materials": {mid: s.model_dump() for mid, s in states.items()},
        "runs": [
            {k: getattr(r, k) for k in ("label", "p1", "p2", "hidden", "setup")}
            | {"usage": r.usage.model_dump(), "forbidden": len(r.forbidden)}
            for r in all_runs
        ],
        "na": {"verdict": na, "effect": na_eff},
        "da": {
            "verdict": da,
            "effect": da_eff,
            "replay": {
                r.label: limit_replay(mats[r.mid], r)
                for r in all_runs
                if r.arm == 1 and r.model == "opus"
            }
            if da == "실패"
            else {},
        },
        "ba": {"verdict": ba, "why": ba_why, "effect": ba_eff},
        "ma": {"verdict": ma, "hit_rate_p4": h4, "hit_rate_p1_opus": h1},
        "ga": {mid: {md: avg(runs, 1, md, "p2") for md in MODELS} for mid, runs in by_mid.items()},
        "causes": cause_rows,
        "next_cause_step": next_cause_step(cause_rows) if na != "통과" else None,
        "needs_human": no_info_questions(all_runs),
        "budget": [f for runs in by_mid.values() for f in budget_flags(runs)],
        "forbidden": {r.label: r.forbidden[:3] for r in all_runs if r.forbidden},
    }
    res = DATA / "results"
    res.mkdir(parents=True, exist_ok=True)
    (res / "analysis.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str), "utf-8"
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("usage", help="서브에이전트 사용량 기록 (작업 알림의 usage)")
    u.add_argument("label")
    u.add_argument("--tokens", type=int, required=True)
    u.add_argument("--tool-uses", type=int, required=True)
    u.add_argument("--ms", type=int, default=None)
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "usage":
        parse_label(a.label)
        p = DATA / "runs" / a.label / "usage.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            Usage(tokens=a.tokens, tool_uses=a.tool_uses, ms=a.ms).model_dump_json(), "utf-8"
        )
        return
    out = report()
    print(
        json.dumps(
            {k: out[k] for k in ("materials", "na", "da", "ba", "ma", "next_cause_step", "budget")},
            ensure_ascii=False,
            indent=1,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
