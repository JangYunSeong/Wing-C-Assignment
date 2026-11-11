# 채점 하네스 API (frontend/react-vite-ts v6)

P1 내부 문서다 (`gen_hidden` 입력에 들어감). 채점용 이미지에만 있고 응시용 이미지에는 없다.

숨은 테스트는 **숨은 요구가 지켜졌는지**를 화면으로 확인하는 Vitest 테스트다. 실제 브라우저 없이 jsdom 에서 돈다. 서버 API 는 하네스가
가로채 **응답 시점을 테스트가 정한다** — 실제 시간 지연(sleep)으로 순서를 만들지 않으므로 몇 번을 돌려도 같은 순서로 도착한다.

## 케이스 파일

- 위치 `src/__grading__/cases/<이름>.test.tsx`. **HiddenPlan 케이스 하나 = 파일 하나**, 맨 바깥 `describe` 제목에 케이스 id(`hc1` …)와 확인할 내용.
- import 는 하네스(`'../harness/grader'`), **이 과제의 계약 도우미(`'./contract'`)**, 테스트 도구(`vitest`, `@testing-library/react`, `@testing-library/user-event`)만.
  **지원자 구현(`src/...`)을 import 하지 않는다** — 공개 계약(화면의 역할·이름·test id·문구, 서버 API 형식)으로만 판정한다.
- 조절값 파일(`src/__grading__/grader-params.json`)은 쓰지 않는다 (빌드 파이프라인이 쓴다).

## 계약 도우미 (`import { … } from './contract'`)

빌드 파이프라인이 공개 계약에서 만든다 (이 문서 끝에 붙은 `contract.ts` — 쓰지 않는다). test id·화면 경로·API 경로를 손으로 쓰지 않고 이것을 쓴다.
- `TID.X` — 화면 요소 test id · `SCREEN.S1` — 화면 경로 (`renderApp(SCREEN.S1)`). 경로 변수가 있는 화면은 함수다:
  `renderApp(SCREEN.S2({ productId: 'P-1' }))` — 변수를 채우지 않은 경로(`/products/:productId`)로 열지 않는다 · `API.GET_…` — 메서드·경로
- `routeContract()` — 이 과제의 계약 API 를 모두 가로챈다 (`beforeEach(() => routeContract())`)
- `waitFor<Api>(match?)` — 그 API 의 요청을 기다린다 (`api.waitForCall` 과 같다, 예 `waitForGetApiProductsSuggest((c) => c.query.get('q') === '노트')`)
- 계약의 데이터 모양(`interface`)과 API 마다 `<Api>Ok`(성공 응답 본문)·`<Api>Error`(오류 응답 본문) 타입 — `waitFor<Api>()` 가 돌려준 요청의
  `respond`·`fail` 은 이 타입만 받는다. **응답 본문은 이 과제 계약의 모양 그대로** (아래 예시의 본문 모양은 예시 계약의 것 — 따라 하지 않는다).

## 하네스 (`import { … } from '../harness/grader'`)

**서버 API — `api`**
- `api.route(method, path)` — 계약의 API 하나를 가로챈다 (경로는 MSW 꼴, 예 `'/api/items/:id'`). 들어온 요청은 응답할 때까지 붙잡힌다.
  테스트 파일 맨 위나 `beforeEach` 에서 그 케이스가 쓰는 API 를 모두 가로챈다.
- `await api.waitForCall(method, path, match?)` → `CallHandle` — 맞는(아직 응답하지 않은) 요청이 올 때까지 기다린다.
  `match(call)` 로 쿼리·본문을 고른다 (예 `(c) => c.query.get('q') === '노트'`). 5초 안에 안 오면 실패 (받은 요청 목록을 보여 줌). **match 없이 부르면 붙잡힌 요청이 하나여야 한다** — 같은 API 요청이 여럿 나갈 수 있으면(입력마다 요청 등) match 로 확인할 요청을 고른다 (여럿이면 오류).
