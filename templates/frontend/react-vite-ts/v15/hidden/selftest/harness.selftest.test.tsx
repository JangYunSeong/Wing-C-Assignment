// 하네스 자체 점검 (잠긴 하네스). verifier 가 케이스보다 먼저 돌린다 — 여기서 실패하면 결과는 HARNESS-ERR 이고
// 지원자·참조 구현의 판정을 내지 않는다. 과제 코드와 무관하게 템플릿 뼈대만으로 통과해야 한다 (앱 대신 작은 확인용 컴포넌트).
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect, useState } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { api, byTestId, headMeta, network, openRoute, params, reason, tag, textsByRole, type TestId } from '../harness/grader';

function Probe() {
  const [log, setLog] = useState<string[]>([]);
  const add = (line: string) => setLog((l) => [...l, line]);
  async function load(q: string) {
    try {
      const res = await fetch(`/__selftest/echo?q=${encodeURIComponent(q)}`);
      if (!res.ok) {
        add(`error:${q}:${res.status}`);
        return;
      }
      const body = (await res.json()) as { value: string };
      add(`ok:${q}:${body.value}`);
    } catch {
      add(`rejected:${q}`);
    }
  }
  async function save() {
    const res = await fetch('/__selftest/items/7', { method: 'POST', body: JSON.stringify({ n: 1 }) });
    add(`saved:${res.status}`);
  }
  async function stray() {
    try {
      await fetch('/__selftest/not-in-contract');
    } catch {
      add('stray-rejected');
    }
  }
  return (
    <div>
      <button onClick={() => load('a')}>a</button>
      <button onClick={() => load('b')}>b</button>
      <button onClick={() => save()}>save</button>
      <button onClick={() => stray()}>stray</button>
      <ul>
        {log.map((x, i) => (
          <li key={i}>{x}</li>
        ))}
      </ul>
    </div>
  );
}

function Things() {
  const [log, setLog] = useState<string[]>([]);
  async function get(path: string) {
    const res = await fetch(path);
    const body = (await res.json()) as { v: string };
    setLog((l) => [...l, `${path}:${body.v}`]);
  }
  return (
    <div>
      <button onClick={() => get('/__selftest/things/list')}>list</button>
      <button onClick={() => get('/__selftest/things/5')}>one</button>
      <ul>
        {log.map((x, i) => (
          <li key={i}>{x}</li>
        ))}
      </ul>
    </div>
  );
}

function setup() {
  const user = userEvent.setup();
  render(<Probe />);
  return { user, click: (name: string) => user.click(screen.getByRole('button', { name })) };
}

