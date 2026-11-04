"""대조 겹침 정답의 두 번째 판정자를 사람·에이전트(파일)로 — 판정 패킷을 쓰고, 답 파일을 읽어 검사한다.

왜: 합의 정답의 두 번째 판정을 API 모델 대신 다른 계열 판정자(예: 새로 띄운 Claude 에이전트)에게 맡기면 비용 없이
판정 잡음을 줄일 수 있다 (2026-11-04 held-out 가상 이력서 평가, 옵션 A). 두 판정자가 **같은 입력**을 보도록
패킷은 run_overlap 이 gpt-4.1 에 보낸 것과 같은 프롬프트(judge_overlap CURRENT, 가명화본 + 역량 "id — 이름")를
그대로 옮긴다. 평가 의도(작성 의도·own/other·split)는 넣지 않는다.
입력: run_overlap 의 report.json (행마다 pseudo_doc·judge_asked 저장). 가명화본만 쓴다 (불변식 2).
출력: <out>/packets/<이력서>.md, <out>/README.md. 답은 <out>/out/<이력서>.json (JudgeOverlapOutput JSON) —
     run_judge_consensus --second-dir <out>/out 이 읽는다. 전부 git 밖 data/.
사용: uv run python -m eval.retrieve.judge_packets --report data/eval/overlap/<run>/report.json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from task_spec.nodes.pseudonymize import load_pseudonymized
from task_spec.prompting import AssembledPrompt, PromptRegistry
from task_spec.schemas import JudgeOverlapOutput, PseudoDocRef

ROOT = Path(__file__).resolve().parents[2]
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def render_packet(resume: str, prompt: AssembledPrompt, answer_path: str, n_items: int) -> str:
    system, user = (m.content for m in prompt.messages)
    return (
        f"# 겹침 판정 — {resume}\n\n"
        f"- 답 파일: `{answer_path}`\n"
        "- 답은 아래 '지시' 끝의 JSON 스키마를 따르는 JSON 객체 하나만 씁니다 (설명·코드 블록 표시 없이).\n"
        f"- 역량 목록의 {n_items}개 모두에 competency_id 를 그대로 써서 한 번씩 답합니다.\n"
        "- 이 파일과 이력서 본문 말고 다른 자료(다른 판정·평가 파일)는 보지 않습니다.\n\n"
        f"## 지시\n\n{system}\n\n## 입력\n\n{user}\n"
    )


def read_judge_dir(
    out_dir: Path, asked: dict[str, dict[str, str]]
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], dict[str, dict[str, Any]]]:
    """답 파일들 → (이력서 → 역량 → 라벨, 이력서 → 역량 → 근거, 이력서 → 검사).

    목록 밖 id 는 버리고, 같은 id 가 두 번이면 첫 답을 쓴다. 파일이 없거나 스키마가 깨지면 그 이력서는 빼고 검사에 남긴다.
    """
    labels: dict[str, dict[str, str]] = {}
    evidence: dict[str, dict[str, str]] = {}
    checks: dict[str, dict[str, Any]] = {}
    for resume, ids in sorted(asked.items()):
        f = out_dir / f"{resume}.json"
        if not f.is_file():
            checks[resume] = {"error": "답 파일 없음"}
            continue
        try:
            parsed = JudgeOverlapOutput.model_validate_json(_FENCE.sub("", f.read_text("utf-8")))
        except ValidationError as exc:
            checks[resume] = {"error": f"스키마 위반 {exc.error_count()}건"}
            continue
        answered = [x.competency_id for x in parsed.items]
        lab: dict[str, str] = {}
        ev: dict[str, str] = {}
        for x in parsed.items:
            if x.competency_id in ids and x.competency_id not in lab:
                lab[x.competency_id] = x.label
                ev[x.competency_id] = x.evidence
        labels[resume], evidence[resume] = lab, ev
        checks[resume] = {
            "asked": len(ids),
            "answered": len(lab),
            "duplicates": len(answered) - len(set(answered)),
            "unknown_ids": sorted(set(answered) - set(ids)),
            "missing_ids": sorted(set(ids) - set(lab)),
        }
    return labels, evidence, checks


def main() -> None:
    from eval.retrieve.run_overlap import judge_prompt

    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None, help="기본 <report 폴더>/claude_judge")
    a = ap.parse_args()
    rep = json.loads(a.report.read_text("utf-8"))
    out = a.out or a.report.parent / "claude_judge"
    (out / "packets").mkdir(parents=True, exist_ok=True)
    (out / "out").mkdir(parents=True, exist_ok=True)
    prompts = PromptRegistry(ROOT / "prompts")
    first: dict[str, dict[str, Any]] = {}
    for r in rep["rows"]:
        if "pseudo_doc" in r:
            first.setdefault(r["resume"], r)
    listing = []
    for resume, r in sorted(first.items()):
        text = load_pseudonymized(PseudoDocRef.model_validate(r["pseudo_doc"]))
        asked: dict[str, str] = r["judge_asked"]
        prompt = judge_prompt(prompts, text, asked, "eval")
        answer = out / "out" / f"{resume}.json"
        packet = out / "packets" / f"{resume}.md"
        packet.write_text(render_packet(resume, prompt, str(answer), len(asked)), "utf-8")
        listing.append(f"- {packet} → {answer} ({len(asked)}개 역량)")
    readme = (
        "# 겹침 판정 패킷 (두 번째 판정자용)\n\n"
        f"판정 프롬프트: {prompts.get('judge_overlap').ref} — gpt-4.1 판정과 같은 입력.\n"
        '답 형식: JudgeOverlapOutput — `{"items": [{"competency_id": ..., "label": '
        '"action|related|mention|none", "evidence": "이력서 문장 그대로 120자 이내, none 이면 빈 문자열"}]}`\n\n'
        + "\n".join(listing)
        + "\n"
    )
    (out / "README.md").write_text(readme, "utf-8")
    print(f"패킷 {len(listing)}개 → {out}")


if __name__ == "__main__":
    main()
