// 하네스 자체 점검 (잠긴 하네스). verifier 가 케이스보다 먼저 돌린다 — 여기서 실패하면 결과는 HARNESS-ERR 이고
// 지원자·참조 구현의 판정을 내지 않는다. 과제 코드와 무관하게 템플릿 뼈대만으로 통과해야 한다 (앱 대신 작은 확인용 컴포넌트).
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect, useState } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { api, byTestId, network, openRoute, params, reason, tag, textsByRole } from '../harness/grader';

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