describe('하네스 자체 점검', () => {
  beforeEach(() => {
    api.route('GET', '/__selftest/echo');
    api.route('POST', '/__selftest/items/:id');
  });

  it('붙잡은 요청을 테스트가 정한 순서로 푼다 (먼저 보낸 요청의 응답을 나중에)', async () => {
    const { click } = setup();
    await click('a');
    const a = await api.waitForCall('GET', '/__selftest/echo', (c) => c.query.get('q') === 'a');
    await click('b');
    const b = await api.waitForCall('GET', '/__selftest/echo', (c) => c.query.get('q') === 'b');
    expect(api.pending('GET', '/__selftest/echo')).toBe(2);
    await b.respond({ value: 'B' });
    await a.respond({ value: 'A' });
    expect(textsByRole('listitem')).toEqual(['ok:b:B', 'ok:a:A']);
  });

  it('match 없이 기다릴 때 붙잡힌 요청이 여럿이면 오류 — 취소된 요청은 세지 않는다 (v7)', async () => {
    void fetch('/__selftest/echo?q=a');
    void fetch('/__selftest/echo?q=b');
    await expect(api.waitForCall('GET', '/__selftest/echo', undefined, 1000)).rejects.toThrow(/2개 — match 로/);
    const a = await api.waitForCall('GET', '/__selftest/echo', (c) => c.query.get('q') === 'a');
    await a.respond({ ok: true });
    await (await api.waitForCall('GET', '/__selftest/echo')).respond({ ok: true }); // 하나만 남음
    // 취소된 지난 요청은 세지 않고, match 없이 기다리면 살아 있는 요청을 준다
    const ctrl = new AbortController();
    void fetch('/__selftest/echo?q=old', { signal: ctrl.signal }).catch(() => undefined);
    await api.waitForCall('GET', '/__selftest/echo', (c) => c.query.get('q') === 'old');
    ctrl.abort();
    void fetch('/__selftest/echo?q=new');
    await api.waitForCall('GET', '/__selftest/echo', (c) => c.query.get('q') === 'new'); // 도착 확인 (응답은 아직)
    const live = await api.waitForCall('GET', '/__selftest/echo');
    expect(live.query.get('q')).toBe('new');
    await live.respond({ ok: true });
  });

  it('respond 는 앱이 응답을 처리한 뒤 돌아온다 (기다리지 않고 바로 판정할 수 있다)', async () => {
    const { click } = setup();
    await click('a');
    const a = await api.waitForCall('GET', '/__selftest/echo');
    await a.respond({ value: 'A' });
    expect(textsByRole('listitem')).toEqual(['ok:a:A']);
  });

  it('fail 은 오류 응답, networkError 는 연결 실패(fetch 거부)', async () => {
    const { click } = setup();
    await click('a');
    await (await api.waitForCall('GET', '/__selftest/echo')).fail(503);
    await click('b');
    await (await api.waitForCall('GET', '/__selftest/echo', (c) => c.query.get('q') === 'b')).networkError();
    expect(textsByRole('listitem')).toEqual(['error:a:503', 'rejected:b']);
  });

  it('respond 는 상태를 먼저 써도 같은 응답 (respond(503, 본문))', async () => {
    const { click } = setup();
    await click('a');
    await (await api.waitForCall('GET', '/__selftest/echo')).respond(503, { value: 'x' });
    await click('b');
    await (await api.waitForCall('GET', '/__selftest/echo', (c) => c.query.get('q') === 'b')).respond({ value: 'B' }, 200);
    expect(textsByRole('listitem')).toEqual(['error:a:503', 'ok:b:B']);
  });

  it('fail 은 본문만 줘도 상태 500 으로 보낸다', async () => {
    const { click } = setup();
    await click('a');
    await (await api.waitForCall('GET', '/__selftest/echo')).fail({ code: 'X', message: 'y' });
    await click('b');
    await (await api.waitForCall('GET', '/__selftest/echo', (c) => c.query.get('q') === 'b')).fail(409, { code: 'C' });
    expect(textsByRole('listitem')).toEqual(['error:a:500', 'error:b:409']);
  });

  it('always 는 붙잡지 않고 바로 응답한다', async () => {
    api.always('GET', '/__selftest/echo', { json: { value: 'fixed' } });
    const { click } = setup();
    await click('a');
    await waitFor(() => expect(textsByRole('listitem')).toEqual(['ok:a:fixed']));
    expect(api.pending()).toBe(0);
  });

  it('받은 요청의 경로 변수·본문을 기록한다', async () => {
    const { click } = setup();
    await click('save');
    const call = await api.waitForCall('POST', '/__selftest/items/:id');
    expect(call.params.id).toBe('7');
    expect(call.body).toEqual({ n: 1 });
    await call.respond({ ok: true }, 201);
    expect(textsByRole('listitem')).toEqual(['saved:201']);
    expect(api.calls('POST').length).toBe(1);
  });

  it('고정 경로가 같은 꼴의 매개변수 경로보다 먼저 받는다 — 매개변수 경로를 나중에 등록해도 (v13)', async () => {
    // MSW 는 나중에 등록한 처리기를 앞에 둔다 — 계약 순서대로 목록 다음 상세를 등록하면 목록 요청이 id="list" 로 붙잡혔다 (2026-11-15)
    api.route('GET', '/__selftest/things/list');
    api.route('GET', '/__selftest/things/{id}');
    const user = userEvent.setup();
    render(<Things />);
    await user.click(screen.getByRole('button', { name: 'list' }));
    const list = await api.waitForCall('GET', '/__selftest/things/list');
    await list.respond({ v: 'L' });
    await user.click(screen.getByRole('button', { name: 'one' }));
    const one = await api.waitForCall('GET', '/__selftest/things/:id');
    expect(one.params.id).toBe('5');
    await one.respond({ v: 'O' });
    expect(textsByRole('listitem')).toEqual(['/__selftest/things/list:L', '/__selftest/things/5:O']);
  });

  it('계약에 없는 요청은 unhandled 에 남는다', async () => {
    const { click } = setup();
    await click('stray');
    await waitFor(() => expect(api.unhandled()).toContain('GET /__selftest/not-in-contract'));
  });

  it('reset 은 붙잡힌 요청을 풀고 기록을 지운다', async () => {
    const { click } = setup();
    await click('a');
    await api.waitForCall('GET', '/__selftest/echo');
    await api.reset();
    expect(api.pending()).toBe(0);
    expect(api.calls()).toEqual([]);
  });

  it('openRoute 는 앱 주소를 화면 경로로 바꾼다 (renderApp(route) 가 그리기 전에)', () => {
    openRoute('/__selftest/route?tab=2');
    expect(window.location.pathname + window.location.search).toBe('/__selftest/route?tab=2');
    openRoute('/');
    expect(window.location.pathname).toBe('/');
  });

  it('byTestId 는 화면 전체 또는 범위 안에서 찾는다', () => {
    render(
      <ul>
        <li data-testid="row">
          <span data-testid="qty">1</span>
        </li>
        <li data-testid="row">
          <span data-testid="qty">2</span>
        </li>
      </ul>,
    );
    const rows = screen.getAllByTestId('row');
    expect(byTestId(rows[1], 'qty')?.textContent).toBe('2');
    expect(textsByRole(rows[0], 'listitem')).toEqual([]);
    expect(textsByRole(screen.getByRole('list'), 'listitem')).toEqual(['1', '2']);
    expect(textsByRole('listitem', screen.getByRole('list'))).toEqual(['1', '2']);
    expect(byTestId('없는-id')).toBe(null);
  });

  it('사유 태그와 조절값 기본값', () => {
    expect(reason('stale_response_shown', '목록이 바뀜')).toBe('[reason:stale_response_shown] 목록이 바뀜');
    expect(tag('x')).toBe('[reason:x]');
    expect(params.int('없는_조절값', 7)).toBe(7);
  });
});

