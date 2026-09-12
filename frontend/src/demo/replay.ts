// Replays recorded events on a compressed clock with a fixed budget per phase, so a real export (hundreds of
// mine.scan events, minutes of benchmarking) still plays as: quick mining, a beat of verifying, and a
// benchmark phase long enough that every bench.step is visible. Relative event order is always preserved.
import type { Event } from "../types";

export const TOTAL_MS = 23_000;
export const MINING_MIN_MS = 2_500;
export const MINING_MAX_MS = 5_000;
export const VERIFY_MS = 5_000;
export const BENCH_MIN_MS = 11_000;
export const SCAN_GAP_MS = 10;
export const MINING_GAP_MS = 120;
const MINING_GAP_FLOOR_MS = 20;

export type Phase = "mining" | "verifying" | "benchmarking";
const PHASE_RANK: Record<Phase, number> = { mining: 0, verifying: 1, benchmarking: 2 };

function phaseOf(ev: Event): Phase | null {
  if (ev.type.startsWith("bench.") || ev.type === "scores" || ev.type === "done" || ev.type === "failed") return "benchmarking";
  if (ev.type.startsWith("verify.") || ev.type === "mine.done") return "verifying";
  if (ev.type === "clone.done" || ev.type.startsWith("mine.")) return "mining";
  if (ev.type === "status") {
    const st = ev.data?.status;
    if (st === "benchmarking") return "benchmarking";
    if (st === "verifying") return "verifying";
    if (st === "queued" || st === "cloning" || st === "mining") return "mining";
  }
  return null;
}

/** Assign every event a phase; phases only move forward so the recorded order is never violated. */
export function phases(events: Event[]): Phase[] {
  let cur: Phase = "mining";
  return events.map((ev) => {
    const p = phaseOf(ev);
    if (p && PHASE_RANK[p] > PHASE_RANK[cur]) cur = p;
    return cur;
  });
}

function scaleTo(gaps: number[], target: number): number[] {
  const sum = gaps.reduce((a, b) => a + b, 0);
  if (sum <= 0) return gaps;
  const k = target / sum;
  return gaps.map((g) => g * k);
}

/** Absolute fire times (ms from start) for each event, in input order. */
export function schedule(events: Event[]): number[] {
  if (!events.length) return [];
  const ph = phases(events);
  const idx: Record<Phase, number[]> = { mining: [], verifying: [], benchmarking: [] };
  events.forEach((_, i) => idx[ph[i]].push(i));

  // Mining: scans tick fast, everything else gets a readable beat; the phase lands inside [MIN, MAX].
  // Over budget, the readable beats give way first (down to a floor) so scans keep their 10ms as long as they fit.
  const isScan = idx.mining.map((i) => events[i].type === "mine.scan");
  let mining: number[] = idx.mining.map((_, k) => (k === 0 ? 0 : isScan[k] ? SCAN_GAP_MS : MINING_GAP_MS));
  const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);
  if (sum(mining) > MINING_MAX_MS) {
    const scanMs = sum(mining.filter((_, k) => isScan[k]));
    const others = mining.filter((g, k) => !isScan[k] && g > 0).length;
    const otherGap = Math.max(MINING_GAP_FLOOR_MS, (MINING_MAX_MS - scanMs) / Math.max(others, 1));
    mining = mining.map((g, k) => (isScan[k] || g === 0 ? g : otherGap));
    if (sum(mining) > MINING_MAX_MS) mining = scaleTo(mining, MINING_MAX_MS);
  } else if (sum(mining) < MINING_MIN_MS && mining.length > 1) {
    mining = scaleTo(mining, MINING_MIN_MS);
  }
  const miningMs = sum(mining);

  // Verifying: evenly spaced across its fixed budget (only if there is something to show).
  const verifyMs = idx.verifying.length ? VERIFY_MS : 0;
  const verify = idx.verifying.map(() => verifyMs / idx.verifying.length);

  // Benchmarking: whatever remains of the total, never less than BENCH_MIN_MS, evenly spaced.
  const benchMs = idx.benchmarking.length ? Math.max(BENCH_MIN_MS, TOTAL_MS - miningMs - verifyMs) : 0;
  const bench = idx.benchmarking.map(() => benchMs / idx.benchmarking.length);

  const out = new Array<number>(events.length).fill(0);
  let t = 0;
  const lay = (ids: number[], gaps: number[]) => {
    ids.forEach((i, k) => {
      t += gaps[k];
      out[i] = t;
    });
  };
  lay(idx.mining, mining);
  lay(idx.verifying, verify);
  lay(idx.benchmarking, bench);
  return out;
}

/** Fire `onEvent` for each event on the compressed clock; `onDone` after the last. Returns a canceller. */
export function replay(events: Event[], onEvent: (ev: Event) => void, onDone: () => void): () => void {
  const times = schedule(events);
  const start = performance.now();
  let i = 0;
  let timer = 0;
  let cancelled = false;
  const tick = () => {
    if (cancelled) return;
    const now = performance.now() - start;
    while (i < events.length && times[i] <= now) {
      onEvent(events[i]);
      i++;
    }
    if (i >= events.length) {
      onDone();
      return;
    }
    timer = window.setTimeout(tick, Math.max(1, times[i] - now));
  };
  timer = window.setTimeout(tick, 0);
  return () => {
    cancelled = true;
    window.clearTimeout(timer);
  };
}
