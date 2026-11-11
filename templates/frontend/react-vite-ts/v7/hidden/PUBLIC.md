# 공개 테스트 작성용 (frontend/react-vite-ts v6)

P1 내부 문서다 (`gen_public` 입력에 들어감). 채점용 이미지에만 있고 응시용 이미지에는 없다.

공개 테스트는 **공개 수용 기준(p\*)이 지켜지는지**를 화면으로 확인하는 기능 테스트다. 시작 코드·참조 구현이 이 테스트를 보고 맞춘다(테스트 먼저).
숨은 요구(응답 순서 뒤바뀜·늦은 실패·중복 요청 같은 것)는 시험하지 않는다 — 그건 숨은 테스트 몫이다.

## 케이스 파일

- 위치 `src/__public__/<이름>.test.tsx`. **계약의 공개 테스트(pt\*) 하나 = 파일 하나**, 맨 바깥 `describe` 제목에 pt id(`pt1` …)와 확인할 내용.
- 하네스(`'../__grading__/harness/grader'`)의 `api`·`network`·`renderApp`·`textsByRole`·`byTestId`·`flush` 를 쓴다 (숨은 테스트와 같은 하네스).
- **이 과제의 계약 도우미(`'./contract'`)** — 빌드 파이프라인이 공개 계약에서 만든다 (이 문서 끝에 붙은 `contract.ts`, 쓰지 않는다).
  `TID.X`(test id) · `SCREEN.S1`(화면 경로, `renderApp(SCREEN.S1)` — 경로 변수가 있으면 함수: `SCREEN.S2({ productId: 'P-1' })`) · `API.…` ·
  `routeContract()`(계약 API 를 모두 가로챔) · `waitFor<Api>(match?)`.
  test id·경로를 손으로 쓰지 않는다. `waitFor<Api>()` 가 돌려준 요청의 `respond`·`fail` 은 계약의 응답 타입(`<Api>Ok`·`<Api>Error`)만 받는다 —
  **응답 본문은 이 과제 계약의 모양 그대로** (예시의 본문 모양은 예시 계약의 것).
- 지원자 구현(`src/...`)을 import 하지 않는다 — 공개 계약(화면의 역할·이름·test id·문구, 서버 API 형식)으로만 판정한다.

## 판정 규칙

- **기대 결과는 계약에서만**: 화면 요소·문구는 계약 그대로, 서버 응답 모양은 계약의 API 형식 그대로.
- 응답은 요청 하나에 하나씩, 보낸 순서대로 준다 (`waitForCall` → `respond`). **응답 순서를 뒤바꾸거나, 늦게 실패시키거나, 같은 조작을 빠르게
  반복하지 않는다** — 숨은 요구 영역이다. 판정과 상관없는 API 는 `api.always`.
- 같은 API 요청이 여럿 나갈 수 있으면(글자마다 검색 요청 등) **`waitFor…(match)` 로 지금 확인할 요청을 골라** 응답한다 — match 없이 기다렸는데
  붙잡힌 요청이 여럿이면 하네스가 오류를 낸다 (먼저 온 지난 요청에 응답하면 지난 응답을 무시하는 올바른 구현이 떨어진다).
- 요소는 **계약의 test id 로** 찾는다 — 조작할 요소는 `await screen.findByTestId(TID.X)`, 목록 항목 안은 `within(항목).getByTestId(TID.X)`
  (`byTestId` 는 없으면 null 이라 클릭에 바로 넘기지 않는다). 계약에 없는 접근 가능한 이름을 짐작하지 않는다.
- **조작은 계약의 '조작' 칸대로** — 서버 요청이 나가는 시점(입력마다·포커스 이동·Enter·클릭)도 그대로 (포커스 이동은 `await user.tab()`).
  값이 있는 입력란은 `await user.clear(el)` 뒤에 `user.type` — 지우지 않으면 뒤에 붙는다 ('2' 에 '3' → '23').
- 기다리는 요청은 계약·공개 요구가 정한 것만. 확인 방법이 구현마다 다를 수 있는 요청(빈 입력에 다시 요청하는지 등)을 말하면
  요청을 기다리지 말고 **화면 결과로만** 확인한다 — 올바른 구현이 "요청이 오지 않음"으로 떨어진다.