function ConnectionProbe() {
  const [events, setEvents] = useState<string[]>([]);
  useEffect(() => {
    const on = () => setEvents((e) => [...e, `online:${navigator.onLine}`]);
    const off = () => setEvents((e) => [...e, `offline:${navigator.onLine}`]);
    window.addEventListener('online', on);
    window.addEventListener('offline', off);
    return () => {
      window.removeEventListener('online', on);
      window.removeEventListener('offline', off);
    };
  }, []);
  return (
    <ul>
      {events.map((x, i) => (
        <li key={i}>{x}</li>
      ))}
    </ul>
  );
}

function idbPut(value: { id: string }): Promise<void> {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open('__selftest', 1);
    open.onupgradeneeded = () => open.result.createObjectStore('items', { keyPath: 'id' });
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const tx = open.result.transaction('items', 'readwrite');
      tx.objectStore('items').put(value);
      tx.oncomplete = () => {
        open.result.close();
        resolve();
      };
      tx.onerror = () => reject(tx.error);
    };
  });
}

function idbCount(): Promise<number> {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open('__selftest', 1);
    open.onupgradeneeded = () => open.result.createObjectStore('items', { keyPath: 'id' });
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const req = open.result.transaction('items').objectStore('items').count();
      req.onsuccess = () => {
        open.result.close();
        resolve(req.result);
      };
      req.onerror = () => reject(req.error);
    };
  });
}

