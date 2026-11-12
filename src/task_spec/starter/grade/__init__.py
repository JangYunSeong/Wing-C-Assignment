"""채점용 이미지의 `grade` 명령 — 스택마다 한 파일 (Java `image_grade` · Node `image_grade_node` · Python `image_grade_py`).

각 파일은 이미지 안에 `/grader/grade.py` 로 복사돼 단독으로 돈다 — **표준 라이브러리만** 쓰고 `task_spec` 을 import 하지 않는다.
Node·Python 검증기(`starter/verify/`)는 같은 판정 함수를 여기서 import 한다 (판정 코드 한 벌). 2026-11-12 eval/starter 에서 옮김 (#126).
"""