- `CallHandle`: `query`(URLSearchParams) · `params`(경로 변수) · `headers`(요청 헤더, 이름은 소문자 — 예 `call.headers.authorization`) · `body`(요청 본문, JSON 이면 객체 — `json` 필드는 없다, 타입은 unknown 이라 계약 모양으로 좁혀 쓴다) ·
  `await respond(json, status = 계약의 성공 상태)`(상태를 주지 않으면 계약의 성공 상태 — 생성 API 가 201 이면 201 · 상태를 먼저 쓴 `respond(404, json)` 도 됨) · `await fail(status = 500, json?)`(본문만 `fail({ code, message })` 도 됨 — 상태 500) · `await networkError()` —
  **앱이 그 응답을 다 처리한 뒤에 돌아온다**. 그 다음 화면이 판정 대상이다 (`waitFor` 없이 바로 단언해도 된다).
- `api.always(method, path, reply, match?)` — 붙잡지 않고 바로 같은 응답 (판정과 상관없는 API).
- `api.once(method, path, reply, match?)` — 맞는 **다음 한 번만** 이 응답, 그 뒤는 원래대로 (collection 이면 목록이, 아니면 붙잡기).
  collection 으로 둔 API 의 일부 요청만 실패시킬 때 (예 `api.once('POST', '/notes', { status: 500, json: { code: 'E', message: '저장 실패' } }, (c) => c.body.text === 'B')`).
- `api.calls(method?, path?)` — 이 테스트에서 받은 요청 전부 (순서대로 — 일부만 셀 때는 앞서 센 수를 뺀다). `api.pending()` — 아직 응답하지 않은 수. `api.unhandled()` — 계약에 없는(가로채지 않은) 요청.
- `api.collection(path, opts?)` — **목 서버 목록**: `path`(예 `'/drafts'`)와 `path/:id` 요청을 붙잡지 않고 메모리 목록으로 바로 응답한다.
  `GET path` 목록 · `POST path` 추가(본문에 id 가 없으면 서버가 정함, 계약의 성공 상태 — 없으면 201) · `GET/PUT/PATCH/DELETE path/:id` 항목 하나(없으면 404, DELETE 는 계약의 성공 상태 — 없으면 204).
  `opts`: `items`(처음 서버에 있는 항목) · `toItem(body, id)`(서버가 채우는 칸 — 예 `updatedAt`) · `listBody(items)`(목록 응답을 감쌀 때) · `createdStatus` · `idField`.
  `items`·`toItem` 은 계약 타입을 그대로 받는다: `toItem: (body: PostApiMemosRequest['body'], id): PostApiMemosOk => ({ ...body, id })`.
  같은 id 로 두 번 POST 하면 두 번 들어간다 (중복 전송이 서버 상태에 보인다). 이 경로들은 `waitForCall`·`waitFor<Api>()` 로 기다리지 않는다.
- `api.serverState<계약 타입>(path)` — 그 목록의 **지금 서버 상태** (복사본, 들어온 순서). 서버에 반영됐는지는 앱이 다시 GET 하기를 기다리지 말고 이것으로 본다.
  계약 타입으로 받는다 (`api.serverState<Memo>('/api/memos').map((m) => m.content)`) — `as Memo[]` 로 바꾸지 않는다.
- `api.once`·`api.always` 의 match 와 `api.calls()` 결과의 `body` 는 칸 이름으로 읽는다 (`(c) => c.body.text === 'B'` — 값은 unknown 이라 비교·`String(…)` 으로). 계약 타입이 필요하면 `api.calls<PostMemosRequest['body']>(…)`.
- 경로 변수 이름은 달라도 같은 경로다 (`'/drafts/:id'` = `'/drafts/:draftId'` = `'/drafts/{draftId}'`).
- 테스트마다 붙잡힌 요청·기록·목록은 자동으로 비워진다 (가로챈 경로는 남음).