describe('하네스 자체 점검 v3 — IndexedDB · 연결 상태 · 목 서버 목록', () => {
  it('IndexedDB 가 있다 (fake-indexeddb) — 쓰고 읽는다', async () => {
    await idbPut({ id: 'a' });
    expect(await idbCount()).toBe(1);
  });

  it('IndexedDB 는 테스트마다 비어 있다', async () => {
    expect(await idbCount()).toBe(0);
  });

  it('goOffline/goOnline — navigator.onLine 과 offline·online 이벤트', async () => {
    render(<ConnectionProbe />);
    expect(navigator.onLine).toBe(true);
    await network.goOffline();
    expect(navigator.onLine).toBe(false);
    await network.goOnline();
    expect(textsByRole('listitem')).toEqual(['offline:false', 'online:true']);
    expect(network.isOnline()).toBe(true);
  });

  it('오프라인 동안 fetch 는 서버에 닿지 않고 TypeError 로 거부된다', async () => {
    api.route('GET', '/__selftest/echo');
    await network.goOffline();
    await expect(fetch('/__selftest/echo?q=x')).rejects.toBeInstanceOf(TypeError);
    expect(api.calls()).toEqual([]);
    expect(network.blocked()).toEqual(['GET /__selftest/echo?q=x']);
    await expect(api.waitForCall('GET', '/__selftest/echo', () => true, 200)).rejects.toThrow('서버에 닿지 않은 요청');
  });

  it('collection — 목록·추가(서버가 id)·수정·부분 수정·삭제·없음, serverState 는 복사본', async () => {
    api.collection('/__selftest/notes', { items: [{ id: 'n1', title: '처음' }] });
    const list = await fetch('/__selftest/notes');
    expect(await list.json()).toEqual([{ id: 'n1', title: '처음' }]);
    const created = await fetch('/__selftest/notes', { method: 'POST', body: JSON.stringify({ title: '새 글' }) });
    expect(created.status).toBe(201);
    const made = (await created.json()) as { id: string; title: string };
    expect(made.title).toBe('새 글');
    expect(made.id).not.toBe('');
    const put = await fetch(`/__selftest/notes/${made.id}`, { method: 'PUT', body: JSON.stringify({ title: '고침' }) });
    expect(await put.json()).toEqual({ id: made.id, title: '고침' });
    await fetch('/__selftest/notes/n1', { method: 'PATCH', body: JSON.stringify({ done: true }) });
    const del = await fetch(`/__selftest/notes/${made.id}`, { method: 'DELETE' });
    expect(del.status).toBe(204);
    expect((await fetch('/__selftest/notes/없음', { method: 'DELETE' })).status).toBe(404);
    const state = api.serverState('/__selftest/notes');
    expect(state).toEqual([{ id: 'n1', title: '처음', done: true }]);
    state.length = 0;
    expect(api.serverState('/__selftest/notes')).toHaveLength(1);
    expect(api.calls('DELETE', '/__selftest/notes/{noteId}')).toHaveLength(2);
  });

  it('collection — 본문에 id 가 있으면 그 id, 같은 id 두 번 POST 는 두 번 들어간다 · toItem·listBody', async () => {
    api.collection('/__selftest/notes', {
      toItem: (body, id) => ({ ...body, id, updatedAt: '2026-01-01T00:00:00Z' }),
      listBody: (items) => ({ items }),
      createdStatus: 200,
    });
    for (let i = 0; i < 2; i += 1) {
      const r = await fetch('/__selftest/notes', { method: 'POST', body: JSON.stringify({ id: 'c1', title: 't' }) });
      expect(r.status).toBe(200);
    }
    expect(await (await fetch('/__selftest/notes')).json()).toEqual({
      items: [
        { id: 'c1', title: 't', updatedAt: '2026-01-01T00:00:00Z' },
        { id: 'c1', title: 't', updatedAt: '2026-01-01T00:00:00Z' },
      ],
    });
  });

  it('collection 경로를 waitForCall 로 기다리면 바로 알려 준다', async () => {
    api.collection('/__selftest/notes');
    await expect(api.waitForCall('POST', '/__selftest/notes')).rejects.toThrow('api.serverState');
    expect(() => api.serverState('/없는-목록')).toThrow('있는 목록');
  });

  it('경로 변수 이름이 달라도 같은 경로 — 먼저 가로챈 경로를 그대로 쓴다', async () => {
    api.route('PUT', '/__selftest/items/{itemId}');
    api.route('PUT', '/__selftest/items/:id');
    const pending = fetch('/__selftest/items/9', { method: 'PUT', body: '{}' });
    const call = await api.waitForCall('PUT', '/__selftest/items/:anything');
    expect(call.params.itemId).toBe('9');
    await call.respond(null, 204);
    expect((await pending).status).toBe(204);
  });

  it('reset 은 연결을 되살리고 목록을 지운다', async () => {
    api.collection('/__selftest/notes');
    await network.goOffline();
    await api.reset();
    expect(navigator.onLine).toBe(true);
    expect(network.blocked()).toEqual([]);
    expect(() => api.serverState('/__selftest/notes')).toThrow();
  });
});