- **서버에 반영됐는지는 서버 상태로** — 계약이 "서버 GET … 응답에 그 항목이 있는지/없는지"를 말하면 그 API 를 `api.collection(path, opts?)` 로 두고
  (`GET path` 목록 · `POST path` 추가, 본문에 id 가 없으면 서버가 정함 · `PUT/PATCH/DELETE path/:id`, 없으면 404 — 붙잡지 않고 바로 응답)
  `await waitFor(() => expect(api.serverState<계약 타입>(path)…).…)` 로 본다 (`as 계약타입[]` 로 바꾸지 않는다). 서버가 정하는 id·시각은 짐작하지 않는다.
  collection 경로는 `waitFor<Api>()` 로 기다리지 않는다. 처음 서버에 있는 항목은 `opts.items`, 서버가 채우는 칸은 `opts.toItem(body, id)`.
- **연결 끊김·재연결은 `await network.goOffline()`·`await network.goOnline()`** — `navigator.onLine` 과 `offline`·`online` 이벤트, 끊긴 동안 `fetch` 거부.
  `window.dispatchEvent(new Event('online'))` 를 직접 보내지 않는다.
- IndexedDB·localStorage 는 있고 테스트마다 비어 있다 (처음 내용이 필요하면 계약 `storage` 모양대로 `renderApp()` 전에 `window.localStorage.setItem(…)`). 한 테스트에서 `renderApp()` 을 다시 부르면 새로고침 — 메모리에만 둔 캐시는 사라진다. '다른 화면에 갔다가 다시 들어오기'는 앱 안의 링크·버튼으로 이동한다 (저장소는 그대로, 앱만 다시 그림).
  두 번째 탭·창·Web Locks·Worker 는 없다 — 그런 것을 흉내 내는 테스트를 쓰지 않는다.
- 테스트끼리 서로 독립 — 순서에 기대지 않는다.

## 예시 (모양만)

오프라인에서 만든 항목이 재연결 뒤 서버에 생기는지 (예시 계약 — 본문 모양은 과제 계약대로):

```tsx
import { screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { api, network, renderApp } from '../__grading__/harness/grader';
import { routeContract, SCREEN, TID } from './contract';

describe('pt2 오프라인에서 저장한 메모가 재연결 뒤 서버에 생긴다', () => {
  beforeEach(() => {
    routeContract();
    api.collection('/notes', { toItem: (body, id) => ({ ...body, id, updatedAt: '2026-01-01T00:00:00.000Z' }) });
  });

  it('재연결 뒤 서버 목록에 그 메모', async () => {
    const { user } = renderApp(SCREEN.S1);
    await network.goOffline();
    await user.type(await screen.findByTestId(TID.NOTE_INPUT), '장보기');
    await user.click(await screen.findByTestId(TID.NOTE_SAVE_BTN));
    expect(api.serverState('/notes')).toEqual([]);
    await network.goOnline();
    await waitFor(() => expect(api.serverState('/notes').map((n) => n.text)).toEqual(['장보기']));
  });
});
```

응답을 하나씩 주는 기능 테스트:

```tsx
import { screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { renderApp, textsByRole } from '../__grading__/harness/grader';
import { routeContract, SCREEN, TID, waitForGetApiProductsSuggest } from './contract';

describe('pt1 검색어를 입력하면 추천 목록이 보인다', () => {
  beforeEach(() => routeContract());

  it('응답의 상품명이 순서대로 보인다', async () => {
    const { user } = renderApp(SCREEN.S1);
    await user.type(await screen.findByTestId(TID.SEARCH_INPUT), '노트');
    const call = await waitForGetApiProductsSuggest((c) => (c.query.get('q') ?? '').trim() === '노트');
    await call.respond({ query: '노트', items: [{ id: '1', name: '노트 패드' }], total: 1 });
    await waitFor(() => expect(textsByRole('option')).toEqual(['노트 패드']));
  });
});
```