**브라우저 — 연결 상태 `network` · 저장소**
- `await network.goOffline()` — 연결을 끊는다: `navigator.onLine` 이 false, `window` 에 `offline` 이벤트. 그 뒤 `fetch` 는 서버에 닿지 않고 `TypeError` 로 거부된다
  (이미 붙잡힌 요청은 그대로). `await network.goOnline()` — 다시 연결: `navigator.onLine` 이 true, `online` 이벤트 (앱의 동기화가 여기서 시작된다).
  `network.blocked()` — 끊긴 동안 거부된 요청. 테스트마다 온라인으로 돌아간다.
  **`window.dispatchEvent(new Event('online'))` 를 직접 보내지 않는다** — `navigator.onLine` 이 그대로라 끊긴 적이 없는 것과 같다.
- IndexedDB(fake-indexeddb)·localStorage·sessionStorage 가 있다. **테스트마다 비어 있다.** 처음 내용(예: 저장된 토큰)이 필요하면 계약 `storage` 의
  이름·모양 그대로 `renderApp()` 전에 넣는다 (`window.localStorage.setItem('auth', JSON.stringify({ … }))`). 한 테스트 안에서 `renderApp()` 을 다시 부르면 새로고침처럼
  저장소는 그대로 두고 앱만 다시 그린다 (앱 모듈의 최상위 변수는 새로고침에도 남는다 — 그것에 기대는 판정은 하지 않는다).
- **이 템플릿에 없는 것**: 두 번째 탭·창(앱을 동시에 둘 그릴 수 없다), Web Locks(`navigator.locks`), SharedWorker·Worker, 서비스 워커.
  `new BroadcastChannel(…)` 로 메시지만 보내는 것은 다른 탭이 아니다 (다른 탭이 저장소에 쓴 것이 없다). 이런 것이 있어야 드러나는 숨은 요구는 이 하네스로 시험할 수 없다.

**화면 — `renderApp()` · `byTestId(id)` · `textOf(el)` · `textsByRole(role)`·`textsByRole(role, 범위)` · `flush()`**

- `textOf(el)` 은 화면에 보이는 글만 돌려준다 (v11) — 선택 상자는 **고른 값만**, 고르지 않은 선택지 글은 들어 있지 않다. 선택 상자의 값은 `(el as HTMLSelectElement).value` 로도 본다.
- `textsByRole` 의 범위는 요소 또는 **계약의 test id** — `textsByRole('listitem', TID.ORDER_LIST)` · `textsByRole('listitem', 목록요소)` (순서는 어느 쪽이든).
  test id 를 글자로 쓴 `textsByRole('order-list', 'listitem')` 은 컴파일되지 않는다 (어느 쪽이 역할인지 짐작해야 해서) — `TID.X` 를 쓴다.
- `const { user } = renderApp(SCREEN.S1)` — 계약 화면 경로(기본 `'/'`)에서 앱 전체를 그리고 사용자 조작 도구(`user.type`·`user.click`·`user.clear`·`user.keyboard`)를 준다.
  다시 부르면 앞서 그린 앱을 지우고 새로 그린다 (새로고침 — 메모리에만 둔 캐시·상태는 사라진다. '다른 화면에 갔다가 다시 들어오기'는 renderApp 을 다시 부르지 않고 앱 안의 링크·버튼으로 이동한다) (받은 요청 기록은 남는다). 한 케이스는 보통 한 번만 그린다.