describe('하네스 자체 점검 v4 — once · headers', () => {
  it('once 는 맞는 다음 한 번만 그 응답, 그 뒤는 collection 이 받는다', async () => {
    api.collection('/__selftest/notes');
    api.once('POST', '/__selftest/notes', { status: 500, json: { code: 'E' } }, (c) => c.body.t === 'B'); // v6: 문서 예시 그대로 컴파일
    const post = (t: string) => fetch('/__selftest/notes', { method: 'POST', body: JSON.stringify({ t }) });
    expect((await post('A')).status).toBe(201);
    expect((await post('B')).status).toBe(500);
    expect((await post('B')).status).toBe(201);
    expect(api.serverState<{ id: string; t: string }>('/__selftest/notes').map((n) => n.t)).toEqual(['A', 'B']);
    expect(api.calls('POST', '/__selftest/notes').map((c) => String(c.body.t))).toEqual(['A', 'B', 'B']);
  });

  it('요청 헤더를 소문자 이름으로 기록한다', async () => {
    api.route('GET', '/__selftest/echo');
    const pending = fetch('/__selftest/echo', { headers: { Authorization: 'Bearer new-token' } });
    const call = await api.waitForCall('GET', '/__selftest/echo');
    expect(call.headers.authorization).toBe('Bearer new-token');
    await call.respond({ value: 'x' });
    expect((await pending).status).toBe(200);
  });
});

