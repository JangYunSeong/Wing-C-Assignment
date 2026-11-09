# 채점 하네스 API (frontend/react-vite-ts v1)

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
- `TID.X` — 화면 요소 test id (`byTestId(TID.SEARCH_INPUT)`) · `SCREEN.S1` — 화면 경로 (`renderApp(SCREEN.S1)`) · `API.GET_…` — 메서드·경로
- `routeContract()` — 이 과제의 계약 API 를 모두 가로챈다 (`beforeEach(() => routeContract())`)
- `waitFor<Api>(match?)` — 그 API 의 요청을 기다린다 (`api.waitForCall` 과 같다, 예 `waitForGetApiProductsSuggest((c) => c.query.get('q') === '노트')`)
- 계약의 데이터 모양(`interface`)과 API 마다 `<Api>Ok`(성공 응답 본문)·`<Api>Error`(오류 응답 본문) 타입 — `waitFor<Api>()` 가 돌려준 요청의
  `respond`·`fail` 은 이 타입만 받는다. **응답 본문은 이 과제 계약의 모양 그대로** (아래 예시의 본문 모양은 예시 계약의 것 — 따라 하지 않는다).

## 하네스 (`import { … } from '../harness/grader'`)

**서버 API — `api`**
- `api.route(method, path)` — 계약의 API 하나를 가로챈다 (경로는 MSW 꼴, 예 `'/api/items/:id'`). 들어온 요청은 응답할 때까지 붙잡힌다.
  테스트 파일 맨 위나 `beforeEach` 에서 그 케이스가 쓰는 API 를 모두 가로챈다.
- `await api.waitForCall(method, path, match?)` → `CallHandle` — 맞는(아직 응답하지 않은) 요청이 올 때까지 기다린다.
  `match(call)` 로 쿼리·본문을 고른다 (예 `(c) => c.query.get('q') === '노트'`). 5초 안에 안 오면 실패 (받은 요청 목록을 보여 줌).
- `CallHandle`: `query`(URLSearchParams) · `params`(경로 변수) · `body`(JSON 이면 객체) ·
  `await respond(json, status = 200)`(상태를 먼저 쓴 `respond(404, json)` 도 됨) · `await fail(status = 500, json?)`(본문만 `fail({ code, message })` 도 됨 — 상태 500) · `await networkError()` —
  **앱이 그 응답을 다 처리한 뒤에 돌아온다**. 그 다음 화면이 판정 대상이다 (`waitFor` 없이 바로 단언해도 된다).
- `api.always(method, path, reply, match?)` — 붙잡지 않고 바로 같은 응답 (판정과 상관없는 API).
- `api.calls(method?, path?)` — 받은 요청 (순서대로). `api.pending()` — 아직 응답하지 않은 수. `api.unhandled()` — 계약에 없는(가로채지 않은) 요청.
- 테스트마다 붙잡힌 요청·기록은 자동으로 비워진다 (가로챈 경로는 남음).

**화면 — `renderApp()` · `byTestId(id)` · `textOf(el)` · `textsByRole(role)` · `flush()`**
- `const { user } = renderApp(SCREEN.S1)` — 계약 화면 경로(기본 `'/'`)에서 앱 전체를 그리고 사용자 조작 도구(`user.type`·`user.click`·`user.clear`·`user.keyboard`)를 준다.
- 요소는 **계약의 test id 로** 찾는다 — `await screen.findByTestId(TID.X)`·`byTestId(TID.X)`. 계약에는 접근 가능한 이름이 없으므로
  `getByRole(role, { name })` 의 이름을 짐작하지 않는다 (목록 항목처럼 여럿이면 `textsByRole(role)`·`getAllByTestId`).
- `flush()` — 대기 중인 타이머·마이크로태스크를 돌리고 React 갱신을 비운다 (응답은 보내지 않는다).

**실패 사유 — `reason(code, description)`**: `expect(값, reason('stale_response_shown', '입력은 "노트" 인데 목록이 바뀜')).toEqual(…)`.
코드는 아래 숨은 테스트 입력의 "실패 사유 코드" 목록 안의 것만.
사유는 **기본 매처**(`toBe`·`toEqual`·`toHaveLength`·`toContain`·`toBeGreaterThan`)에 붙인다 — jest-dom 매처(`toBeInTheDocument`·`toHaveTextContent` …)는
메시지를 버려 사유가 사라진다. 값을 꺼내 단언한다: `expect(screen.queryByRole('alert'), reason(…)).toBe(null)`.

**조절값 — `params.int(name, fallback)` · `params.str` · `params.list`** (숨은 테스트 계획의 조절값).

## 판정 규칙

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