- 요소는 **계약의 test id 로** 찾는다 — 조작할 요소는 `await screen.findByTestId(TID.X)`(없으면 실패), 있는지 볼 요소는 `byTestId(TID.X)`(없으면 null).
  목록 항목 안의 요소는 `within(항목).getByTestId(TID.X)` 또는 `byTestId(항목, TID.X)`. 입력 요소의 값은 `textContent` 가 아니라
  `(el as HTMLInputElement).value` 로 읽는다. 값이 있는 입력란은 `await user.clear(el)` 뒤에 `user.type` — 지우지 않으면 뒤에 붙는다
  ('2' 에 '3' → '23'). **조작은 계약의 '조작' 칸대로** — 서버 요청이 나가는 시점(입력마다·포커스 이동·Enter·클릭)도 그 칸에 적힌 대로 일으킨다
  (포커스 이동은 `await user.tab()`). 계약에는 접근 가능한 이름이 없으므로
  `getByRole(role, { name })` 의 이름을 짐작하지 않는다 (목록 항목처럼 여럿이면 `textsByRole(role)`·`getAllByTestId`).
- `flush()` — 대기 중인 타이머·마이크로태스크를 돌리고 React 갱신을 비운다 (응답은 보내지 않는다).

**실패 사유 — `reason(code, description)`**: `expect(값, reason('stale_response_shown', '입력은 "노트" 인데 목록이 바뀜')).toEqual(…)`.
코드는 아래 숨은 테스트 입력의 "실패 사유 코드" 목록 안의 것만.
사유는 **기본 매처**(`toBe`·`toEqual`·`toHaveLength`·`toContain`·`toBeGreaterThan`)에 붙인다 — jest-dom 매처(`toBeInTheDocument`·`toHaveTextContent` …)는
메시지를 버려 사유가 사라진다. 값을 꺼내 단언한다: `expect(screen.queryByRole('alert'), reason(…)).toBe(null)`.

**조절값 — `params.int(name, fallback)` · `params.str` · `params.list`** (숨은 테스트 계획의 조절값).

## 판정 규칙

- **변경 뒤 재조회는 하네스가 응답한다** (v9): 테스트가 성공 응답을 준 GET 과 같은 요청(메서드·경로·쿼리)이 그 뒤 성공한 변경 응답 다음에
  다시 오면, 테스트가 그 GET 을 기다리지 않는 한 테스트의 다음 하네스 동작 전에 하네스가 지금 서버 상태(테스트가 준 GET 본문 + 그 뒤
  성공한 변경 — 같은 id 항목에 PATCH·PUT 응답을 합침, DELETE 는 뺌, 같은 목록 경로의 POST 응답은 더함)로 응답한다. 그래서 성공 뒤
  목록을 다시 받는 구현도, 변경 응답으로 목록을 고치는 구현도 같은 확정 값을 갖는다 — 확정 값은 변경 응답 본문과 맞게 준다.
  응답은 그 변경의 `respond`·`fail` 이 돌아오기 전에 간다 (앱이 응답을 처리하며 보낸 재조회까지). 재조회의 응답 시점·실패를 시험할
  테스트만 변경에 응답하기 전에 `api.manualRefetch()` 를 부르고 그 GET 을 `waitForCall`·`waitFor<Api>()` 로 기다려 응답한다.
- **응답 순서는 테스트가 정한다**: 요청 둘 이상을 `waitForCall` 로 붙잡은 뒤 원하는 순서로 `respond`/`fail`. 같은 요청(메서드·경로·쿼리)이
  여럿이면 한 번에 모두 같은 응답을 받는다.
- **의도한 사유는 그 실패만 걸리는 단언에 붙인다** — 예: 늦게 온 응답 뒤 "목록이 최신 입력의 것인지" 에 `stale_response_shown`,
  늦게 실패한 요청 뒤 "오류 알림이 없는지" 에 `stale_error_shown`. 준비 단계 확인(첫 응답 뒤 목록이 보이는지)에는 사유를 붙이지 않는다.
- **판정 전에 기능이 동작했는지 먼저 확인한다** — "목록이 없다" 같은 부재만 단언하면 기능이 아예 없는 시작 코드도 통과한다. 먼저 정상 응답으로
  목록이 보이는 것을 확인한 뒤 숨은 요구를 시험한다.