describe('하네스 자체 점검 v8 — 계약의 성공 상태 · test id 범위 · 계약 타입 목록 · 기다림 시간 초과 안내', () => {
  it('상태를 주지 않은 응답은 계약의 성공 상태 (respond·always·collection) — 상태를 주면 그 상태, 계약에 없으면 v7 과 같다', async () => {
    api.contract({
      ok: {
        'POST /__selftest/v8/orders': 201,
        'GET /__selftest/v8/fixed': 203,
        'POST /__selftest/v8/memos': 202,
        'DELETE /__selftest/v8/memos/{memoId}': 200,
      },
    });
    api.route('POST', '/__selftest/v8/orders');
    const first = fetch('/__selftest/v8/orders', { method: 'POST', body: JSON.stringify({ n: 1 }) });
    await (await api.waitForCall('POST', '/__selftest/v8/orders')).respond({ id: 'o1' });
    expect((await first).status).toBe(201);
    const second = fetch('/__selftest/v8/orders', { method: 'POST', body: JSON.stringify({ n: 2 }) });
    await (await api.waitForCall('POST', '/__selftest/v8/orders')).respond({ id: 'o2' }, 200);
    expect((await second).status).toBe(200);
    api.always('GET', '/__selftest/v8/fixed', { json: { v: 1 } });
    expect((await fetch('/__selftest/v8/fixed')).status).toBe(203);
    api.collection('/__selftest/v8/memos');
    expect((await fetch('/__selftest/v8/memos', { method: 'POST', body: JSON.stringify({ id: 'm1' }) })).status).toBe(202);
    expect((await fetch('/__selftest/v8/memos/m1', { method: 'DELETE' })).status).toBe(200);
    api.collection('/__selftest/v8/plain');
    expect((await fetch('/__selftest/v8/plain', { method: 'POST', body: '{}' })).status).toBe(201);
    api.route('GET', '/__selftest/v8/echo');
    const plain = fetch('/__selftest/v8/echo');
    await (await api.waitForCall('GET', '/__selftest/v8/echo')).respond({ v: 1 });
    expect((await plain).status).toBe(200);
  });

  it('textsByRole 은 범위를 계약 test id 로도 받는다 — 순서는 어느 쪽이든, 범위 요소가 아직 없으면 빈 배열', () => {
    api.contract({ testIds: ['v8-list', 'v8-later'] });
    const LIST = 'v8-list' as TestId;
    render(
      <div>
        <ul data-testid="v8-list">
          <li>가</li>
          <li>나</li>
        </ul>
        <ul>
          <li>다</li>
        </ul>
      </div>,
    );
    expect(textsByRole('listitem', LIST)).toEqual(['가', '나']);
    expect(textsByRole(LIST, 'listitem')).toEqual(['가', '나']);
    expect(textsByRole('listitem', 'v8-later' as TestId)).toEqual([]);
    expect(textsByRole('listitem')).toEqual(['가', '나', '다']);
    expect(screen.getByTestId(LIST).tagName).toBe('UL'); // TestId 는 문자열 — getByTestId 에 그대로
    void (() => {
      // @ts-expect-error — 문자열 두 개는 어느 쪽이 역할인지 짐작해야 해서 받지 않는다 (계약의 TID 를 쓴다)
      textsByRole('v8-list', 'listitem');
    });
  });

  it('collection 의 toItem·items 가 계약 타입을 받는다 (v7 은 Record 고정이라 TS2322)', async () => {
    interface MemoBody {
      content: string;
      siteId: string;
    }
    interface Memo extends MemoBody {
      id: string;
      createdAt: string;
    }
    const seed: Memo[] = [{ id: 'm0', content: '처음', siteId: 's', createdAt: '2026-01-01T00:00:00Z' }];
    api.collection('/__selftest/v8/typed', {
      items: seed,
      toItem: (body: MemoBody, id: string): Memo => ({ ...body, id, createdAt: '2026-01-02T00:00:00Z' }),
      listBody: (items) => ({ items: items.map((m) => m.id) }),
    });
    await fetch('/__selftest/v8/typed', { method: 'POST', body: JSON.stringify({ content: '새', siteId: 's' }) });
    expect(api.serverState<Memo>('/__selftest/v8/typed').map((m) => m.createdAt)).toEqual([
      '2026-01-01T00:00:00Z',
      '2026-01-02T00:00:00Z',
    ]);
    // 타입을 달지 않은 toItem 이 items 에 없는 칸을 더해도 된다 (v6·v7 꼴 그대로 컴파일)
    api.collection('/__selftest/v8/loose', {
      items: [{ id: 'a', title: 'x' }],
      toItem: (body, id) => ({ ...body, id, updatedAt: 'z' }),
      listBody: (items) => ({ items }),
    });
    await fetch('/__selftest/v8/loose', { method: 'POST', body: JSON.stringify({ title: 'y' }) });
    expect(api.serverState<{ id: string; updatedAt?: string }>('/__selftest/v8/loose').map((n) => n.updatedAt)).toEqual([
      undefined,
      'z',
    ]);
  });

  it('match 없이 기다릴 때 같은 요청 여럿은 모호하지 않다 — 한 응답이 모두에 간다', async () => {
    api.route('POST', '/__selftest/v8/refresh');
    const sent = [1, 2, 3].map(() => fetch('/__selftest/v8/refresh', { method: 'POST', body: '{}' }));
    await waitFor(() => expect(api.pending('POST', '/__selftest/v8/refresh')).toBe(3));
    const call = await api.waitForCall('POST', '/__selftest/v8/refresh');
    await call.respond({ ok: true });
    expect((await Promise.all(sent)).map((r) => r.status)).toEqual([200, 200, 200]);
    expect(api.pending()).toBe(0);
  });

  it('요청이 오지 않아 기다림이 끝나면 응답하지 않은 채 붙잡힌 요청을 함께 알린다', async () => {
    api.route('POST', '/__selftest/v8/queue');
    void fetch('/__selftest/v8/queue', { method: 'POST', body: JSON.stringify({ n: 1 }) });
    const first = await api.waitForCall('POST', '/__selftest/v8/queue');
    await expect(api.waitForCall('POST', '/__selftest/v8/queue', () => false, 300)).rejects.toThrow(
      /응답하지 않은 요청 1개가 붙잡혀 있는 동안 기다렸다 \["POST \/__selftest\/v8\/queue"\]/,
    );
    // 같은 경로의 요청이 와 있으면 match 에 맞지 않은 것 — 본문을 함께
    await expect(api.waitForCall('POST', '/__selftest/v8/queue', () => false, 300)).rejects.toThrow(
      /같은 경로의 요청 1개가 왔지만 match 에 맞지 않았다 \(본문 \{"n":1\}\)/,
    );
    await first.respond({});
    await expect(api.waitForCall('POST', '/__selftest/v8/queue', () => false, 300)).rejects.toThrow(
      /^요청이 오지 않음: POST \/__selftest\/v8\/queue \(받은 요청/,
    );
  });
});

describe('하네스 자체 점검 v12 — 본문까지 같은 요청만 묶기 · 계약 id 형식의 목록 id', () => {
  const post = (path: string, body: unknown) => fetch(path, { method: 'POST', body: JSON.stringify(body) });

  it('본문이 다른 요청은 따로 응답한다 — 메모 B 만 실패, A·C 는 붙잡힌 채 · 응답 처리 기다림이 A·C 를 기다리지 않는다', async () => {
    api.route('POST', '/__selftest/v12/memos');
    const sent = ['A', 'B', 'C'].map((note) => post('/__selftest/v12/memos', { note }));
    await waitFor(() => expect(api.pending('POST', '/__selftest/v12/memos')).toBe(3));
    const t0 = performance.now();
    await (await api.waitForCall('POST', '/__selftest/v12/memos', (c) => (c.body as { note: string }).note === 'B')).fail(503);
    expect(performance.now() - t0).toBeLessThan(2500); // v11 처럼 같은 URL 의 A·C 를 기다리면 3초 대기 뒤에야 넘어간다
    expect(api.pending('POST', '/__selftest/v12/memos')).toBe(2);
    for (const note of ['A', 'C']) {
      await (await api.waitForCall('POST', '/__selftest/v12/memos', (c) => (c.body as { note: string }).note === note)).respond({ note });
    }
    expect((await Promise.all(sent)).map((r) => r.status)).toEqual([200, 503, 200]);
  });

  it('본문까지 같은 요청 여럿은 지금처럼 한 응답이 모두에 간다 (v8) · 본문이 다른 요청을 match 없이 기다리면 모호함 안내', async () => {
    api.route('POST', '/__selftest/v12/refresh');
    const same = [1, 2, 3].map(() => post('/__selftest/v12/refresh', { token: 't' }));
    await waitFor(() => expect(api.pending('POST', '/__selftest/v12/refresh')).toBe(3));
    await (await api.waitForCall('POST', '/__selftest/v12/refresh')).respond({ ok: true });
    expect((await Promise.all(same)).map((r) => r.status)).toEqual([200, 200, 200]);
    void post('/__selftest/v12/refresh', { token: 'x' });
    void post('/__selftest/v12/refresh', { token: 'y' });
    await waitFor(() => expect(api.pending('POST', '/__selftest/v12/refresh')).toBe(2));
    await expect(api.waitForCall('POST', '/__selftest/v12/refresh', undefined, 500)).rejects.toThrow(/2개 — match 로/);
  });

  it('계약이 숫자 id 목록을 알리면 서버가 정하는 id 는 숫자(가장 큰 값 + 1), 본문의 id 는 그 형식 그대로 · 알리지 않으면 글자', async () => {
    api.contract({ idKinds: { '/__selftest/v12/nums': 'number' } });
    api.collection('/__selftest/v12/nums', {
      items: [{ id: 3, t: 'a' }],
      toItem: (body: { t: string }, id: number) => ({ ...body, id, made: true }), // id: number 로 써도 컴파일 (v12)
    });
    const made = (await (await post('/__selftest/v12/nums', { t: 'b' })).json()) as { id: unknown; made: boolean };
    expect(made).toEqual({ t: 'b', id: 4, made: true });
    const given = (await (await post('/__selftest/v12/nums', { id: 10, t: 'c' })).json()) as { id: unknown };
    expect(given.id).toBe(10);
    expect((await fetch('/__selftest/v12/nums/4')).status).toBe(200);
    expect(api.serverState<{ id: number }>('/__selftest/v12/nums').map((x) => x.id)).toEqual([3, 4, 10]);
    api.collection('/__selftest/v12/strs', { toItem: (body, id: string) => ({ ...body, id }) }); // 글자로 써도 컴파일
    const s = (await (await post('/__selftest/v12/strs', { t: 'x' })).json()) as { id: unknown };
    expect(typeof s.id).toBe('string');
  });
});

describe('하네스 자체 점검 v14 — 변경 뒤 재조회 자동 응답은 같은 신원(Authorization·Cookie)만', () => {
  // 2026-11-18 로그아웃 뒤 다른 사용자 재료: A 의 목록에 응답한 뒤 B 의 로그인(성공한 POST) 다음 온 B 의 같은 목록 요청을
  // 재조회로 보고 A 의 목록으로 대신 응답해, 허용 구현까지 모두 'B 응답 전 A 항목이 보임'으로 떨어졌다.
  const LIST = '/__selftest/v14/list';
  const LOGIN = '/__selftest/v14/login';
  const list = (who: string) => fetch(LIST, { headers: { Authorization: `Bearer ${who}` } });

  beforeEach(() => {
    api.route('GET', LIST);
    api.route('POST', LOGIN);
  });

  /** A 로 목록을 받고, 로그인 응답을 처리하며 목록을 다시 받는 앱 — 같은 사용자면 재조회, 다른 사용자면 새 요청 */
  async function listAThenLogin(next: string): Promise<{ after: Promise<Response> }> {
    const first = list('A');
    await (await api.waitForCall('GET', LIST)).respond([{ id: 1, owner: 'A' }]);
    expect((await first).status).toBe(200);
    const after = fetch(LOGIN, { method: 'POST', body: JSON.stringify({ who: next }) }).then(() => list(next));
    await (await api.waitForCall('POST', LOGIN)).respond({ token: next });
    return { after };
  }

  it('같은 신원이 변경 뒤 다시 받으면 지금 서버 상태로 자동 응답한다 (v9 그대로)', async () => {
    const { after } = await listAThenLogin('A');
    expect(await (await after).json()).toEqual([{ id: 1, owner: 'A' }]);
  });

  it('다른 신원의 같은 요청은 재조회가 아니다 — 앞 사용자의 목록을 주지 않고 붙잡아 두어 테스트가 응답한다', async () => {
    const { after } = await listAThenLogin('B');
    const b = await api.waitForCall('GET', LIST, (c) => c.headers.authorization === 'Bearer B');
    await b.respond([{ id: 2, owner: 'B' }]);
    expect(await (await after).json()).toEqual([{ id: 2, owner: 'B' }]);
  });
});

describe('하네스 자체 점검 v15 — 문서 머리는 index.html 그대로 시작 · headMeta', () => {
  /** 앱처럼 데이터를 받은 뒤 제목·meta 를 바꾸는 확인용 컴포넌트 — '있는 meta 를 고친다'와 '없으면 만든다' 두 꼴 */
  function MetaProbe({ title, image }: { title: string; image: string }) {
    useEffect(() => {
      document.title = title;
      const og = document.head.querySelector('meta[property="og:image"]') ?? document.createElement('meta');
      og.setAttribute('property', 'og:image');
      og.setAttribute('content', image);
      document.head.appendChild(og);
    }, [title, image]);
    return <p>{title}</p>;
  }

  // 순서가 있는 두 점검 — 앞 점검이 바꾼 head 가 다음 점검에 남지 않는지 본다
  it('head 는 앱의 index.html 의 <title>·<meta> 로 시작한다 — 앱이 바꾼 제목·meta 는 headMeta·document.title 로 읽는다', async () => {
    expect(document.head.querySelectorAll('title, meta').length, 'index.html 의 head 를 읽지 못함').toBeGreaterThan(0);
    expect(document.head.querySelector('script')).toBe(null);
    expect(headMeta('og:image')).toBe(null);
    render(<MetaProbe title="자체 점검 제목" image="https://cdn.example.com/a.png" />);
    await waitFor(() => expect(headMeta('og:image')).toBe('https://cdn.example.com/a.png'));
    expect(document.title).toBe('자체 점검 제목');
  });

  it('앞 점검이 바꾼 제목·meta 는 다음 점검에 남지 않는다 (테스트마다 index.html 그대로)', () => {
    expect(headMeta('og:image')).toBe(null);
    expect(document.title).not.toBe('자체 점검 제목');
  });

  it('headMeta 는 property·name 어느 쪽에 적었든 content — 없으면 null, 같은 키가 여럿이면 첫 것', () => {
    const add = (attr: 'property' | 'name', key: string, content: string) => {
      const el = document.createElement('meta');
      el.setAttribute(attr, key);
      el.setAttribute('content', content);
      document.head.appendChild(el);
    };
    add('property', 'og:description', '소개 A');
    add('name', 'description', '설명');
    add('property', 'og:description', '소개 B');
    expect(headMeta('og:description')).toBe('소개 A');
    expect(headMeta('description')).toBe('설명');
    expect(headMeta('og:title')).toBe(null);
  });
});
