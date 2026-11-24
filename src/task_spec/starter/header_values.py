"""요청 헤더 값 — 계약 공개 테스트 예시에서 규칙을 통과하는 값·권한이 거절되는 값을 뽑는다 (도우미 상수의 근거).

2026-11-08 알파 (2df170e2): 계약은 권한 있는 요청자를 공개 테스트 예시(`X-Requester-Id=admin … → 200`)로만 말하는데,
숨은 테스트가 요청자를 `grader` 로 지어내 모든 요청이 403 에서 끝났다 — hc2(저장 실패 주입)는 저장 단계에 닿지 못해
기능 없는 시작 코드도 통과했다. 테스트가 값을 짐작하지 않게 도우미가 상수로 준다.

오탐을 줄이려고 좁게 읽는다: 계약 엔드포인트에 선언된 헤더 이름 바로 뒤의 값, 그 뒤에 처음 나오는 상태 코드 중
그 헤더를 받는 엔드포인트가 **선언한** 상태만 본다. 401·403 이면 거절되는 값, 그 밖이면 통과하는 값(권한은 통과했고
다른 규칙에서 갈림). 같은 값이 양쪽에 나오면 버린다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from task_spec.schemas.build import TaskContract

DENIED = (401, 403)
# 예시가 없고 그 헤더를 받는 엔드포인트에 권한 거절 응답도 없으면 아무 값이나 받는 헤더 — 이 값을 준다
NEUTRAL = "grader"


PER_REQUEST = "요청마다"
# 다른 API(로그인)가 응답으로 발급하는 값 — 통과 값은 도우미가 로그인해 받는다 (권한 칸 issued, auth_rule)
ISSUED = "발급"
# 요청 하나를 가리키는 이름·제약 — 계약 칸 per_request 의 바닥 (fit_per_request). '고유 식별자'·uuid 는 회원 id 에도 써서 넣지 않는다
_REQUEST_KEY = re.compile(
    r"request[-_]?id|idempot|nonce|dedup|요청\s*(?:번호|id|식별자)|멱등", re.I
)


def per_request(contract: TaskContract, header: str) -> bool:
    """요청마다 새 값이어야 하는 헤더 — 계약 헤더 칸 per_request (HeaderField). 2026-11-10 retrydup-c: 요청 번호 X-Request-Id 를
    도우미가 상수 하나로 줘 모든 공개·숨은 테스트가 같은 번호를 공유 — 명세대로 번호로 중복을 막은 허용 구현 d2 가 서로 다른 충전을
    재요청으로 보고 다른 회원의 처음 결과를 돌려줘 떨어졌다. #181: 이름·제약 글의 낱말 사전으로 짐작하던 것을 칸으로 — 사전 밖 이름
    (X-Nonce·X-Dedup-Key)·제약 '고유·중복 불가'는 놓쳤다. 칸이 비면 False (계약 검사가 재요청한다 — unset_per_request).
    사전은 칸이 틀린 경우의 바닥으로 남았다 — fit_per_request 가 칸을 맞춘 뒤 여기는 칸만 본다."""
    return any(h.per_request for e in contract.endpoints for h in e.headers if h.name == header)


def _auth_headers(contract: TaskContract) -> set[str]:
    """권한 칸(auth)이 가리키는 헤더 — 값은 권한 규칙이 정한다 (요청자)."""
    return {e.auth.header for e in contract.endpoints if e.auth is not None}


def fit_per_request(contract: TaskContract) -> tuple[TaskContract, list[str]]:
    """이름·제약이 요청 하나를 가리키는 헤더(요청 번호·멱등 키·nonce)는 per_request 를 true 로 (권한 칸의 헤더 밖, 기계적 출력).

    2026-11-15 알파 pilot-retry-ae030: 계약이 X-Request-Id(제약 '요청 고유 식별자')에 false 를 적어 도우미가 상수 하나를 줬다 —
    공개 테스트의 세 번 충전이 같은 번호라, 명세대로 번호로 중복을 막은 허용 구현 d1·d2 가 둘째·셋째를 재요청으로 보고 떨어졌고
    수리는 구현·테스트를 다시 만들다 예산을 다 썼다. 같은 날 같은 이름·거의 같은 제약의 계약 셋 중 둘은 true 였다.
    #181 이 사전을 칸으로 바꾼 뒤 칸이 틀리면 막는 것이 없었다 — 사전은 바닥, 사전 밖 이름은 칸이 맡는다. 틀림의 값이 한쪽으로
    쏠려 있다: 요청마다 새 값이어야 할 헤더에 상수를 주면 맞는 구현이 떨어지지만, 상수여도 되는 헤더에 새 값을 주면 해가 없다."""
    auth = _auth_headers(contract)
    was: dict[str, set[bool | None]] = {}
    for e in contract.endpoints:
        for h in e.headers:
            if h.name not in auth and _REQUEST_KEY.search(f"{h.name} {h.constraints or ''}"):
                was.setdefault(h.name, set()).add(h.per_request)
    names = {n for n in was if not per_request(contract, n)}
    if not names:
        return contract, []
    endpoints = [
        e.model_copy(
            update={
                "headers": [
                    h.model_copy(update={"per_request": True}) if h.name in names else h
                    for h in e.headers
                ]
            }
        )
        for e in contract.endpoints
    ]
    notes = [
        f"헤더 {n} 의 per_request 를 true 로 (계약은 {'비어 있음' if was[n] == {None} else 'false'}) — "
        "이름·제약이 요청 하나를 가리킴: 도우미가 요청마다 새 값을 준다"
        for n in sorted(names)
    ]
    return contract.model_copy(update={"endpoints": endpoints}), notes


def unset_per_request(contract: TaskContract) -> list[str]:
    """per_request 를 적지 않은 헤더 (권한 칸의 헤더 밖) → 재요청 사유 하나."""
    auth = _auth_headers(contract)
    names = sorted(
        {
            h.name
            for e in contract.endpoints
            for h in e.headers
            if h.per_request is None and h.name not in auth
        }
    )
    if not names:
        return []
    return [
        f"헤더 {', '.join(names)} 에 per_request 가 없음 — 요청마다 새 값이어야 하는 헤더(요청 번호·멱등 키처럼 요청 하나를 가리키는 값)면 "
        "true, 여러 요청이 같은 값을 써도 되면(요청자·클라이언트 이름 등) false 로 적는다. 테스트 도우미가 true 면 요청마다 새 값을, "
        "false 면 정해진 값 하나를 준다"
    ]


@dataclass(frozen=True)
class HeaderValues:
    name: str
    ok: str | None
    denied: str | None
    source: str  # "예시" · "아무 값" · "권한 규칙" · "발급" · "요청마다" — 도우미 주석에


def _statuses(contract: TaskContract, header: str) -> set[int]:
    return {
        r.status
        for e in contract.endpoints
        if any(h.name == header for h in e.headers)
        for r in e.responses
    }


def header_values(contract: TaskContract) -> list[HeaderValues]:
    """계약 헤더마다 (통과하는 값, 거절되는 값). 정할 수 없으면 None — 지어내지 않는다."""
    names = sorted({h.name for e in contract.endpoints for h in e.headers})
    ruled = {
        e.auth.header: e.auth
        for e in contract.endpoints
        if e.auth is not None and e.auth.ok_value is not None
    }
    issued = {
        e.auth.header: e.auth
        for e in contract.endpoints
        if e.auth is not None and e.auth.kind == "issued"
    }
    out: list[HeaderValues] = []
    for name in names:
        if (
            rule := ruled.get(name)
        ) is not None:  # 권한 칸에서 코드가 정한 요청자 행의 값 (auth_rule.fit_auth)
            out.append(HeaderValues(name, rule.ok_value, rule.denied_value, "권한 규칙"))
            continue
        # 발급받는 값(권한 칸 issued) — 통과 값은 고정 값이 없다, 도우미가 로그인해 받는다
        if (got := issued.get(name)) is not None:
            out.append(HeaderValues(name, None, got.denied_value, ISSUED))
            continue
        if per_request(
            contract, name
        ):  # 요청마다 다른 값 — 상수 하나로 주면 모든 요청이 같은 요청이 된다
            out.append(HeaderValues(name, None, None, PER_REQUEST))
            continue
        declared = _statuses(contract, name)
        value_rx = re.compile(
            r"(?<![\w-])" + re.escape(name) + r"\s*[=:]\s*[\"'`]?([A-Za-z0-9_.@-]+)", re.I
        )
        ok: Counter[str] = Counter()
        bad: Counter[str] = Counter()
        for t in contract.public_tests:
            for m in value_rx.finditer(t.checks):
                status = next(
                    (
                        int(s.group(0))
                        for s in re.finditer(r"(?<!\d)[1-5]\d\d(?!\d)", t.checks[m.end() :])
                        if int(s.group(0)) in declared
                    ),
                    None,
                )
                if status is None:
                    continue
                (bad if status in DENIED else ok)[m.group(1)] += 1
        both = set(ok) & set(bad)
        ok_v = next((v for v, _ in ok.most_common() if v not in both), None)
        bad_v = next((v for v, _ in bad.most_common() if v not in both), None)
        if ok_v is None and bad_v is None and not declared & set(DENIED):
            out.append(HeaderValues(name, NEUTRAL, None, "아무 값"))
        else:
            out.append(HeaderValues(name, ok_v, bad_v, "예시"))
    return out


_VALUE = re.compile(r"[\"'`]?[A-Za-z0-9_.@-]+")
_SHOWN = re.compile(r"<[^>]{1,60}>|\{[^}]{1,60}\}|\[[^\]]{1,60}\]|\([^)]{1,60}\)|[^\s,;]{1,40}")


def _placeholders(contract: TaskContract, header: str) -> list[str]:
    """값 자리에 실제 값 대신 쓴 것 — `헤더=`·`헤더:` 대입 중 값 글자(header_values 가 읽는 꼴)로 시작하지 않는 것 전부
    (`<MASTER 등급 멤버의 id>`·`{값}`·`[통과 값]`·`(통과하는 값)`·`관리자`). #181: 예전엔 `<…>` 꼴만 찾아 다른 꼴은 진단에 없었다."""
    rx = re.compile(r"(?<![\w-])" + re.escape(header) + r"\s*[=:]\s*", re.I)
    out: list[str] = []
    for t in contract.public_tests:
        for m in rx.finditer(t.checks):
            if _VALUE.match(t.checks, m.end()):
                continue
            shown = _SHOWN.match(t.checks, m.end())
            out.append(
                f"{t.test_id} 의 {shown.group(0) if shown else t.checks[m.end() : m.end() + 20]}"
            )
    return out


