"""D2 평가 표본 (#266) — 전부 가상. 실존 회사·인물·연락처 없음 (이름은 '김가상' 같은 가상 표기, 회사는 '가상테크' 등).

1) 기능형 재료(interview_feature_seed): 확인된 사례·처음 시도한 대응·기준 답안. 원문(가상 이름 포함)은 실행기가 실제 탐지기로 가명화한다.
   심은 값: reasons(템플릿 실패 사유 중 맞는 것 — 하나 이상 들어가야 함), perf(성능 사례인가 — 아니면 harness 를 비워야 함),
   concept(숨은 요구에 들어가야 할 개념 — 하나 이상), need_forbidden(업무 목적 business_need 에 나오면 안 되는 원인·해법 낱말).
2) 답할 범위 초안(interview_targeted_answer): 지원자에게 보이는 과제 글·짚은 조건·담당자가 적은 예시 질문.
   심은 값: solution(답에 나오면 안 되는 해법 낱말), pointer(가리켜야 할 자료 제목 — 하나 이상).

dev 는 고칠 때 보고, test 는 사람이 정한 때 한 번만 돌리며 내용을 보지 않는다 (eval-harness).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Split = Literal["dev", "test"]


@dataclass(frozen=True)
class SeedCase:
    id: str
    split: Split
    sub: str
    company: str
    title: str
    symptom: str
    naive_fix: str
    root_cause: str
    why_naive_fails: str
    key_insight: str
    deep_structure: str
    accepted_fixes: tuple[str, ...]
    interview_text: str
    answer_text: str
    trap_text: str
    trap_answer_text: str
    reasons: frozenset[str]
    perf: bool
    concept: tuple[str, ...]
    need_forbidden: tuple[str, ...]
    tech: tuple[str, ...] = ()
    surface: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnswerCase:
    id: str
    split: Split
    question: str
    hidden_condition: str
    task: dict[str, object]
    solution: tuple[str, ...]
    pointer: tuple[str, ...]
    tags: tuple[str, ...] = field(default_factory=tuple)


def _answer(repro: str, success: str, keep: str, check: str, fixes: str) -> str:
    return (
        f"[재현 조건] {repro}\n[성공 기준] {success}\n[지켜야 할 조건] {keep}\n"
        f"[확인 방법] {check}\n[인정할 해법과 조건] {fixes}"
    )


def _trap(judgment: str, why: str, right: str) -> str:
    return f"[실패한 판단] {judgment}\n[실패를 가르는 근거] {why}\n[올바른 판단 기준] {right}"


SEED_CASES: tuple[SeedCase, ...] = (
    # ---------------------------------------------------------------- dev
    SeedCase(
        id="seed-dev-01",
        split="dev",
        sub="sw.backend",
        company="가상테크",
        title="결제 승인 응답 유실 뒤 재시도로 주문이 두 번 생김",
        symptom="고객 한 명의 주문 내역에 같은 상품 주문이 두 건 생겼고 결제 승인 기록도 두 건이었다",
        naive_fix="클라이언트 재시도를 끄고 요청 시간 제한을 늘렸다",
        root_cause="승인 응답이 네트워크에서 유실되자 클라이언트가 같은 주문 요청을 다시 보냈고 서버는 요청을 구별하지 못해 새 주문을 만들었다",
        why_naive_fails="재시도를 끄면 실제 통신 실패에서 주문이 영영 처리되지 않는다",
        key_insight="같은 요청의 재전송을 서버가 알아볼 수 있어야 한다",
        deep_structure="불확실한 실패 뒤 재시도가 같은 작업을 두 번 적용한다",
        accepted_fixes=(
            "요청 식별자로 이미 처리한 주문이면 이전 결과를 돌려준다",
            "주문 번호 유일 제약으로 두 번째 생성을 막는다",
        ),
        interview_text="김가상 대리가 고객 문의로 같은 주문이 두 건인 것을 확인했다. 승인 응답이 늦게 도착한 구간에서만 생겼고, 앱은 응답이 없으면 3초 뒤 같은 요청을 다시 보낸다.",
        answer_text=_answer(
            "승인 응답 유실 뒤 같은 주문 요청을 다시 보낸다",
            "같은 요청을 여러 번 받아도 주문과 결제 기록은 한 건만 남는다",
            "실제 통신 실패 뒤 재시도로 주문을 완료할 수 있어야 한다",
            "같은 요청을 두 번 보내고 주문·결제 기록 수를 대조한다",
            "요청 식별자 확인 또는 주문 번호 유일 제약 — 두 번째 요청에 첫 결과를 돌려줄 것",
        ),
        trap_text="처음에는 앱의 자동 재시도를 껐다.",
        trap_answer_text=_trap(
            "재시도 금지", "통신 실패 뒤 주문이 완료되지 않는다", "중복을 막으면서 재시도는 살린다"
        ),
        reasons=frozenset({"duplicate_record"}),
        perf=False,
        concept=("중복", "한 건", "두 번"),
        need_forbidden=("멱등", "요청 식별자", "유일 제약", "재전송"),
        tech=("Java", "Spring"),
    ),
    SeedCase(
        id="seed-dev-02",
        split="dev",
        sub="sw.backend",
        company="예시물산",
        title="재고 차감이 동시에 들어오면 수량이 덜 줄어듦",
        symptom="한정 수량 상품이 판매 수량보다 재고가 많게 남았다",
        naive_fix="재고 조회 결과를 캐시해 응답을 빠르게 했다",
        root_cause="두 요청이 같은 재고 값을 읽고 각각 1을 빼서 저장해 한쪽 차감이 덮어써졌다",
        why_naive_fails="캐시는 오래된 값을 더 오래 보여 줘 덮어쓰기를 늘린다",
        key_insight="읽고-계산하고-쓰는 사이에 다른 갱신이 끼면 결과를 잃는다",
        deep_structure="동시 갱신에서 마지막 쓰기가 앞 쓰기를 지운다",
        accepted_fixes=(
            "버전 칼럼으로 갱신 충돌을 감지해 다시 시도한다",
            "갱신 문에서 재고를 원자적으로 뺀다",
        ),
        interview_text="박예시 과장이 행사 때 재고가 실제 판매보다 많이 남은 것을 발견했다. 같은 상품에 주문이 몰린 시각에만 어긋났다.",
        answer_text=_answer(
            "같은 상품의 재고를 두 요청이 동시에 줄인다",
            "동시 주문 뒤 남은 재고는 처음 수량에서 주문 수를 뺀 값과 같다",
            "주문이 하나뿐일 때의 처리 결과는 바뀌지 않는다",
            "같은 상품에 주문을 동시에 보내고 남은 재고를 대조한다",
            "버전 충돌 감지 후 재시도 또는 원자적 차감 — 차감을 잃지 않을 것",
        ),
        trap_text="처음에는 재고 조회를 캐시했다.",
        trap_answer_text=_trap("조회 캐시", "오래된 값이 덮어쓰기를 늘린다", "갱신 충돌을 막는다"),
        reasons=frozenset({"lost_update"}),
        perf=False,
        concept=("동시", "덮어", "잃"),
        need_forbidden=("버전", "원자적", "락", "충돌 감지"),
        tech=("Java", "JPA"),
    ),
    SeedCase(
        id="seed-dev-03",
        split="dev",
        sub="sw.frontend",
        company="가상테크",
        title="검색어를 빨리 바꾸면 이전 검색 결과가 보임",
        symptom="검색어를 '가방'에서 '모자'로 바꿨는데 화면에 가방 결과가 남았다",
        naive_fix="입력 지연(디바운스)을 길게 늘렸다",
        root_cause="먼저 보낸 요청의 응답이 늦게 도착해 나중 요청 결과를 덮었다",
        why_naive_fails="지연을 늘려도 네트워크 응답 순서는 보장되지 않는다",
        key_insight="화면은 마지막 요청의 응답만 보여야 한다",
        deep_structure="비동기 응답 순서가 요청 순서와 다를 때 낡은 결과가 이긴다",
        accepted_fixes=("요청마다 순번을 두고 마지막 요청 응답만 반영한다", "이전 요청을 취소한다"),
        interview_text="김가상 님이 고객 화면 녹화에서 검색어와 결과가 다른 순간을 찾았다. 느린 회선에서 자주 보였다.",
        answer_text=_answer(
            "첫 검색 응답을 늦게 오게 하고 검색어를 바꾼다",
            "화면에는 마지막 검색어의 결과만 남는다",
            "응답이 정상 순서로 오면 지금처럼 보인다",
            "응답 순서를 뒤바꿔 보내고 화면 결과를 대조한다",
            "마지막 요청만 반영 또는 이전 요청 취소 — 낡은 응답을 보이지 않을 것",
        ),
        trap_text="처음에는 입력 지연을 늘렸다.",
        trap_answer_text=_trap(
            "디바운스 증가",
            "응답 순서는 그대로 뒤바뀐다",
            "응답 순서와 상관없이 마지막 요청만 반영한다",
        ),
        reasons=frozenset({"stale_response_shown"}),
        perf=False,
        concept=("마지막", "늦게", "이전"),
        need_forbidden=("취소", "순번", "AbortController", "경합"),
        tech=("React", "TypeScript"),
    ),
    SeedCase(
        id="seed-dev-04",
        split="dev",
        sub="sw.frontend",
        company="예시물산",
        title="좋아요 버튼 실패 뒤에도 눌린 상태가 남음",
        symptom="서버가 오류를 냈는데 화면의 좋아요는 눌린 채로 남았다",
        naive_fix="버튼을 누르면 서버 응답을 기다린 뒤에만 바꾸도록 했다",
        root_cause="낙관적으로 바꾼 화면 상태를 실패 응답 뒤 되돌리지 않았다",
        why_naive_fails="응답을 기다리면 느린 회선에서 버튼이 반응하지 않는 것처럼 보여 사용자가 여러 번 누른다",
        key_insight="빠른 반응은 살리고 실패하면 원래 상태로 돌린다",
        deep_structure="낙관적 갱신 뒤 실패 경로의 복구 누락",
        accepted_fixes=("실패하면 이전 상태로 되돌리고 알린다", "서버 결과로 다시 맞춘다"),
        interview_text="박예시 님이 장애 시간대 문의에서 좋아요 수와 실제 수가 다른 것을 확인했다.",
        answer_text=_answer(
            "좋아요 요청이 실패하게 만든다",
            "실패하면 버튼과 수가 누르기 전으로 돌아간다",
            "성공하면 바로 눌린 상태가 보인다",
            "실패 응답을 주고 화면 상태를 대조한다",
            "실패 시 되돌리기 또는 서버 값으로 다시 맞추기 — 실패 뒤 눌린 상태를 남기지 않을 것",
        ),
        trap_text="처음에는 서버 응답을 기다린 뒤에만 화면을 바꿨다.",
        trap_answer_text=_trap(
            "응답 대기", "반응이 느려 중복 클릭이 생긴다", "빠른 반응을 살리고 실패를 되돌린다"
        ),
        reasons=frozenset({"optimistic_not_rolled_back"}),
        perf=False,
        concept=("되돌", "실패", "원래"),
        need_forbidden=("낙관적", "롤백", "되돌리"),
        tech=("React",),
    ),
    SeedCase(
        id="seed-dev-05",
        split="dev",
        sub="data.eng",
        company="가상테크",
        title="일 배치를 다시 돌리면 매출이 두 번 집계됨",
        symptom="실패한 일 배치를 다시 돌린 날 매출 합계가 두 배였다",
        naive_fix="다시 돌리기 전에 담당자가 결과 표를 직접 지웠다",
        root_cause="배치가 결과 표에 덧붙이기만 해서 같은 날짜를 다시 돌리면 행이 겹쳤다",
        why_naive_fails="사람이 지우는 단계는 빠뜨리기 쉽고 일부만 지우면 더 어긋난다",
        key_insight="같은 날짜를 몇 번 돌려도 결과가 같아야 한다",
        deep_structure="재실행이 결과를 누적시킨다",
        accepted_fixes=("날짜 단위로 결과를 지우고 다시 쓴다", "날짜·주문 키로 덮어쓴다"),
        interview_text="김가상 님이 월말 보고에서 하루 매출이 두 배인 것을 찾았다. 그날 새벽 배치가 실패해 아침에 다시 돌렸다.",
        answer_text=_answer(
            "같은 날짜 배치를 두 번 돌린다",
            "다시 돌려도 그날 매출 합계와 행 수가 한 번 돌린 것과 같다",
            "다른 날짜의 결과는 바뀌지 않는다",
            "두 번 돌린 뒤 합계와 행 수를 대조한다",
            "날짜 단위 교체 또는 키 기준 덮어쓰기 — 다시 돌려도 같은 결과일 것",
        ),
        trap_text="처음에는 다시 돌리기 전에 사람이 표를 지웠다.",
        trap_answer_text=_trap(
            "수동 삭제", "빠뜨리거나 일부만 지운다", "다시 돌려도 같은 결과가 나오게 한다"
        ),
        reasons=frozenset({"rerun_not_idempotent", "duplicate_counted"}),
        perf=False,
        concept=("다시", "같은 결과", "두 번", "중복"),
        need_forbidden=("멱등", "덮어쓰", "교체", "MERGE"),
        tech=("Python", "DuckDB"),
    ),
    SeedCase(
        id="seed-dev-06",
        split="dev",
        sub="data.eng",
        company="예시물산",
        title="늦게 들어온 주문이 집계에서 빠짐",
        symptom="전날 주문 일부가 다음 날 오전에야 들어와 전날 매출에서 빠졌다",
        naive_fix="배치 시작 시각을 한 시간 늦췄다",
        root_cause="배치가 실행 시점까지 들어온 행만 집계하고 늦게 도착한 전날 주문을 다시 보지 않았다",
        why_naive_fails="시각을 늦춰도 그보다 늦게 오는 주문은 계속 빠진다",
        key_insight="주문 날짜 기준으로 늦게 온 행을 다시 반영해야 한다",
        deep_structure="도착 시각과 사건 시각이 다를 때 늦은 데이터가 빠진다",
        accepted_fixes=("최근 며칠을 다시 집계한다", "늦게 온 행만 골라 그 날짜 결과를 고친다"),
        interview_text="박예시 님이 정산 대조에서 전날 매출이 주문 시스템보다 적은 것을 확인했다. 늦게 동기화되는 매장이 있었다.",
        answer_text=_answer(
            "전날 주문 일부를 다음 날 들어오게 한다",
            "늦게 온 주문도 주문 날짜의 매출에 들어간다",
            "이미 맞는 날짜의 결과는 바뀌지 않는다",
            "늦게 온 주문을 넣고 다시 돌린 뒤 날짜별 합계를 대조한다",
            "최근 기간 재집계 또는 늦은 행 반영 — 늦은 주문을 빠뜨리지 않을 것",
        ),
        trap_text="처음에는 배치 시각을 늦췄다.",
        trap_answer_text=_trap(
            "시각 늦추기",
            "더 늦게 오는 주문은 여전히 빠진다",
            "주문 날짜 기준으로 늦은 행을 반영한다",
        ),
        reasons=frozenset({"late_data_missed"}),
        perf=False,
        concept=("늦게", "늦은", "빠지", "빠뜨"),
        need_forbidden=("재집계", "워터마크", "늦게 온 행"),
        tech=("Python",),
    ),
    # ---------------------------------------------------------------- test (내용을 보지 않는다)
    SeedCase(
        id="seed-test-01",
        split="test",
        sub="sw.backend",
        company="가상테크",
        title="쿠폰을 두 번 눌러 할인이 두 번 들어감",
        symptom="쿠폰 한 장으로 할인이 두 번 적용된 주문이 생겼다",
        naive_fix="버튼을 한 번 누르면 비활성화하도록 화면을 고쳤다",
        root_cause="같은 쿠폰 사용 요청이 거의 동시에 두 번 들어와 둘 다 사용 가능으로 판단했다",
        why_naive_fails="화면을 고쳐도 다른 경로(앱·재전송)의 중복 요청은 그대로 들어온다",
        key_insight="쿠폰 사용은 서버에서 한 번만 성공해야 한다",
        deep_structure="확인과 사용 사이에 다른 요청이 끼어 같은 자원을 두 번 쓴다",
        accepted_fixes=("쿠폰 사용 기록에 유일 제약을 둔다", "사용 상태를 조건부 갱신으로 바꾼다"),
        interview_text="김가상 님이 정산에서 할인 금액이 쿠폰 발행 수보다 많은 것을 발견했다.",
        answer_text=_answer(
            "같은 쿠폰 사용 요청을 동시에 두 번 보낸다",
            "쿠폰 할인은 한 번만 적용된다",
            "서로 다른 쿠폰은 각각 정상 적용된다",
            "동시에 두 번 보내고 사용 기록과 할인 수를 대조한다",
            "유일 제약 또는 조건부 갱신 — 두 번째 사용은 실패로 알릴 것",
        ),
        trap_text="처음에는 화면 버튼만 막았다.",
        trap_answer_text=_trap(
            "버튼 비활성화", "다른 경로의 중복 요청이 남는다", "서버에서 한 번만 성공하게 한다"
        ),
        reasons=frozenset({"duplicate_record", "lost_update"}),
        perf=False,
        concept=("한 번", "중복", "두 번"),
        need_forbidden=("유일 제약", "조건부 갱신", "락"),
    ),
    SeedCase(
        id="seed-test-02",
        split="test",
        sub="sw.backend",
        company="예시물산",
        title="정산 저장 중 일부만 실패했는데 성공으로 보임",
        symptom="정산 결과 화면에는 완료로 나왔지만 일부 매장의 정산 행이 없었다",
        naive_fix="저장 실패를 로그로만 남기고 계속 진행했다",
        root_cause="여러 매장 정산을 차례로 저장하다 한 곳이 실패해도 전체를 성공으로 표시했다",
        why_naive_fails="로그만 남기면 아무도 실패를 알아채지 못하고 빠진 정산이 그대로 지급된다",
        key_insight="일부 실패가 성공으로 보이면 안 된다",
        deep_structure="부분 실패를 삼켜 성공으로 보고한다",
        accepted_fixes=(
            "한 트랜잭션으로 묶어 전부 되돌린다",
            "실패한 매장을 결과에 표시하고 재처리한다",
        ),
        interview_text="박예시 님이 매장 문의로 정산 누락을 확인했다. 그날 저장소 연결이 잠깐 끊겼다.",
        answer_text=_answer(
            "여러 매장 정산 중 하나의 저장을 실패하게 만든다",
            "실패가 있으면 성공으로 표시하지 않는다",
            "모두 성공하면 지금처럼 완료로 보인다",
            "저장 실패를 주입하고 결과 상태와 정산 행을 대조한다",
            "전체 되돌리기 또는 실패 매장 표시 — 빠진 정산을 숨기지 않을 것",
        ),
        trap_text="처음에는 실패를 로그로만 남겼다.",
        trap_answer_text=_trap("로그만 남김", "실패를 아무도 모른다", "실패를 결과에 드러낸다"),
        reasons=frozenset({"rollback_left_success", "injected_failure_ignored", "record_missing"}),
        perf=False,
        concept=("실패", "성공", "빠진", "누락"),
        need_forbidden=("트랜잭션", "롤백", "되돌"),
    ),
    SeedCase(
        id="seed-test-03",
        split="test",
        sub="sw.frontend",
        company="가상테크",
        title="로그아웃 뒤 다른 사람 장바구니가 보임",
        symptom="공용 기기에서 로그아웃 후 다른 사용자로 들어가자 이전 사용자의 장바구니가 잠깐 보였다",
        naive_fix="로그아웃 버튼에 새로고침을 붙였다",
        root_cause="이전 사용자 데이터를 메모리에 둔 채 다음 사용자 화면을 그렸다",
        why_naive_fails="새로고침 전에 그려지는 순간과 다른 로그아웃 경로가 남는다",
        key_insight="사용자가 바뀌면 이전 사용자 데이터는 보이면 안 된다",
        deep_structure="세션 전환 때 이전 사용자 상태가 남는다",
        accepted_fixes=("사용자가 바뀌면 저장된 데이터를 비운다", "사용자 키별로 데이터를 나눈다"),
        interview_text="김가상 님이 매장 기기 점검에서 다른 고객 장바구니가 보인다는 신고를 받았다.",
        answer_text=_answer(
            "한 사용자로 장바구니를 채운 뒤 로그아웃하고 다른 사용자로 들어간다",
            "다른 사용자에게 이전 사용자의 장바구니가 보이지 않는다",
            "같은 사용자가 다시 들어오면 자기 장바구니가 보인다",
            "사용자를 바꾸고 화면에 보인 항목을 대조한다",
            "사용자 전환 때 비우기 또는 사용자별 저장 — 남의 데이터를 보이지 않을 것",
        ),
        trap_text="처음에는 로그아웃 때 새로고침만 붙였다.",
        trap_answer_text=_trap(
            "새로고침", "그려지는 순간과 다른 경로가 남는다", "사용자 전환 때 데이터를 비운다"
        ),
        reasons=frozenset({"other_user_data_shown", "session_lost"}),
        perf=False,
        concept=("다른 사용자", "이전 사용자", "남의"),
        need_forbidden=("비운다", "캐시 초기화", "사용자 키"),
    ),
    SeedCase(
        id="seed-test-04",
        split="test",
        sub="sw.frontend",
        company="예시물산",
        title="오프라인에서 한 메모 수정이 다시 연결되면 사라짐",
        symptom="지하철에서 고친 메모가 연결이 돌아온 뒤 예전 내용으로 돌아갔다",
        naive_fix="오프라인이면 편집을 막았다",
        root_cause="오프라인 변경을 보관하지 않고 연결되자 서버 데이터로 덮었다",
        why_naive_fails="편집을 막으면 현장 직원이 메모를 쓸 수 없다",
        key_insight="오프라인 변경을 잃지 않고 한 번만 반영해야 한다",
        deep_structure="연결 복구 때 로컬 변경이 서버 값에 덮인다",
        accepted_fixes=(
            "변경을 대기열에 두고 연결되면 보낸다",
            "변경마다 키를 두어 중복 반영을 막는다",
        ),
        interview_text="박예시 님이 현장 직원의 메모 분실 신고를 받았다. 연결이 끊긴 구간에서만 생겼다.",
        answer_text=_answer(
            "오프라인에서 메모를 고친 뒤 다시 연결한다",
            "고친 내용이 한 번만 서버에 반영되고 화면에 남는다",
            "온라인 편집은 지금처럼 바로 저장된다",
            "연결을 끊고 고친 뒤 다시 연결해 서버와 화면을 대조한다",
            "대기열 후 전송, 변경 키로 중복 방지 — 변경을 잃거나 두 번 반영하지 않을 것",
        ),
        trap_text="처음에는 오프라인 편집을 막았다.",
        trap_answer_text=_trap(
            "편집 막기", "현장에서 메모를 못 쓴다", "변경을 보관했다가 한 번만 반영한다"
        ),
        reasons=frozenset({"offline_change_lost", "offline_change_duplicated"}),
        perf=False,
        concept=("오프라인", "잃", "한 번"),
        need_forbidden=("대기열", "큐", "멱등"),
    ),
    SeedCase(
        id="seed-test-05",
        split="test",
        sub="data.eng",
        company="가상테크",
        title="적재 중간에 실패하면 반쯤 쓴 결과가 보고서에 보임",
        symptom="아침 보고서에 일부 지역 매출만 들어간 숫자가 보였다",
        naive_fix="실패하면 담당자에게 메일만 보내도록 했다",
        root_cause="결과 표에 지역별로 차례로 쓰다 중간에 실패해 반쯤 쓴 상태가 그대로 보였다",
        why_naive_fails="메일이 오기 전에 보고서를 본 사람은 틀린 숫자를 쓴다",
        key_insight="보고서는 완성된 결과만 봐야 한다",
        deep_structure="부분 적재가 소비자에게 보인다",
        accepted_fixes=("임시 표에 다 쓴 뒤 한 번에 바꾼다", "완료 표시가 있는 결과만 보이게 한다"),
        interview_text="김가상 님이 지역 매출 합계가 전국 합계보다 적은 보고서를 발견했다.",
        answer_text=_answer(
            "적재를 중간에 실패하게 만든다",
            "보고서에는 이전 완성본이나 새 완성본만 보인다",
            "성공하면 새 결과가 보인다",
            "중간 실패를 주입하고 보고서가 읽는 결과를 대조한다",
            "임시 표 후 교체 또는 완료 표시 — 반쯤 쓴 결과를 보이지 않을 것",
        ),
        trap_text="처음에는 실패 메일만 보냈다.",
        trap_answer_text=_trap(
            "메일 알림", "그 전에 본 사람이 틀린 숫자를 쓴다", "완성본만 보이게 한다"
        ),
        reasons=frozenset({"partial_output_visible"}),
        perf=False,
        concept=("반쯤", "완성", "일부", "중간"),
        need_forbidden=("임시 표", "교체", "원자적", "스왑"),
    ),
    SeedCase(
        id="seed-test-06",
        split="test",
        sub="data.eng",
        company="예시물산",
        title="같은 주문이 두 원천에서 와서 두 번 집계됨",
        symptom="온라인과 매장 시스템 양쪽에 남은 주문이 매출에 두 번 들어갔다",
        naive_fix="매장 원천을 집계에서 뺐다",
        root_cause="두 원천이 같은 주문을 서로 다른 형식으로 보내고 집계는 주문을 구별하지 않았다",
        why_naive_fails="매장 원천을 빼면 매장에서만 난 주문이 빠진다",
        key_insight="같은 주문은 원천과 상관없이 한 번만 센다",
        deep_structure="여러 원천의 같은 사건을 중복 집계한다",
        accepted_fixes=("주문 번호로 합쳐 한 번만 센다", "원천 우선순위로 하나만 남긴다"),
        interview_text="박예시 님이 매출이 결제 금액보다 많은 날을 찾았다. 매장 픽업 주문이 많던 날이었다.",
        answer_text=_answer(
            "같은 주문을 두 원천에 넣는다",
            "같은 주문은 매출에 한 번만 들어간다",
            "한 원천에만 있는 주문도 빠지지 않는다",
            "두 원천에 같은 주문을 넣고 집계를 대조한다",
            "주문 번호로 합치기 또는 원천 우선순위 — 두 번 세지도 빠뜨리지도 않을 것",
        ),
        trap_text="처음에는 매장 원천을 뺐다.",
        trap_answer_text=_trap("원천 제외", "매장 전용 주문이 빠진다", "같은 주문만 하나로 합친다"),
        reasons=frozenset({"duplicate_counted"}),
        perf=False,
        concept=("한 번", "두 번", "중복", "같은 주문"),
        need_forbidden=("주문 번호로 합", "우선순위", "중복 제거"),
    ),
)


def _task(
    scenario: str, requirements: list[str], artifacts: list[tuple[str, str]]
) -> dict[str, object]:
    """서비스가 초안 프롬프트에 넣는 과제 글과 같은 모양 (상황·공식 요구·자료 제목·내용 — id 없음)."""
    return {
        "scenario": scenario,
        "requirements": [{"text": t} for t in requirements],
        "facts": [],
        "clarifications": [],
        "artifacts": [{"title": t, "content": c} for t, c in artifacts],
    }


ANSWER_CASES: tuple[AnswerCase, ...] = (
    # ---------------------------------------------------------------- dev
    AnswerCase(
        id="ans-dev-01",
        split="dev",
        question="주문 요청이 같은 내용으로 두 번 오면 어떻게 처리해야 하나요?",
        hidden_condition="응답 유실 뒤 같은 주문 요청의 재전송",
        task=_task(
            "가상 쇼핑몰의 주문 생성 API 를 만든다. 결제 승인 뒤 주문을 저장한다.",
            ["주문을 생성하고 주문 번호를 돌려준다", "결제 승인 결과를 함께 저장한다"],
            [
                ("주문 요청 기록", "같은 내용의 요청이 짧은 간격으로 반복된 줄이 있다"),
                ("주문 표 스냅숏", "같은 상품 주문 두 건"),
            ],
        ),
        solution=("멱등", "요청 식별자", "유일 제약", "idempotency"),
        pointer=("주문 요청 기록", "주문 표"),
    ),
    AnswerCase(
        id="ans-dev-02",
        split="dev",
        question="재고가 동시에 줄어들 때 기대하는 결과가 뭔가요?",
        hidden_condition="같은 상품 재고를 동시에 차감하는 요청",
        task=_task(
            "가상 상점의 재고 차감 기능을 만든다. 주문이 들어오면 재고를 줄인다.",
            ["주문 수량만큼 재고를 줄인다", "재고가 모자라면 주문을 거절한다"],
            [
                ("행사 시간대 주문 기록", "같은 상품 주문이 같은 초에 여러 건"),
                ("재고 표", "판매 수보다 재고가 많이 남음"),
            ],
        ),
        solution=("버전", "낙관적 락", "원자적", "SELECT FOR UPDATE"),
        pointer=("주문 기록", "재고 표"),
    ),
    AnswerCase(
        id="ans-dev-03",
        split="dev",
        question="김가상 대리님 말로는 검색 결과가 가끔 틀린다던데 어떤 경우인가요?",
        hidden_condition="먼저 보낸 검색 요청의 응답이 늦게 도착하는 경우",
        task=_task(
            "가상 쇼핑몰 검색 화면을 만든다. 입력할 때마다 결과를 보여 준다.",
            ["검색어 입력에 따라 결과 목록을 보여 준다"],
            [
                ("느린 회선 화면 녹화 메모", "검색어와 결과가 다른 순간이 있다"),
                ("네트워크 기록", "응답 도착 순서가 요청 순서와 다르다"),
            ],
        ),
        solution=("AbortController", "취소", "순번", "최신 요청만"),
        pointer=("네트워크 기록", "화면 녹화"),
        tags=("이름 포함 질문",),
    ),
    AnswerCase(
        id="ans-dev-04",
        split="dev",
        question="배치를 다시 돌리는 상황도 고려해야 하나요?",
        hidden_condition="실패한 일 배치를 같은 날짜로 다시 실행",
        task=_task(
            "가상 매출의 일별 집계 배치를 만든다. 원천 주문을 읽어 날짜별 합계를 쓴다.",
            ["날짜별 매출 합계를 결과 표에 쓴다"],
            [
                ("배치 실행 기록", "같은 날짜가 두 번 실행된 날이 있다"),
                ("월말 보고 메모", "하루 매출이 다른 날의 두 배"),
            ],
        ),
        solution=("멱등", "덮어쓰기", "MERGE", "DELETE 후 INSERT"),
        pointer=("배치 실행 기록", "보고 메모"),
    ),
    AnswerCase(
        id="ans-dev-05",
        split="dev",
        question="좋아요 요청이 실패하면 화면은 어떻게 되어야 하나요?",
        hidden_condition="낙관적으로 바꾼 상태의 실패 뒤 복구",
        task=_task(
            "가상 커뮤니티의 좋아요 버튼을 만든다. 누르면 바로 반영되어 보인다.",
            ["좋아요를 누르면 수와 버튼 상태가 바뀐다"],
            [
                ("오류 시간대 문의 메모", "좋아요 수와 실제 수가 다르다는 문의"),
                ("서버 응답 기록", "좋아요 요청 일부가 오류"),
            ],
        ),
        solution=("롤백", "이전 상태로 되돌", "rollback"),
        pointer=("서버 응답 기록", "문의 메모"),
    ),
    AnswerCase(
        id="ans-dev-06",
        split="dev",
        question="늦게 들어오는 주문이 몇 시간 뒤에 오나요?",
        hidden_condition="전날 주문이 다음 날 늦게 도착",
        task=_task(
            "가상 매장 매출의 일별 집계를 만든다. 매일 새벽에 전날 주문을 집계한다.",
            ["전날 주문의 매출 합계를 낸다"],
            [
                ("동기화 기록", "일부 매장 주문이 다음 날 오전에 들어온 줄"),
                ("정산 대조표", "전날 매출이 주문 시스템보다 적다"),
            ],
        ),
        solution=("워터마크", "재집계", "최근 며칠"),
        pointer=("동기화 기록", "정산 대조표"),
        tags=("자료에 없는 수치 유도",),
    ),
    # ---------------------------------------------------------------- test (내용을 보지 않는다)
    AnswerCase(
        id="ans-test-01",
        split="test",
        question="쿠폰을 동시에 두 번 쓰는 경우도 생각해야 하나요?",
        hidden_condition="같은 쿠폰 사용 요청의 동시 도착",
        task=_task(
            "가상 쇼핑몰의 쿠폰 사용 API 를 만든다.",
            ["쿠폰을 주문에 적용하고 사용 처리한다"],
            [
                ("쿠폰 사용 기록", "같은 쿠폰이 같은 초에 두 번 사용됨"),
                ("정산 메모", "할인 금액이 발행 수보다 많다"),
            ],
        ),
        solution=("유일 제약", "조건부 갱신", "락"),
        pointer=("쿠폰 사용 기록", "정산 메모"),
    ),
    AnswerCase(
        id="ans-test-02",
        split="test",
        question="저장이 일부 실패하면 결과를 어떻게 보여 줘야 하나요?",
        hidden_condition="여러 매장 정산 중 일부 저장 실패",
        task=_task(
            "가상 정산 저장 API 를 만든다. 여러 매장 정산을 한 번에 저장한다.",
            ["매장별 정산을 저장하고 결과를 돌려준다"],
            [
                ("저장소 오류 기록", "정산 중 연결이 잠깐 끊긴 줄"),
                ("매장 문의", "정산 행이 없다는 문의"),
            ],
        ),
        solution=("트랜잭션", "롤백", "rollback"),
        pointer=("오류 기록", "매장 문의"),
    ),
    AnswerCase(
        id="ans-test-03",
        split="test",
        question="박예시 님 기기에서 다른 사람 장바구니가 보였다는데 어떤 조건인가요?",
        hidden_condition="공용 기기에서 사용자 전환",
        task=_task(
            "가상 매장 키오스크의 장바구니 화면을 만든다.",
            ["로그인한 사용자의 장바구니를 보여 준다"],
            [
                ("기기 점검 기록", "로그아웃 직후 다른 사용자 로그인"),
                ("신고 메모", "다른 고객 장바구니가 보였다는 신고"),
            ],
        ),
        solution=("초기화", "비운다", "사용자 키"),
        pointer=("점검 기록", "신고 메모"),
        tags=("이름 포함 질문",),
    ),
    AnswerCase(
        id="ans-test-04",
        split="test",
        question="연결이 끊긴 상태에서 고친 내용은 어떻게 되어야 하나요?",
        hidden_condition="오프라인 변경의 재연결 뒤 반영",
        task=_task(
            "가상 현장 메모 앱을 만든다. 메모를 고치면 서버에 저장한다.",
            ["메모를 고치고 저장한다"],
            [
                ("현장 신고 기록", "연결이 돌아온 뒤 메모가 예전 내용으로 돌아감"),
                ("연결 상태 기록", "지하철 구간에서 연결 끊김"),
            ],
        ),
        solution=("대기열", "큐", "멱등"),
        pointer=("신고 기록", "연결 상태"),
    ),
    AnswerCase(
        id="ans-test-05",
        split="test",
        question="적재가 중간에 실패하면 보고서는 무엇을 보여야 하나요?",
        hidden_condition="지역별 적재 도중 실패",
        task=_task(
            "가상 지역 매출 적재 작업을 만든다. 지역별 결과를 보고서 표에 쓴다.",
            ["지역별 매출을 결과 표에 쓴다"],
            [
                ("적재 실행 기록", "중간 지역에서 실패한 줄"),
                ("보고서 캡처 메모", "지역 합계가 전국보다 적다"),
            ],
        ),
        solution=("임시 표", "스왑", "원자적"),
        pointer=("적재 실행 기록", "보고서"),
    ),
    AnswerCase(
        id="ans-test-06",
        split="test",
        question="두 시스템에 같은 주문이 있으면 몇 번 세나요?",
        hidden_condition="같은 주문이 두 원천에서 도착",
        task=_task(
            "가상 매출 집계 작업을 만든다. 온라인과 매장 원천을 합쳐 집계한다.",
            ["두 원천의 주문을 합쳐 매출을 낸다"],
            [
                ("원천 비교 기록", "같은 주문이 두 원천에 다른 형식으로 있음"),
                ("결제 대조표", "매출이 결제 금액보다 많다"),
            ],
        ),
        solution=("주문 번호로 합", "중복 제거", "우선순위"),
        pointer=("원천 비교", "결제 대조표"),
    ),
)


def seed_cases(split: Split) -> list[SeedCase]:
    return [c for c in SEED_CASES if c.split == split]


def answer_cases(split: Split) -> list[AnswerCase]:
    return [c for c in ANSWER_CASES if c.split == split]
