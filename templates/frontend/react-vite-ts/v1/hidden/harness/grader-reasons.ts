// 의도한 실패 사유 — 단언 메시지 앞에 붙는 태그 `[reason:<코드>]` (verifier 가 실행 보고서에서 찾는다, manifest failure_reasons).
// 사유는 그 실패만 걸리는 단언에 붙인다 — 모든 실패를 한꺼번에 보는 단언에 붙이면 어떤 실패든 그 사유로 보인다.

/** `expect(값, reason('stale_response_shown', '입력은 "노트" 인데 목록이 바뀜')).toEqual(...)` */
export function reason(code: string, description: string): string {
  return `[reason:${code}] ${description}`;
}

/** 사유 코드만 (메시지 없이). */
export function tag(code: string): string {
  return `[reason:${code}]`;
}
