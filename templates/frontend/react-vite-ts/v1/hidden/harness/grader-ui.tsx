// 화면 도우미 — 앱을 그리고 사용자처럼 조작한다. 판정은 공개 계약(화면 요소의 역할·이름·test id·문구)으로만 한다.
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../../App';

export { flush } from './grader-api';

/** 앱 전체를 그리고 사용자 조작 도구를 준다. 지원자 구현의 내부 모듈은 import 하지 않는다. */
export function renderApp() {
  const user = userEvent.setup();
  const view = render(<App />);
  return { user, ...view };
}

/** test id 로 찾기 (없으면 null). */
export function byTestId(id: string): HTMLElement | null {
  return screen.queryByTestId(id);
}

/** 보이는 텍스트를 공백 하나로 접어 돌려준다. */
export function textOf(el: Element | null): string {
  return (el?.textContent ?? '').replace(/\s+/g, ' ').trim();
}

/** 역할이 role 인 요소들의 텍스트 (예: 'option', 'listitem', 'row'). */
export function textsByRole(role: string): string[] {
  return screen.queryAllByRole(role).map((e) => textOf(e));
}
