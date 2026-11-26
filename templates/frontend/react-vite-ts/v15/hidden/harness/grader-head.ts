// 문서 머리(<head>) — jsdom 은 앱의 index.html 을 읽지 않아 head 가 비고 제목이 '' 로 시작한다 (v15). 브라우저가 페이지를 열 때처럼
// 테스트마다·renderApp(새로고침)마다 index.html 의 <title>·<meta> 로 되돌린다 — '있는 meta 를 고친다'는 구현이 테스트에서만 아무것도
// 안 하게 되지 않도록. head 의 요소는 test id 로 찾을 수 없어(screen 은 body 만 본다) 값은 headMeta 로 읽는다.
// 2026-11-26 ct-9433f54e7d06-b1: 계약이 og:image·og:description meta 를 확인하는데 head 를 읽을 도구가 없어, 공개·숨은 테스트가
// querySelector 로 찾다 정적 검사(계약 test id 밖 짐작)에 막혀 생성이 모두 실패.
const loaded = import.meta.glob('../../../index.html', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;
const indexHtml: string = Object.values(loaded)[0] ?? '';

/** 하네스 안에서만 — head 를 앱의 index.html 의 <title>·<meta> 로 되돌린다 (스크립트·스타일은 넣지 않는다). */
export function resetHead(): void {
  const parsed = new DOMParser().parseFromString(indexHtml, 'text/html');
  const kept = Array.from(parsed.head.children).filter((el) => el.tagName === 'TITLE' || el.tagName === 'META');
  document.head.replaceChildren(...kept.map((el) => document.importNode(el, true)));
}

/** 문서 머리의 meta 값 — `<meta property="키">`(Open Graph 등)나 `<meta name="키">` 의 content, 없으면 null.
 *  property·name 어느 쪽에 적었는지는 짐작하지 않게 둘 다 본다. 같은 키가 여럿이면 문서 순서로 첫 것. 제목은 `document.title`. */
export function headMeta(key: string): string | null {
  const found = Array.from(document.head.querySelectorAll('meta')).find(
    (el) => el.getAttribute('property') === key || el.getAttribute('name') === key,
  );
  return found ? found.getAttribute('content') : null;
}
