"""LLM 이 쓴 가상 지원 문서 — 템플릿이 못 만드는 문체 다양성을 넣는다.

심을 값(planted)은 코드가 정하고, LLM 에게 '글자 그대로' 넣게 한다. 실제로 본문에 들어간 값만 정답으로 남긴다.
입력이 전부 가상이라 외부 API 로 보내도 불변식 2 와 무관하다 (지원자 데이터 아님).

비결정적이므로 결과 JSONL 을 커밋해 고정한다. 다시 만들 때는 새 파일 이름으로.
사용: uv run python -m eval.pseudonymize.generate_llm --n 20 --seed 11 --split dev
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from dataclasses import asdict
from pathlib import Path

import yaml

from eval.pseudonymize.generate_template import (
    BARE_INVENTED_COMPANIES,
    CITIES,
    COURSES,
    DICT_COMPANIES,
    DICT_SCHOOLS,
    GIVEN,
    INVENTED_COMPANIES,
    INVENTED_SCHOOLS,
    PRODUCTS,
    ROLES,
    SURNAMES,
    TECH,
    TITLES,
    Sample,
)
from task_spec.config import Settings
from task_spec.llm.openai_compat import OpenAICompatBackend
from task_spec.prompting import Message

HERE = Path(__file__).parent
STYLES = [
    "줄글 위주의 경력기술서 (프로젝트별 문제 정의 / 과정 / 기여한 점)",
    "자기소개서 3문항 답변 (지원 동기, 협업 경험, 입사 후 포부)",
    "한글·영문이 섞인 1페이지 이력서 (머리줄에 인적사항을 한 줄로)",
    "링크드인 프로필 요약처럼 쓴 소개글",
    "인사 담당자에게 보내는 이메일 형식의 지원서",
    "표 대신 들여쓰기와 기호(▶, ㆍ)로 정리한 이력서",
]


def _load_prompt() -> tuple[dict, str, str]:
    raw = (HERE / "prompts" / "generate_v1.md").read_text(encoding="utf-8")
    _, fm, body = raw.split("---\n", 2)
    meta = yaml.safe_load(fm)
    system, user = re.split(r"^## user\s*$", body.split("## system", 1)[1], flags=re.M)
    return meta, system.strip(), user.strip()


def _values(r: random.Random, idx: int) -> tuple[list[tuple[str, str]], list[str], dict[str, str]]:
    name = r.choice(SURNAMES) + r.choice(GIVEN)
    colleague = r.choice(SURNAMES) + r.choice(GIVEN)
    company = r.choice(
        DICT_COMPANIES + [c[1] for c in INVENTED_COMPANIES] + BARE_INVENTED_COMPANIES
    )
    city, gu = r.choice(CITIES)
    title = r.choice(TITLES)
    planted = [
        ("PERSON", name),
        ("PERSON", colleague),
        ("CONTACT", f"010-0000-{r.randint(0, 9999):04d}"),
        ("CONTACT", f"applicant{idx}@example.com"),
        ("CONTACT", f"{city} {gu} 가상로 {r.randint(1, 200)}"),
        ("LINK", f"github.com/applicant-{idx}"),
        ("SCHOOL", r.choice(DICT_SCHOOLS + INVENTED_SCHOOLS)),
        ("ORG", company),
        ("COURSE", r.choice(COURSES)),
        ("PRODUCT", r.choice(PRODUCTS)),
        ("REMOVE", f"{r.randint(1993, 2001)}년생"),
    ]
    hard = [company] if company in BARE_INVENTED_COMPANIES else []
    ctx = {
        "kind": r.choice(["이력서", "자기소개서"]),
        "style": r.choice(STYLES),
        "role": r.choice(ROLES),
        "tech": ", ".join(r.sample(TECH, 3)),
        "colleague_hint": f"{colleague} {title}",
    }
    return planted, hard, ctx


async def _one(
    backend: OpenAICompatBackend,
    meta: dict,
    system: str,
    user_tpl: str,
    idx: int,
    seed: int,
    split: str,
) -> Sample | None:
    r = random.Random(seed * 1000 + idx)
    planted, hard, ctx = _values(r, idx)
    values = (
        "\n".join(f"- {v}" for _, v in planted) + f"\n- (동료를 부를 때) {ctx['colleague_hint']}"
    )
    user = user_tpl
    for k, v in {**ctx, "values": values}.items():
        user = user.replace("{{" + k + "}}", v)
    provider, _, model = meta["model"].partition(":")
    comp = await backend.complete(
        model,
        [Message(role="system", content=system), Message(role="user", content=user)],
        meta["params"],
    )
    try:
        text = json.loads(re.sub(r"^```(?:json)?|```$", "", comp.content.strip()))["text"]
    except (ValueError, KeyError, TypeError):
        print(f"[{idx}] JSON 파싱 실패 — 건너뜀")
        return None
    present = [(c, v) for c, v in planted if v in text]
    dropped = len(planted) - len(present)
    if dropped:
        print(f"[{idx}] 값 {dropped}개가 그대로 들어가지 않아 정답에서 제외")
    return Sample(
        id=f"llm-{split}-{seed}-{idx:03d}",
        split=split,
        source=f"llm:{meta['model']}",
        layout=ctx["style"],
        text="가상 데이터 — 실존 인물 아님\n" + text,
        planted=[{"category": c, "value": v} for c, v in present],
        keep=[t for t in ctx["tech"].split(", ") if t in text],
        hard=[h for h in hard if h in text],
    )


async def main_async(n: int, seed: int, split: str) -> None:
    meta, system, user_tpl = _load_prompt()
    settings = Settings()  # type: ignore[call-arg]
    backend = OpenAICompatBackend(api_key=settings.openai_api_key)
    sem = asyncio.Semaphore(5)

    async def guarded(i: int) -> Sample | None:
        async with sem:
            return await _one(backend, meta, system, user_tpl, i, seed, split)

    samples = [s for s in await asyncio.gather(*(guarded(i) for i in range(n))) if s]
    out = HERE / "samples" / f"llm_{split}_s{seed}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")
    print(f"{out}: {len(samples)}/{n} samples")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--split", choices=["dev", "test"], required=True)
    a = ap.parse_args()
    asyncio.run(main_async(a.n, a.seed, a.split))


if __name__ == "__main__":
    main()
