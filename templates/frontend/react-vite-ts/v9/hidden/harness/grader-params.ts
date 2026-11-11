// 조절값 — 빌드 파이프라인이 숨은 테스트 계획(HiddenPlan.harness)에서 쓴 src/__grading__/grader-params.json 을 읽는다 (없으면 기본값).
const loaded = import.meta.glob('../grader-params.json', { eager: true, import: 'default' }) as Record<
  string,
  Record<string, unknown>
>;
const values: Record<string, unknown> = Object.values(loaded)[0] ?? {};

export const params = {
  has(name: string): boolean {
    return name in values;
  },
  int(name: string, fallback: number): number {
    const v = values[name];
    return typeof v === 'number' && Number.isFinite(v) ? Math.trunc(v) : fallback;
  },
  str(name: string, fallback: string): string {
    const v = values[name];
    return typeof v === 'string' ? v : fallback;
  },
  list(name: string): number[] {
    const v = values[name];
    return Array.isArray(v) ? v.filter((x): x is number => typeof x === 'number') : [];
  },
};
