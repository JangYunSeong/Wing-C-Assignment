// 하네스 자체 점검 (잠긴 하네스). verifier 가 케이스보다 먼저 돌린다 — 여기서 실패하면 결과는 HARNESS-ERR 이고
// 지원자·참조 구현의 판정을 내지 않는다. 과제 코드와 무관하게 템플릿 뼈대만으로 통과해야 한다 (앱 대신 작은 확인용 컴포넌트).
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { api, params, reason, tag, textsByRole } from '../harness/grader';

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

  it('사유 태그와 조절값 기본값', () => {
    expect(reason('stale_response_shown', '목록이 바뀜')).toBe('[reason:stale_response_shown] 목록이 바뀜');
    expect(tag('x')).toBe('[reason:x]');
    expect(params.int('없는_조절값', 7)).toBe(7);
  });
});
