"""과제 생성 builder (#129) — 큐(#128)에서 생성 요청을 하나씩 가져가 공통 과제 하나를 review 까지 만든다.

계약(plan_contract) → 시작 코드·테스트·참조 구현(build_and_repair, 생성 코드는 격리 컨테이너 #127) → 응시용·채점용 이미지·검증
→ 빌드 저장소 적재(승인 전 묶음, approval=pending). 승인은 사람이 그래프 interrupt 로 (#130). API 는 빌드하지 않는다 (#115).

    uv run python -m task_spec.builder [--once]
"""
