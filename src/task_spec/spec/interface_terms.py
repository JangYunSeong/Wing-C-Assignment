"""재료 인터페이스 ↔ 기능형 명세 (#256 1단계 명세 쪽, 데이터 재료).

계약은 재료가 정한 명령·입출력 파일·엔티티로 만들어진다 (starter/interface.py). 명세가 다른 형식·칸 이름·묶는 기준이나 채점이 시험할 수
없는 요구를 공개 요구로 정하면, 승인된 명세와 계약이 어긋난다 — 2026-11-15 kb002 명세: "집계 결과를 CSV 파일로 출력"·"컬럼은
collection_date, merchant_category…", 2026-11-18·19 woori001 명세: "매일 정해진 시간에 자동으로 실행되도록 스케줄에 등록한다" (4/4).

- interface_brief — 명세 생성 입력의 재료 블록(feature.seed_block)에 붙는 요약.
- spec_interface_problems — 공개 요구·공개 수용 기준이 인터페이스에 없는 파일 형식·칸 이름, 스케줄 등록 요구를 쓰면 재요청.
- spec_interface_flags — 상황 글·1단계 자료는 승인 화면 표시만 (업무 맥락의 'CSV 내려받기'·로그 키 같은 글을 재요청하지 않는다).
인터페이스가 없으면 셋 다 빈 결과 — 명세 생성은 예전과 같다.

일부러 단순하게 둔 것: 형식은 입력·출력을 가리지 않고 재료의 형식 전부를 허용한다 (글에서 입력·출력 문맥을 가르지 않는다).
2단계 자료(debug_stage)는 보지 않는다 — 배포 뒤 로그·증상이라 계약 인터페이스와 이름이 같을 까닭이 없다. 정규식은 흔한 표현만
잡는다 (스케줄 등록의 낯선 바꿔 쓰기는 놓칠 수 있다 — 계약 단계의 하네스 범위 검사가 한 번 더 막는다).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from task_spec.schemas import MaterialInterface

# 공개 요구에 나오면 형식을 정한 것으로 보는 낱말 (소문자 비교). jsonl 이 있으면 json 은 같은 말로 본다 (한 줄 = JSON 객체)
FORMAT_WORDS = ("csv", "tsv", "json", "jsonl", "parquet", "xlsx", "excel", "avro", "orc")
_KOREAN_FORMATS = {"엑셀": "excel"}
_ASCII = r"A-Za-z0-9_"
_SNAKE = re.compile(rf"(?<![{_ASCII}])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![{_ASCII}])")
_JSON_KEY = re.compile(r'"([a-z][a-z0-9_]*)"\s*:')
# 스케줄을 등록·설정하는 요구 — "스케줄(러)에/을/를 (작업) 등록·설정", "자동 실행되도록 등록·설정", cron. 입력 요약의
# "스케줄러가 매일 실행 날짜로 부른다" 는 걸리지 않는다 (조사 '가' 뒤에 등록·설정이 붙지 않는다)
_SCHEDULE = re.compile(
    r"스케줄(?:러)?\s*(?:에|을|를|에서)?\s*(?:작업\s*)?(?:등록|설정)"
    r"|자동\s*(?:으로\s*)?실행(?:되도록|하도록|되게)?\s*(?:등록|설정)|크론"
    rf"|(?<![{_ASCII}])(?:cron|crontab|cronjob)(?![{_ASCII}])",
    re.IGNORECASE,
)
# 칸 이름이 아니라 명령의 동작을 말하는 이름 — 공개 요구가 써도 계약과 어긋나지 않는다 (종료 코드 등)
_PROCESS_NAMES = frozenset({"exit_code", "return_code", "status_code", "error_code"})

FLAG_SPEC_INTERFACE = "상황·1단계 자료에 재료 인터페이스와 다른 형식·칸 이름 포함 — "
FLAG_MAX_NAMES = 6  # 승인 화면에는 앞의 몇 개만 — LLM 글에서 뽑은 이름을 길게 늘어놓지 않는다


def _names(interface: MaterialInterface) -> set[str]:
    """명세 글에 써도 되는 이름 — 옵션·칸 이름과 위치 패턴의 낱말 (daily_sales 등)."""
    names = {o.name for c in interface.commands for o in c.options}
    names |= {f.name for m in interface.entities for f in m.fields}
    for f in interface.files:
        names |= set(_SNAKE.findall(f.path))
    return names | _PROCESS_NAMES


def _formats(interface: MaterialInterface) -> set[str]:
    formats = {f.format for f in interface.files}
    return formats | ({"json"} if "jsonl" in formats else set())


def _format_words(text: str) -> list[str]:
    low = text.lower()
    found = [w for w in FORMAT_WORDS if re.search(rf"(?<![{_ASCII}]){w}(?![{_ASCII}])", low)]
    found += [v for k, v in _KOREAN_FORMATS.items() if k in text]
    return found


def _record_keys(text: str, names: set[str]) -> list[str]:
    """자료 본문의 JSON 줄 가운데 재료 칸 이름이 하나라도 있는 줄(원시 데이터 예시)의 키 — 로그 JSON({"level":…, "msg":…})은
    재료 칸이 없어 보지 않는다."""
    found: list[str] = []
    for line in text.splitlines():
        keys = _JSON_KEY.findall(line)
        if set(keys) & names:
            found += keys
    return found


def _other(text: str, interface: MaterialInterface, records_only: bool = False) -> list[str]:
    """글에 나온, 인터페이스에 없는 형식·칸 이름 (나온 순서, 겹침 없이). records_only: 이름은 원시 데이터 예시 줄의 JSON 키만."""
    names = _names(interface)
    formats = [w.upper() for w in _format_words(text) if w not in _formats(interface)]
    found = _record_keys(text, names) if records_only else _SNAKE.findall(text)
    return list(dict.fromkeys([*formats, *(n for n in found if n not in names)]))


def _fields(m_name: str, interface: MaterialInterface) -> str:
    entity = next(m for m in interface.entities if m.name == m_name)
    return " · ".join(f.name for f in entity.fields)


def interface_brief(interface: MaterialInterface | None) -> list[str]:
    """seed_block 끝에 붙는 줄들 — 계약이 이대로 만들어진다는 것과 명세가 지킬 것."""
    if interface is None:
        return []
    lines = [
        "재료가 정한 공개 인터페이스 (계약이 이대로 만들어진다 — 공개 요구·공개 수용 기준·1단계 자료가 이것과 어긋나지 않게):"
    ]
    for c in interface.commands:
        options = " · ".join(o.name for o in c.options)
        lines.append(f"- 명령 `{c.path}` — 옵션 {options}. {c.summary}")
    for f in interface.files:
        role = "입력" if f.role == "input" else "출력"
        lines.append(
            f"- {role} `{f.path}` — {f.format}, 한 줄(행)에 {f.entity} 하나: {_fields(f.entity, interface)}. {f.summary}"
        )
    lines += [
        "- 공개 요구·공개 수용 기준에 파일 형식·칸 이름을 쓰면 위의 것만 쓴다 — 다른 형식·칸 이름·묶는 기준을 만들지 않는다. "
        "1단계 자료의 원시 데이터 예시도 이 칸 이름으로 쓴다.",
        "- 스케줄 등록·시스템 설정(cron 등)은 공개 요구·수용 기준으로 쓰지 않는다 — 채점이 시험할 수 없다. "
        "'매일 실행'은 스케줄러가 실행 날짜로 명령을 부르는 것으로 담는다.",
    ]
    return lines


def _hint(interface: MaterialInterface) -> str:
    parts = []
    for f in interface.files:
        role = "입력" if f.role == "input" else "출력"
        parts.append(f"{role} {f.format}({f.entity}: {_fields(f.entity, interface)})")
    return " · ".join(parts)


def spec_interface_problems(
    requirements: Sequence[tuple[str, str]],
    acceptance: Sequence[tuple[str, str]],
    interface: MaterialInterface | None,
) -> list[str]:
    """(id, 글) 목록 — 공개 요구·공개 수용 기준. 인터페이스에 없는 형식·칸 이름과 스케줄 등록 요구를 재요청 사유로."""
    if interface is None:
        return []
    items = [*requirements, *acceptance]
    other = [(i, words) for i, text in items if (words := _other(text, interface))]
    schedule = [i for i, text in items if _SCHEDULE.search(text)]
    problems: list[str] = []
    if other:
        where = "; ".join(f"{i} {'·'.join(words)}" for i, words in other)
        problems.append(
            f"공개 요구·수용 기준이 재료 인터페이스에 없는 형식·칸 이름을 씀: {where} — 계약은 재료 인터페이스로 만들어진다 "
            f"({_hint(interface)}). 형식·칸 이름은 이대로 쓰고, 묶는 기준도 이 칸으로 쓴다"
        )
    if schedule:
        problems.append(
            f"공개 요구·수용 기준에 채점이 시험할 수 없는 스케줄 등록 요구: {', '.join(schedule)} — 등록 자체를 요구하지 않고, "
            "'매일 실행'은 스케줄러가 실행 날짜로 명령을 부르는 것으로 담는다"
        )
    return problems


def spec_interface_flags(
    scenario: str, artifacts: Iterable[str], interface: MaterialInterface | None
) -> list[str]:
    """상황 글(형식·snake_case 이름)과 1단계 자료(형식·JSON 키) — 승인자 확인용 표시."""
    if interface is None:
        return []
    found = _other(scenario, interface)
    for body in artifacts:
        found += _other(body, interface, records_only=True)
    found = list(dict.fromkeys(found))
    if not found:
        return []
    shown = "·".join(found[:FLAG_MAX_NAMES]) + (
        f" 외 {len(found) - FLAG_MAX_NAMES}개" if len(found) > FLAG_MAX_NAMES else ""
    )
    return [FLAG_SPEC_INTERFACE + shown + " (확인 필요)"]
