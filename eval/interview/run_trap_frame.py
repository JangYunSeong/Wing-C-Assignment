"""함정 틀 평가 (#318, prompts/trap_frame) — 재료로 이 공고의 틀(숨길 것·공개 쪽 금지·상태 대상·허용 설계 대조)을 맞게 정하는가.

운영과 같은 순서로 돈다: 재료 분석(analyze_material CURRENT) → 함정 틀(평가할 판). 재료 블록은 운영과 같은 저장본 검사(stored_json)를,
틀 출력은 운영과 같은 가명화 검사(derived)와 틀 검사(frame_problems)·틀 적용(apply_frame)을 거친다.

표본 (가상·공개만 — 지원자·회사 원문 없음). 함정 재료 = 흔한 구현이 요청이 만드는 상태를 DB 테이블에 두고, 공개 계약이 그 테이블을
요구하며(stateful_target), 허용 설계 하나가 업무 요구를 못 지키는 재료 (2026-11-24 세션 유지 과제 ct-f6087a7ba679 의 꼴).
- dev (8): 함정 2 (가상 세션 유지 · 가상 요청 한도) + 평범한 6 (공개 4: 풀 교착·갱신 손실·재시도 중복·목록 조회 + 가상 디스크
  가득 참·가상 주문 상세 포화형). 요청 한도·주문 상세는 v1 의 held-out 이었다 — v1 결과를 본 뒤 dev 로 옮겼다.
- heldout (3, v2 를 쓴 뒤 만든 것): 함정 1 (가상 인증 코드 — 서명 코드 설계는 '5번 틀리면 무효'를 못 지킴) + 평범한 2 (가상 좌석
  중복 예약 — 좌석은 정당한 상태 대상 · 가상 배송사 호출 시간 초과 — 계약 필요 요소 없음)

기준 (실행 전 고정 — v1 2026-11-25, v2 에서 측정 보정과 hidden_kept 추가):
- kind_ok: 틀 종류가 허용 안 (지금 템플릿은 design 만)
- reason_ok: 실패 사유가 기대 목록 안
- storage_ok: 함정 재료는 금지 저장 엔티티가 함정 낱말(세션·사용량 등)에 걸린다 / 평범한 재료는 핵심 업무 엔티티(주문·재고·좌석 등)를
  금지하지 않는다
- stateful_ok: 함정 재료는 상태 대상을 흔한 구현의 저장 방식으로 본다(true) / 평범한 재료는 false
- design_ok: 함정 재료는 업무 요구를 못 지키는 설계(재료의 몇 번째 설계인지로 지정)를 yes 가 아니게 보고 다른 설계를 하나 이상
  yes 로 남긴다 / 평범한 재료는 no 가 하나도 없다. 틀의 설계 글은 재료 설계 앞부분과 글자로 맞춘다 (띄어쓰기·[설계] 표시 무시)
- usable: 가명화 검사·틀 검사를 통과하고 틀 적용 재료가 스키마를 지킨다
- hidden_kept (v2~): design 틀의 숨은 요구가 재료의 숨은 요구를 글자 그대로 담는다 (interview/trap_frame.keeps_hidden — 아니면
  코드가 재료 것을 쓴다). v1 평가에서 평범한 재료의 조건을 빼거나 허용 설계를 배제하는 조건을 더했다
- entity_ok (v3~): 금지 엔티티 이름이 모두 영문 PascalCase 식별자다 — 계약 검사 4 가 계약 엔티티 이름으로 대조한다. v2 는 요청 한도
  표본에서 두 번 모두 '회원별 요청 사용량'으로 써 대조할 수 없었다
- 안정성(stable): 같은 표본의 회차끼리 금지 엔티티 판단·상태 대상 판단·걸러낸 설계(번호)가 같다 (2회 이상일 때)
- 승격 제안: dev 에서 hidden_kept 를 뺀 일곱이 모두 1.0·hidden_kept ≥ 0.9, held-out 에서 모두 통과(출력도 읽어 본다). 같은 held-out 에서
  v1 과 견준다

측정 보정 (v1 결과를 본 뒤): 주문 상세 표본의 핵심 낱말에 inventory 가 빠져 'Inventory' 금지를 통과로 셌다 → 낱말 추가.
설계를 글자(40자)로 견줘 띄어쓰기만 다른 같은 설계를 다른 설계로 셌다 → 번호로.

평가할 판을 CURRENT 로 둔 임시 프롬프트 폴더를 쓴다 (레포의 CURRENT 는 건드리지 않는다). 결과는 git 밖에 쓴다.

사용 (레포 루트에서, 모듈로):
  uv run python -m eval.interview.run_trap_frame --version 2 --runs 2 --out data/eval/interview/trap_frame
  uv run python -m eval.interview.run_trap_frame --version 2 --set heldout --out data/eval/interview/trap_frame
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from eval.interview.run_material_analysis import DEADLOCK, JAVA, REPO, _pseudonymized, _virtual
from task_spec.config import Settings
from task_spec.interview.material_analysis import TASK as ANALYSIS_TASK
from task_spec.interview.material_analysis import analyze_material, material_block
from task_spec.interview.trap_frame import (
    TASK,
    apply_frame,
    frame_block,
    frame_problems,
    frame_texts,
    keeps_hidden,
    make_frame,
)
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.pseudonymize import InterviewPseudonymizer
from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import Detectors, RunMapping
from task_spec.prompting import PromptRegistry
from task_spec.schemas import FeatureSeed, PublicIncident, TrapFrame
from task_spec.schemas.incident import ContractNeed
from task_spec.spec.incidents import incident_by_id, load_incidents
from task_spec.starter.templates import current_version, load_template


@dataclass(frozen=True)
class Sample:
    name: str
    incident: PublicIncident
    reasons: tuple[str, ...]  # 맞다고 볼 사유 (빈 튜플 = 아무 사유)
    trap: bool  # 함정 재료인가 (공개 쪽 금지·상태 대상·설계 충돌이 있어야 함)
    # 함정: 금지 엔티티가 걸려야 할 낱말 / 평범: 걸리면 안 되는 핵심 업무 낱말
    storage_words: tuple[str, ...]
    bad_design: int | None = None  # 함정: yes 가 아니어야 할 재료 accepted_designs 의 번호 (0부터)


def _need(kind: str, note: str) -> ContractNeed:
    return ContractNeed.model_validate({"kind": kind, "note": note})


def _stateful(note: str) -> ContractNeed:
    return _need("stateful_target", f"{note} — 이미 있는 엔티티, 시드 행, 숨은 케이스가 바꿀 행")


POOL = {"reasons": ["pool_acquire_timeout"], "harness": ["pool_size", "concurrency"]}
AUDIT = (
    "주문",
    "배송",
    "거래",
    "감사",
    "기록",
    "order",
    "delivery",
    "shipment",
    "audit",
    "history",
)


def samples(root: Path) -> list[Sample]:
    catalog = load_incidents(root)

    def pub(incident_id: str) -> PublicIncident:
        found = incident_by_id(catalog, incident_id)
        assert found is not None, incident_id
        return found

    base = pub(DEADLOCK)
    session = _virtual(
        base,
        "co:sw.backend:virtual-session-frame",
        symptom="피크 시간대 로그인 유지 요청이 늘면 일부 요청이 DB 연결 획득 시간 초과로 실패",
        root_cause="요청마다 세션 테이블을 조회하고 마지막 접근 시각을 갱신해 요청 하나가 DB 문장을 여러 개 실행한다 — 연결을 쥐는 "
        "시간이 길어 대기가 쌓인다",
        naive_fix="커넥션 풀 크기를 늘린다",
        why_naive_fails="부하가 바뀌면 다시 대기가 쌓인다 — 요청당 DB 일을 줄이지 않았다",
        notes="커넥션 풀 수치·동시 접속 규모는 확인되지 않음",
        # 틀 입력은 업무 요구·계약 필요 요소도 받는다 — 바탕(교착 재료)의 감사 기록 요구를 물려받지 않게 표본마다 정한다
        business_need="로그인 API 를 추가하고, 로그인으로 받은 세션 값으로 기존 조회 API(상품·주문)를 쓰게 한다. 세션은 마지막 접근 뒤 "
        "30분 동안 유효하고 접근할 때마다 만료가 연장된다. 만료되었거나 없는 세션이면 401 을 준다.",
        naive_impl="세션을 DB 테이블에 저장하고, 요청마다 세션 테이블에서 세션을 조회해 마지막 접근 시각을 갱신한 뒤 본 처리를 한다",
        trigger="피크 시간대 동시 요청 수가 커넥션 풀 크기보다 크다",
        why_naive_breaks="요청 하나가 세션 조회·갱신까지 DB 문장 약 4개를 실행해 연결을 오래 쥐고, 동시 요청이 풀보다 많으면 뒤 요청의 연결 "
        "대기가 대기 한도를 넘는다",
        hidden_requirement="피크 시간대에도 로그인 유지 확인이 DB 연결 대기로 실패하지 않는다",
        accepted_designs=[
            "[설계] 서명된 토큰(JWT)으로 세션을 확인해 요청마다 DB 를 보지 않음 — 조건: 만료 시각은 발급할 때 토큰에 담는다 — "
            "함정: 폐기를 확인하려고 요청마다 DB 를 보면 흔한 구현과 같다",
            "[설계] 세션을 메모리 저장소에 두고 접근할 때 만료를 연장, DB 에는 로그인 때만 씀 — 조건: 저장소를 서버끼리 공유 — "
            "함정: 메모리 저장소를 쓰면서 요청마다 DB 에도 반영하면 흔한 구현과 같다",
        ],
        contract_needs=[_stateful("세션(로그인 상태) 엔티티")],
        **POOL,
    )
    quota = _virtual(
        base,
        "co:sw.backend:virtual-rate-limit-frame",
        symptom="이벤트 시간에 API 요청이 몰리면 요청 한도 확인 자체가 DB 잠금 대기로 느려지고 일부가 연결 획득 시간 초과로 실패",
        root_cause="요청마다 회원별 사용량 테이블의 행을 읽고 1 을 더해 다시 쓴다 — 같은 회원 행에 잠금이 몰리고 연결을 쥐는 시간이 "
        "길어진다",
        naive_fix="사용량 테이블에 색인을 더한다",
        why_naive_fails="행 잠금 경합과 요청당 DB 문장 수는 그대로다",
        notes="이벤트 시간 동시 요청 규모는 확인되지 않음",
        business_need="회원별로 분당 요청 한도를 두는 기능을 기존 조회 API 에 더한다. 한도는 모든 서버의 요청을 합쳐 센다. 한도를 넘으면 "
        "429 와 다시 시도할 시각을 준다.",
        naive_impl="요청 사용량을 DB 테이블에 저장하고, 요청마다 회원별 사용량 행을 읽고 1 을 더해 다시 쓴다",
        trigger="이벤트 시간 동시 요청 수가 커넥션 풀 크기보다 크다",
        why_naive_breaks="요청마다 사용량 행 잠금을 잡고 DB 문장을 여러 번 실행해 연결 대기가 대기 한도를 넘는다",
        hidden_requirement="이벤트 시간에도 요청 한도 확인이 DB 연결 대기로 실패하지 않는다",
        accepted_designs=[
            "[설계] 서버마다 메모리에 회원별 고정 윈도 카운터를 둠 — 조건: 서버가 하나 — 함정: 카운터를 요청마다 DB 에 반영하면 흔한 "
            "구현과 같다",
            "[설계] 공유 메모리 저장소(캐시)의 원자 증가로 회원별 카운터를 셈 — 조건: 저장소의 만료로 윈도를 정함 — 함정: 저장소 장애 때 "
            "한도 확인을 건너뛰면 한도가 사라진다",
        ],
        contract_needs=[_stateful("회원별 요청 사용량 엔티티")],
        **POOL,
    )
    order = _virtual(
        base,
        "co:sw.backend:virtual-order-detail-frame",
        symptom="점심 피크에 주문 상세 조회가 느려지고 일부 요청이 DB 연결 획득 시간 초과로 실패",
        root_cause="주문 상세를 만들 때 요청마다 재고·가격·쿠폰·배송 테이블을 차례로 조회해 요청 하나가 DB 문장 5개를 실행한다",
        naive_fix="커넥션 풀 크기를 늘린다",
        why_naive_fails="피크 규모가 바뀌면 다시 대기가 쌓인다",
        notes="풀 크기·피크 동시 요청 수는 확인되지 않음",
        business_need="기존 주문 상세 조회 API 가 주문의 재고·가격·쿠폰·배송 정보를 함께 내려 준다.",
        naive_impl="주문 상세 API 가 요청마다 재고·가격·쿠폰·배송 정보를 각각 조회해 합친다",
        trigger="피크 시간 동시 요청 수가 커넥션 풀 크기보다 크다",
        why_naive_breaks="요청 하나가 DB 문장 5개를 실행해 연결을 오래 쥐고 뒤 요청의 연결 대기가 대기 한도를 넘는다",
        hidden_requirement="피크 시간에도 주문 상세 조회가 DB 연결 대기로 실패하지 않는다",
        accepted_designs=[
            "[설계] 한 번의 조인 조회로 필요한 값을 함께 읽음 — 조건: 조인 결과 크기가 응답 한도 안",
            "[설계] 자주 바뀌지 않는 가격·쿠폰 정보를 메모리 캐시에서 읽음 — 조건: 캐시 만료가 가격 변경 반영 한도보다 짧음",
        ],
        contract_needs=[
            _need(
                "related_entities",
                "딸린 정보(재고·가격·쿠폰·배송)는 주문과 foreign_keys 로 이어진 다른 엔티티에서 읽는다",
            )
        ],
        **POOL,
    )
    disk = _virtual(
        base,
        "co:sw.backend:virtual-disk-full-frame",
        symptom="야간 배치 뒤 로그 디스크가 가득 차 API 가 요청 기록을 쓰지 못하고 500 을 돌려줌",
        root_cause="요청 로그 파일을 회전하지 않아 디스크가 가득 찼다",
        naive_fix="로그 수준을 낮춘다",
        why_naive_fails="쌓이는 속도만 늦출 뿐 디스크가 차는 조건은 그대로다",
        notes="디스크 크기·로그 증가 속도는 확인되지 않음",
        business_need="기존 주문 조회 API 의 요청마다 요청 기록(시각·경로·응답 코드)을 남기고, 운영자가 날짜별 요청 기록 건수를 "
        "조회하는 API 를 둔다.",
        naive_impl="요청마다 로컬 파일에 요청 기록 한 줄을 덧붙인다",
        trigger="디스크 여유 공간이 바닥난다",
        why_naive_breaks="파일 쓰기가 실패하면 요청 처리 전체가 실패한다",
        hidden_requirement="로그 디스크가 가득 차도 요청 처리는 성공하고 기록 실패만 따로 알린다",
        accepted_designs=[
            "[설계] 로그 쓰기 실패를 요청 처리와 떼어 내고 경보만 남김 — 조건: 기록 누락을 다른 경로로 셀 수 있음",
            "[설계] 크기 기준으로 로그 파일을 회전 — 조건: 보관 기간이 디스크 크기 안에 맞음",
        ],
        contract_needs=[],
        reasons=["server_error"],
        harness=[],
    )
    order_words = (
        "주문",
        "배송",
        "재고",
        "가격",
        "쿠폰",
        "order",
        "delivery",
        "stock",
        "inventory",
    )
    return [
        Sample(
            "virtual-session",
            session,
            ("pool_acquire_timeout",),
            True,
            ("session", "세션"),
            bad_design=0,
        ),
        Sample(
            "rate-limit",
            quota,
            ("pool_acquire_timeout",),
            True,
            ("usage", "quota", "limit", "count", "사용량", "한도", "카운터"),
            bad_design=0,
        ),
        Sample("pool-deadlock", base, ("pool_acquire_timeout",), False, AUDIT),
        Sample(
            "lost-update",
            pub("pub:sw.backend:read-modify-write-lost-update"),
            ("lost_update",),
            False,
            ("상품", "포인트", "계정", "좌석", "product", "stock", "inventory", "account", "seat"),
        ),
        Sample(
            "retry-duplicate",
            pub("pub:sw.backend:retry-double-processing"),
            ("duplicate_record", "record_mismatch"),
            False,
            (
                "회원",
                "지갑",
                "계정",
                "충전",
                "기록",
                "member",
                "wallet",
                "account",
                "charge",
                "record",
            ),
        ),
        Sample(
            "list-n-plus-one",
            pub("pub:sw.backend:list-query-per-row"),
            ("request_timeout",),
            False,
            (
                "주문",
                "게시",
                "예약",
                "고객",
                "상품",
                "order",
                "post",
                "reservation",
                "customer",
                "item",
            ),
        ),
        Sample("virtual-disk-full", disk, ("server_error",), False, AUDIT),
        Sample(
            "order-detail",
            order,
            ("pool_acquire_timeout",),
            False,
            (*order_words, "price", "coupon", "shipping", "shipment"),
        ),
    ]


def heldout_samples(root: Path) -> list[Sample]:
    """프롬프트를 고칠 때 보지 않은 가상 재료 (v2 를 쓴 뒤 만든 것) — 함정 하나·평범한 것 둘."""
    base = incident_by_id(load_incidents(root), DEADLOCK)
    assert base is not None
    verify = _virtual(
        base,
        "co:sw.backend:heldout-verify-code",
        symptom="가입 이벤트 때 인증 코드 확인 요청이 몰리면 일부 요청이 DB 연결 획득 시간 초과로 실패",
        root_cause="확인 요청마다 인증 코드 테이블에서 코드를 조회하고 시도 횟수를 갱신해 요청 하나가 DB 문장을 여러 개 실행한다 — "
        "연결을 쥐는 시간이 길어 대기가 쌓인다",
        naive_fix="커넥션 풀 크기를 늘린다",
        why_naive_fails="이벤트 규모가 바뀌면 다시 대기가 쌓인다 — 요청당 DB 일을 줄이지 않았다",
        notes="풀 크기·이벤트 동시 요청 수는 확인되지 않음",
        business_need="회원 가입에 이메일 인증을 더한다. 인증 코드를 발급하는 API 와 확인하는 API 를 둔다. 코드는 발급 뒤 3분 동안 "
        "유효하고, 5번 틀리면 그 코드는 무효가 된다. 만료되었거나 무효인 코드면 400 을 준다.",
        naive_impl="인증 코드를 DB 테이블에 저장하고, 확인 요청마다 코드 행을 조회해 시도 횟수를 갱신한 뒤 맞는지 본다",
        trigger="가입 이벤트 때 동시 확인 요청 수가 커넥션 풀 크기보다 크다",
        why_naive_breaks="요청 하나가 코드 조회·시도 횟수 갱신까지 DB 문장 여러 개를 실행해 연결을 오래 쥐고, 동시 요청이 풀보다 많으면 "
        "뒤 요청의 연결 대기가 대기 한도를 넘는다",
        hidden_requirement="가입 이벤트 때에도 인증 코드 확인이 DB 연결 대기로 실패하지 않는다",
        accepted_designs=[
            "[설계] 코드와 만료 시각을 서명해 클라이언트에 돌려주고 확인할 때 서명만 검사 — 조건: 서명 키를 서버끼리 공유 — 함정: 서명 "
            "검사 뒤 DB 에서 코드를 다시 조회하면 흔한 구현과 같다",
            "[설계] 코드와 시도 횟수를 공유 캐시에 만료 3분으로 두고 확인할 때 시도 횟수를 원자 증가로 셈 — 조건: 캐시를 서버끼리 공유 "
            "— 함정: 캐시 장애 때 확인을 건너뛰면 인증이 사라진다",
        ],
        contract_needs=[_stateful("인증 코드 엔티티")],
        **POOL,
    )
    booking = _virtual(
        base,
        "co:sw.backend:heldout-seat-booking",
        symptom="공연 예매 오픈 직후 같은 좌석이 두 사람에게 예약됨",
        root_cause="좌석에 예약이 있는지 조회한 뒤 없으면 예약 행을 넣는다 — 동시에 들어온 두 요청이 모두 빈 좌석으로 본다",
        naive_fix="예약 전에 한 번 더 조회한다",
        why_naive_fails="경합 창이 줄 뿐 사라지지 않는다",
        notes="오픈 직후 동시 요청 규모는 확인되지 않음",
        business_need="기존 공연 좌석에 예약 API 를 추가한다. 예약하면 예약 번호를 응답하고, 이미 예약된 좌석이면 409 를 준다. 좌석별 "
        "예약 상태를 조회하는 API 도 둔다.",
        naive_impl="예약 요청마다 그 좌석의 예약 행이 있는지 조회하고, 없으면 예약 행을 넣는다",
        trigger="오픈 직후 같은 좌석에 예약 요청이 동시에 몰린다",
        why_naive_breaks="두 요청이 모두 빈 좌석으로 확인한 뒤 각자 예약 행을 넣어 한 좌석에 예약이 둘 생긴다",
        hidden_requirement="같은 좌석에 동시에 들어온 예약 가운데 하나만 성공하고 나머지는 거절된다 — 좌석 하나에 성공한 예약은 한 "
        "건이다.",
        accepted_designs=[
            "[설계] 좌석별 예약에 유일 제약을 두고 위반이면 409 로 거절 — 조건: 취소된 예약은 유일 제약에서 빠짐",
            "[설계] 좌석 행을 잠그고 예약 여부를 확인해 바꾼다 — 조건: 좌석을 바꾸는 모든 경로가 같은 잠금을 씀",
        ],
        contract_needs=[
            _need(
                "stateful_target",
                "좌석 엔티티 — 이미 있는 엔티티, 시드 행, 숨은 케이스가 같은 좌석에 예약을 몰아 보낼 행",
            ),
            _need(
                "empty_record_entity",
                "예약 요청마다 쌓이는 예약 엔티티 — 이 과제에서 새로 만듦, 처음 0행",
            ),
        ],
        reasons=["duplicate_record"],
        harness=["concurrency"],
    )
    courier = _virtual(
        base,
        "co:sw.backend:heldout-courier-timeout",
        symptom="배송사 API 가 느려지면 주문 조회까지 느려지고 일부 요청이 시간 초과로 실패",
        root_cause="주문 조회가 배송 상태를 배송사 API 에 동기로 묻고 시간 한도 없이 기다린다",
        naive_fix="서버 스레드 수를 늘린다",
        why_naive_fails="느린 응답이 늘면 다시 막힌다 — 기다리는 시간을 줄이지 않았다",
        notes="배송사 API 지연 분포는 확인되지 않음",
        business_need="기존 주문 조회 API 에 배송 상태를 함께 내려 준다. 배송 상태는 배송사 API 에서 받는다.",
        naive_impl="주문 조회 요청마다 배송사 API 를 시간 한도 없이 불러 배송 상태를 붙인다",
        trigger="배송사 API 응답이 수 초씩 늦어진다",
        why_naive_breaks="요청이 배송사 응답을 기다리느라 주문 조회 응답이 응답 시간 한도를 넘는다",
        hidden_requirement="배송사 API 가 느려도 주문 조회는 응답 시간 한도 안에 끝나고, 배송 상태를 받지 못했으면 그 사실을 응답에 "
        "표시한다.",
        accepted_designs=[
            "[설계] 배송사 호출에 짧은 시간 한도를 두고 넘으면 배송 상태를 '확인 중'으로 표시 — 조건: 시간 한도가 응답 시간 한도보다 짧음",
            "[설계] 배송 상태를 주기적으로 받아 저장해 두고 조회 때는 저장한 값을 씀 — 조건: 저장 시각을 함께 응답",
        ],
        contract_needs=[],
        reasons=["request_timeout"],
        harness=[],
    )
    return [
        Sample(
            "heldout-verify-code",
            verify,
            ("pool_acquire_timeout",),
            True,
            ("verif", "code", "otp", "인증", "코드"),
            bad_design=0,
        ),
        Sample(
            "heldout-seat-booking",
            booking,
            ("duplicate_record",),
            False,
            (
                "좌석",
                "예약",
                "공연",
                "seat",
                "reservation",
                "booking",
                "performance",
                "show",
                "ticket",
            ),
        ),
        Sample(
            "heldout-courier-timeout",
            courier,
            ("request_timeout",),
            False,
            (
                "주문",
                "배송",
                "order",
                "delivery",
                "shipping",
                "shipment",
                "courier",
                "status",
                "상태",
            ),
        ),
    ]


SETS = {"dev": samples, "heldout": heldout_samples}


def registry(tmp: Path, version: int) -> PromptRegistry:
    """평가할 틀 판과 재료 분석 CURRENT 를 CURRENT 로 둔 임시 프롬프트 폴더."""
    for task, v in ((TASK, version), (ANALYSIS_TASK, None)):
        src = REPO / "prompts" / task
        ver = v if v is not None else int((src / "CURRENT").read_text().strip().lstrip("v"))
        dst = tmp / "prompts" / task
        dst.mkdir(parents=True)
        shutil.copy(src / f"v{ver}.md", dst / f"v{ver}.md")
        (dst / "CURRENT").write_text(f"v{ver}\n")
    return PromptRegistry(tmp / "prompts")


def _hits(text: str, words: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in words)


_MARK = re.compile(r"^\s*\[[^\]]*\]\s*")


def _key(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", _MARK.sub("", text).lower())


def design_index(text: str, designs: list[str]) -> int:
    """틀이 옮겨 적은 설계가 재료의 몇 번째 허용 설계인지 — 앞부분 글자가 가장 길게 겹치는 것 (8자 미만이면 -1)."""
    k = _key(text)
    best, idx = 0, -1
    for i, d in enumerate(designs):
        n = len(os.path.commonprefix([k, _key(d)]))
        if n > best:
            best, idx = n, i
    return idx if best >= 8 else -1


@dataclass
class Row:
    sample: str
    run: int
    trap: bool
    kind: str = ""
    reason: str = ""
    storage: list[str] = field(default_factory=list)
    stateful: bool = False
    flagged: list[int] = field(
        default_factory=list
    )  # yes 가 아닌 설계 번호 (-1 = 재료 설계와 못 맞춤)
    designs: list[str] = field(default_factory=list)  # 'd번호 holds: 이유' — 읽기용
    kind_ok: bool = False
    reason_ok: bool = False
    storage_ok: bool = False
    stateful_ok: bool = False
    design_ok: bool = False
    usable: bool = False
    hidden_kept: bool = False
    entity_ok: bool = False
    error: str = ""
    hidden_requirement: str = ""
    forbidden_terms: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


def score(s: Sample, frame: TrapFrame, seed: FeatureSeed, template: Any, run: int) -> Row:
    checks = [(design_index(d.design, seed.accepted_designs), d) for d in frame.design_matrix]
    flagged = sorted({i for i, d in checks if d.holds != "yes"})
    held = {i for i, d in checks if d.holds == "yes"}
    storage_hit = any(_hits(x.entity, s.storage_words) for x in frame.forbidden_storage)
    if s.trap:
        storage_ok = storage_hit
        # 업무 요구를 못 지키는 설계를 걸러 내고, 지킬 수 있는 설계는 하나 이상 남긴다 (모두 거르면 과제가 성립하지 않는다)
        design_ok = s.bad_design in flagged and bool(held - {s.bad_design, -1})
    else:
        storage_ok = not storage_hit
        design_ok = not any(d.holds == "no" for d in frame.design_matrix)
    usable = not frame_problems(frame, template)
    if usable:
        try:
            framed, _ = apply_frame(seed, frame)
            FeatureSeed.model_validate(framed.model_dump())
        except ValueError:
            usable = False
    return Row(
        sample=s.name,
        run=run,
        trap=s.trap,
        kind=frame.kind,
        reason=frame.reason,
        storage=[f"{x.entity} ({x.why})" for x in frame.forbidden_storage],
        stateful=frame.stateful_target_forbidden,
        flagged=flagged,
        designs=[f"d{i} {d.holds}: {d.why}" for i, d in checks],
        kind_ok=frame.kind == "design",
        reason_ok=not s.reasons or frame.reason in s.reasons,
        storage_ok=storage_ok,
        stateful_ok=frame.stateful_target_forbidden == s.trap,
        design_ok=design_ok,
        usable=usable,
        hidden_kept=frame.kind != "design" or keeps_hidden(seed, frame),
        entity_ok=all(
            re.fullmatch(r"[A-Z][A-Za-z0-9]*", x.entity) for x in frame.forbidden_storage
        ),
        hidden_requirement=frame.hidden_requirement,
        forbidden_terms=list(frame.forbidden_terms),
        gaps=list(frame.gaps),
    )


CHECKS = (
    "kind_ok",
    "reason_ok",
    "storage_ok",
    "stateful_ok",
    "design_ok",
    "usable",
    "hidden_kept",
    "entity_ok",
)


async def evaluate(
    version: int, runs: int, template_version: int, sample_set: str = "dev"
) -> list[Row]:
    settings = Settings()  # type: ignore[call-arg]
    llm = build_llm_client(settings)
    template = load_template(JAVA, template_version)  # 운영 인터뷰가 넘기는 템플릿 정보
    rows: list[Row] = []
    with tempfile.TemporaryDirectory(prefix="trap-frame-eval-") as tmp:
        prompts = registry(Path(tmp), version)
        pseudo = InterviewPseudonymizer(
            Detectors(load_dictionaries(), use_kiwi=False), Path(tmp) / "mappings"
        )
        for run in range(runs):
            for s in SETS[sample_set](settings.incidents_dir):
                assert s.incident.feature is not None
                mapping: RunMapping = pseudo.new_mapping(f"int-{uuid.uuid4().hex}", "가상회사")
                cost = 0.0
                try:
                    analysis, prov_a = await analyze_material(
                        pseudo.stored_json(
                            _pseudonymized(material_block(s.incident), pseudo, mapping), mapping
                        ),
                        template,
                        llm=llm,
                        prompts=prompts,
                        ontology_version="onto-eval",
                    )
                    cost += float(prov_a.cost_usd or 0.0)
                    block = _pseudonymized(frame_block(s.incident, analysis), pseudo, mapping)
                    frame, prov = await make_frame(
                        pseudo.stored_json(block, mapping),
                        template,
                        llm=llm,
                        prompts=prompts,
                        ontology_version="onto-eval",
                    )
                    cost += float(prov.cost_usd or 0.0)
                    for text in frame_texts(frame):  # 운영과 같은 가명화 검사
                        pseudo.derived(text, mapping)
                    row = score(s, frame, s.incident.feature, template, run)
                except Exception as exc:  # noqa: BLE001 — 평가는 실패도 한 줄로 센다
                    row = Row(sample=s.name, run=run, trap=s.trap, error=type(exc).__name__)
                row.cost_usd = cost
                rows.append(row)
                ok = "".join("o" if getattr(row, k) else "x" for k in CHECKS)
                print(
                    f"[{run}] {s.name}: {ok} (종류·사유·금지엔티티·상태대상·설계·사용·숨은요구·이름) 금지 {row.storage} · "
                    f"상태 {row.stateful} · 걸러낸 설계 {row.flagged}{' · 오류 ' + row.error if row.error else ''}"
                )
    return rows


def _stable(rows: list[Row]) -> float:
    by: dict[str, set[tuple[Any, ...]]] = {}
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.sample] = counts.get(r.sample, 0) + 1
        by.setdefault(r.sample, set()).add((r.storage_ok, r.stateful, tuple(r.flagged)))
    many = [s for s, n in counts.items() if n > 1]
    if not many:
        return 1.0
    return round(sum(len(by[s]) == 1 for s in many) / len(many), 3)


def summary(rows: list[Row]) -> dict[str, Any]:
    n = len(rows) or 1
    return {
        "n": len(rows),
        **{k: round(sum(getattr(r, k) for r in rows) / n, 3) for k in CHECKS},
        "stable": _stable(rows),
        "errors": sum(1 for r in rows if r.error),
        "cost_usd": round(sum(r.cost_usd for r in rows), 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--version", type=int, default=2, help="평가할 trap_frame 판")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--template", type=int, help=f"{JAVA} 판 (없으면 CURRENT)")
    ap.add_argument(
        "--set",
        choices=sorted(SETS),
        default="dev",
        help="표본 묶음 — dev: 프롬프트를 고칠 때 본 것, heldout: 보지 않은 가상 재료",
    )
    ap.add_argument(
        "--out", type=Path, help="결과 폴더 (git 밖, 예: data/eval/interview/trap_frame)"
    )
    a = ap.parse_args()
    tv = a.template if a.template is not None else current_version(JAVA)
    rows = asyncio.run(evaluate(a.version, a.runs, tv, a.set))
    s = {"prompt": f"{TASK}@{a.version}", "template": f"{JAVA}@v{tv}", **summary(rows)}
    print(json.dumps(s, ensure_ascii=False))
    if a.out is not None:
        out = a.out.resolve()
        if out.is_relative_to(REPO) and not out.is_relative_to(REPO / "data"):
            raise SystemExit(f"결과 폴더는 git 밖(레포의 data/ 아래)이어야 합니다: {a.out}")
        a.out.mkdir(parents=True, exist_ok=True)
        suffix = "" if a.set == "dev" else f"_{a.set}"
        path = a.out / f"{date.today().isoformat()}_v{a.version}_t{tv}{suffix}_r2.json"
        path.write_text(
            json.dumps(
                {"summary": s, "rows": [r.__dict__ for r in rows]}, ensure_ascii=False, indent=1
            ),
            "utf-8",
        )
        print(f"결과: {path}")


if __name__ == "__main__":
    main()