def header_problems(contract: TaskContract) -> list[str]:
    """권한 거절(401·403)을 선언한 엔드포인트의 헤더인데 통과하는 값 예시가 없음 → 재요청 사유 (계약 검사 §3.3).
    값이 없으면 숨은·공개 테스트가 요청자를 지어내 요청이 거절에서 끝난다 (2026-11-08 알파: 계약 5개 중 2개가 '권한 없는 요청자'만 글로).

    2026-11-09 알파(cap2): 재요청 문구가 `<통과하는 값>` 꼴이라 답이 `<MASTER 등급 멤버의 memberId>` 로 돌아왔고, 그 권한을
    통과하는 행이 시드에 없었다 — 문구는 실제 값 예시와 시드 행을 함께 말한다."""
    out = unset_per_request(contract)
    ruled = {
        e.auth.header for e in contract.endpoints if e.auth is not None and e.auth.kind != "other"
    }
    for h in header_values(contract):
        if h.source == PER_REQUEST:
            continue
        if h.ok is not None or h.name in ruled or not _statuses(contract, h.name) & set(DENIED):
            continue  # 권한 칸(role·fixed·issued)이 있으면 그쪽 검사가 말한다 (auth_rule.auth_problems)
        # 여기 오는 헤더는 코드가 요청자 값을 정하지 않는다 (auth 칸 없음·other) — 문구 하나 (#181: other 일 때만 앞에 붙이던
        # 갈래는 지웠다. 2026-11-09 fixmt-be2 의 지시 충돌은 plan_contract v15 부터 프롬프트가 같은 말을 한다)
        msg = (
            f"헤더 {h.name} 를 받는 엔드포인트가 권한 거절({'·'.join(map(str, DENIED))})을 내는데 통과하는 값의 예시가 없음 — "
            "이 헤더는 코드가 요청자 값을 정하지 않으므로(auth 칸 없음·other) '통과하는 요청자로' 같은 말 대신 "
            f"공개 테스트 checks 에 `{h.name}=1 → 200`, `{h.name}=7 → 403` 처럼 실제 값과 결과를 함께 적는다. "
            "권한이 요청자의 데이터(등급·소유 등)에 달렸으면 조건을 만족하는 행과 만족하지 않는 행을 시드 groups 에 두고 그 행의 값을 쓴다"
        )
        if ph := _placeholders(contract, h.name):
            msg += f" (값 자리에 자리표시자를 씀: {'; '.join(ph[:3])} — 값으로 읽지 못함)"
        out.append(msg)
    return out
