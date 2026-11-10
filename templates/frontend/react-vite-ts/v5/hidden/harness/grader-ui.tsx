// 화면 도우미 — 앱을 그리고 사용자처럼 조작한다. 판정은 공개 계약(화면 요소의 역할·이름·test id·문구)으로만 한다.
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../../App';

export { flush } from './grader-api';

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

/** 역할이 role 인 요소들의 텍스트 (예: 'option', 'listitem', 'row') — `textsByRole(role)` 은 화면 전체, `textsByRole(범위, role)` 은 그 요소 안에서.
 *  v5: 범위를 받는다 (2026-11-10 알파 fe7b-token: 테스트가 byTestId(범위, id) 처럼 textsByRole(목록, 'listitem') 을 써 컴파일 재요청 4번에도 남음). */
export function textsByRole(role: string): string[];
export function textsByRole(scope: HTMLElement, role: string): string[];
export function textsByRole(role: string, scope: HTMLElement): string[];
export function textsByRole(a: string | HTMLElement, b?: string | HTMLElement): string[] {
  // 범위·역할을 어느 순서로 줘도 받는다 — 2026-11-10 알파 fe7c-token: 범위를 받게 하자 이번엔 textsByRole('listitem', 범위) 로 써 컴파일 실패
  const scope = typeof a === 'string' ? (typeof b === 'object' ? b : undefined) : a;
  const role = typeof a === 'string' ? a : typeof b === 'string' ? b : '';
  const found = scope ? within(scope).queryAllByRole(role) : screen.queryAllByRole(role);
  return found.map((e) => textOf(e));
}