- 입력을 지운 뒤의 판정처럼 앱의 디바운스가 끝나야 하는 경우, 지운 결과가 화면에 반영된 것을 `waitFor` 로 확인하고 계약의 시간(예 "1초 안에 요청")에
  여유를 더해 기다린 뒤 응답을 보낸다 — 장비 부하에 따라 판정이 바뀌지 않게.
- **기다리는 요청은 계약·공개 요구가 정한 것만**: 빈 입력에도 요청하는지, 같은 입력을 다시 요청하는지처럼 구현마다 다른 요청을 `waitFor…()` 로
  기다리지 않는다 — 올바른 구현이 "요청이 오지 않음"으로 떨어진다. 늦게 오는 응답은 **이미 붙잡아 둔 요청**으로 만든다
  (기능 동작 확인용 요청과 늦게 응답할 요청을 따로: 먼저 한 입력으로 응답해 목록을 확인하고, 글자를 더 입력해 생긴 요청을 붙잡아 둔다).
- **앞 요청에 응답하지 않은 채 다음 요청을 기다리는 것은, 앞 응답을 기다리지 않는 것이 공개 계약인 경우만**: 대기열을 하나씩 보내는 구현은
  앞 요청의 응답을 받아야 다음 요청을 보낸다 — 붙잡아 둔 요청부터 응답하고 다음 요청을 기다린다 (요청의 순서·횟수가 구현마다 다르면 아래 서버 상태로).
  기다림이 끝나면 하네스가 "응답하지 않은 요청 N개가 붙잡혀 있는 동안 기다렸다"고 알린다. 같은 경로의 요청이 왔는데 `match` 에 맞지 않았으면
  그 본문을 함께 보여 준다 — 테스트가 짐작한 값(저장소 키·본문 칸)이 계약과 다른지 본다.
- **오프라인 저장·재연결 동기화처럼 요청의 순서·횟수가 구현마다 다른 흐름은 서버 상태로 판정한다**: 그 API 를 `api.collection` 으로 두고
  `network.goOffline()` → 화면 조작 → `network.goOnline()` → `await waitFor(() => expect(api.serverState('/drafts')…).…)`.
  서버가 정하는 값(id·시각)은 짐작하지 않고 `api.serverState` 에서 읽는다. 응답 시점을 정해야 하는 요청만 `waitForCall` 로 붙잡는다.
- 테스트끼리 서로 독립 — 순서에 기대지 않는다. 화면 문구·역할은 계약에 적힌 그대로.

## 예시 (모양만 — 과제마다 계약에 맞춘다)

```tsx
import { screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { reason, renderApp, textsByRole } from '../harness/grader';
import { routeContract, SCREEN, TID, waitForGetApiProductsSuggest } from './contract';

const q = (v: string) => (c: { query: URLSearchParams }) => (c.query.get('q') ?? '').trim() === v;

describe('hc1 먼저 보낸 검색의 응답이 늦게 와도 최신 입력의 목록이 남는다', () => {
  beforeEach(() => routeContract());

  it('"노" 응답이 "노트" 응답보다 늦게 와도 "노트" 목록', async () => {
    const { user } = renderApp(SCREEN.S1);
    const input = await screen.findByTestId(TID.SEARCH_INPUT);
    await user.type(input, '노');
    const a = await waitForGetApiProductsSuggest(q('노'));
    await user.type(input, '트');
    const b = await waitForGetApiProductsSuggest(q('노트'));
    await b.respond({ query: '노트', items: [{ id: '1', name: '노트 패드' }], total: 1 });
    await waitFor(() => expect(textsByRole('option')).toEqual(['노트 패드']));
    await a.respond({ query: '노', items: [{ id: '2', name: '노끈' }], total: 1 });
    expect(textsByRole('option'), reason('stale_response_shown', '입력은 "노트" 인데 늦게 온 "노" 응답으로 목록이 바뀜')).toEqual([
      '노트 패드',
    ]);
  });
});
```
