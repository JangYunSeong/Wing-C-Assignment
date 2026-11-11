// 화면 도우미 — 앱을 그리고 사용자처럼 조작한다. 판정은 공개 계약(화면 요소의 역할·이름·test id·문구)으로만 한다.
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../../App';

import { isContractTestId } from './grader-api';

export { flush } from './grader-api';

/** 계약의 화면 요소 test id — contract.ts 의 TID.X 가 이 타입이다 (v8). 역할 이름('listitem' 등)과 형으로 구별돼,
 *  textsByRole 에 범위를 test id 로 줄 때 어느 인자가 범위인지 짐작하지 않는다. 문자열이라 getByTestId(TID.X) 에도 그대로 쓴다. */
export type TestId = string & { readonly __testId: true };

/** 앱의 주소를 route 로 바꾼다 (renderApp 이 그리기 전에 부른다 — 앱의 라우터가 이 주소의 화면을 연다). */
export function openRoute(route: string): void {
  window.history.replaceState(null, '', route);
}

/** 계약 화면 경로(route, 기본 '/')에서 앱 전체를 그리고 사용자 조작 도구를 준다. 지원자 구현의 내부 모듈은 import 하지 않는다. */
export function renderApp(route = '/') {
  // 앞서 그린 앱은 지우고 새로 그린다 — 한 테스트에서 두 번 불러도 화면에 앱이 한 벌만 (2026-11-09 알파: 두 벌이 그려져 test id 가 겹침)
  cleanup();
  openRoute(route);
  const user = userEvent.setup();
  const view = render(<App />);
  return { user, ...view };
}

/** test id 로 찾기 (없으면 null) — `byTestId(id)` 는 화면 전체에서, `byTestId(범위, id)` 는 그 요소 안에서 (목록 항목 안의 버튼 등). */
export function byTestId(id: string): HTMLElement | null;
export function byTestId(scope: HTMLElement, id: string): HTMLElement | null;
export function byTestId(a: string | HTMLElement, b?: string): HTMLElement | null {
  if (typeof a === 'string') return screen.queryByTestId(a);
  return within(a).queryByTestId(b ?? '');
}

/** 보이는 텍스트를 공백 하나로 접어 돌려준다. */
export function textOf(el: Element | null): string {
  return (el?.textContent ?? '').replace(/\s+/g, ' ').trim();
}

/** 역할이 role 인 요소들의 텍스트 (예: 'option', 'listitem', 'row') — `textsByRole(role)` 은 화면 전체, 범위를 주면 그 요소 안에서.
 *  범위는 요소 또는 계약의 test id (`TID.X`) — `textsByRole('listitem', TID.ORDER_LIST)` · `textsByRole(목록요소, 'listitem')`, 순서는 어느 쪽이든.
 *  범위 요소가 아직 없으면 빈 배열 (판정은 단언이 한다).
 *  v5: 범위(요소)를 받는다 (2026-11-10 알파 fe7b-token) · 순서 무관 (fe7c-token).
 *  v8: 범위를 계약 test id(TestId)로도 받는다 — 알파 fe8-token 이 `textsByRole('listitem', TID.ACCOUNT_LIST)` 로 써 컴파일 실패.
 *  문자열 두 개(`textsByRole('memo-list', 'listitem')`)는 받지 않는다 — 어느 쪽이 역할인지 짐작해야 해서. 계약의 TID 를 쓴다. */
export function textsByRole(role: string): string[];
export function textsByRole(role: string, scope: HTMLElement | TestId): string[];
export function textsByRole(scope: HTMLElement | TestId, role: string): string[];
export function textsByRole(a: string | HTMLElement, b?: string | HTMLElement): string[] {
  let scope: HTMLElement | string | undefined;
  let role: string;
  if (typeof a !== 'string') [scope, role] = [a, typeof b === 'string' ? b : ''];
  else if (b === undefined) [scope, role] = [undefined, a];
  else if (typeof b !== 'string') [scope, role] = [b, a];
  else if (isContractTestId(a) && !isContractTestId(b)) [scope, role] = [a, b];
  else [scope, role] = [b, a]; // (역할, test id) — 문서의 순서
  const root = typeof scope === 'string' ? screen.queryByTestId(scope) : scope;
  if (typeof scope === 'string' && root === null) return [];
  const found = root ? within(root).queryAllByRole(role) : screen.queryAllByRole(role);
  return found.map((e) => textOf(e));
}
