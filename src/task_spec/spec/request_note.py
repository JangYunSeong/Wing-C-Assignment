"""상황 끝에 코드가 붙이는 자료·질문 안내 한 문장 — 만드는 곳은 여기 하나 (#184).

기능형 서술 채우기(spec.feature.fill_from_seed_v4)가 1단계·2단계 상황 끝에 붙이고, 지원자 README 는 같은 내용을 '진행 방식' 절에
따로 쓰므로 상황에서는 이 문장을 그대로 뗀다(starter.render.public_scenario). 예전엔 README 쪽이 정규식으로 다시 떼어, 문장을 한 글자만
바꿔도 README 에 안내가 두 번 나왔다. 저장·전달되는 상황 글(승인 화면·진행자 묶음 2단계 scenario)은 그대로다.
"""

from __future__ import annotations


def request_note(max_requests: int, max_questions: int | None = None) -> str:
    """'아래 자료 중 최대 N개를 요청해 볼 수 있습니다.' — 질문 한도가 있으면(1단계) '…있고, 질문은 최대 M개까지 할 수 있습니다.'
    앞의 빈 줄까지 포함한다 (상황 본문과 문단을 나눈다)."""
    ask = f"아래 자료 중 최대 {max_requests}개를 요청해 볼 수 있"
    if max_questions is None:
        return f"\n\n{ask}습니다."
    return f"\n\n{ask}고, 질문은 최대 {max_questions}개까지 할 수 있습니다."
