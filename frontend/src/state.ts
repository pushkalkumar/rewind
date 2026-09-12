// Folds Event[] into UI state. Live SSE and demo replay both feed this reducer.
import type { AgentResult, Event, Instance, MinedStats, ModelScore, RunReport, RunStatus } from "./types";
import { benchKey, instanceIdOf, shortSha } from "./format";

export type LogKind = "scan" | "candidate" | "keep" | "discard" | "info" | "bench";

export interface LogLine {
  seq: number;
  kind: LogKind;
  text: string;
}

export interface VerifiedInstance {
  id: string;
  sha: string;
  subject: string;
  fail_to_pass: string[];
  pass_to_pass_count: number;
}

export interface BenchRow {
  key: string;
  instanceId: string;
  modelId: string;
  modelLabel: string;
  step: number;
  tool: string;
  argsPreview: string;
  done: boolean;
}

export interface Counters {
  scanned: number;
  candidates: number;
  verified: number;
  discarded: number;
}

export interface UIState {
  status: RunStatus | "idle";
  message: string;
  repoUrl: string;
  repoName: string;
  commits: number;
  counters: Counters;
  log: LogLine[];
  verified: VerifiedInstance[];
  bench: Record<string, BenchRow>;
  benchOrder: string[];
  results: AgentResult[];
  scores: ModelScore[];
  stats: MinedStats | null;
  report: RunReport | null;
  error: string | null;
  lastSeq: number;
}

export const initialState: UIState = {
  status: "idle",
  message: "",
  repoUrl: "",
  repoName: "",
  commits: 0,
  counters: { scanned: 0, candidates: 0, verified: 0, discarded: 0 },
  log: [],
  verified: [],
  bench: {},
  benchOrder: [],
  results: [],
  scores: [],
  stats: null,
  report: null,
  error: null,
  lastSeq: 0,
};

export type Action =
  | { type: "reset"; repoUrl?: string }
  | { type: "event"; event: Event }
  | { type: "report"; report: RunReport }
  | { type: "error"; error: string };

const LOG_MAX = 200;

function pushLog(state: UIState, seq: number, kind: LogKind, text: string): LogLine[] {
  const log = state.log.length >= LOG_MAX ? state.log.slice(state.log.length - LOG_MAX + 1) : state.log.slice();
  log.push({ seq, kind, text });
  return log;
}

function flags(r: AgentResult): string {
  const f = [r.fixed ? "FIXED" : "not fixed"];
  if (r.cheated) f.push("CHEATED");
  if (r.broke) f.push("BROKE");
  return f.join("  ");
}

function newRow(instanceId: string, modelId: string, modelLabel: string): BenchRow {
  return { key: benchKey(instanceId, modelId), instanceId, modelId, modelLabel, step: 0, tool: "", argsPreview: "", done: false };
}

/**
 * Bench events carry the backend's authoritative instance id. Verified rows derive theirs from the sha
 * prefix; if a bench event names a row by a different-length prefix of the same sha, adopt the backend id
 * so table cells, bench keys and receipts line up.
 */
function adoptInstanceId(s: UIState, instanceId: string): void {
  if (!instanceId) return;
  const i = s.verified.findIndex((v) => v.id !== instanceId && v.sha.startsWith(instanceId));
  if (i < 0) return;
  const verified = s.verified.slice();
  verified[i] = { ...verified[i], id: instanceId };
  s.verified = verified;
}

function applyEvent(state: UIState, ev: Event): UIState {
  const seq = Number(ev.seq) || 0;
  // Idempotent: history from GET /events and the SSE replay may overlap; a seq we already folded is a no-op.
  if (seq && seq <= state.lastSeq) return state;
  const d = ev.data;
  const s: UIState = { ...state, lastSeq: Math.max(state.lastSeq, seq) };
  switch (ev.type) {
    case "status":
      s.status = d.status as RunStatus;
      s.message = d.message || "";
      if (d.message) s.log = pushLog(s, ev.seq, "info", `${d.status}  ${d.message}`);
      return s;
    case "clone.done":
      s.repoName = d.repo_name || s.repoName;
      s.commits = d.commits || 0;
      s.log = pushLog(s, ev.seq, "info", `clone ${d.repo_name}  ${d.commits} commits`);
      return s;
    case "mine.scan":
      s.counters = { ...s.counters, scanned: s.counters.scanned + 1 };
      s.log = pushLog(s, ev.seq, d.candidate ? "candidate" : "scan", `scan ${shortSha(d.sha)}  ${d.subject}${d.candidate ? "  → candidate" : ""}`);
      return s;
    case "mine.candidate":
      s.counters = { ...s.counters, candidates: s.counters.candidates + 1 };
      return s;
    case "verify.start":
      s.log = pushLog(s, ev.seq, "info", `verify ${shortSha(d.sha)}  build broken tree`);
      return s;
    case "verify.keep": {
      const f2p: string[] = d.fail_to_pass || [];
      s.counters = { ...s.counters, verified: s.counters.verified + 1 };
      s.verified = [...s.verified, { id: instanceIdOf(d.sha), sha: d.sha, subject: d.subject, fail_to_pass: f2p, pass_to_pass_count: d.pass_to_pass_count || 0 }];
      s.log = pushLog(s, ev.seq, "keep", `verify ${shortSha(d.sha)}  keep  ${f2p.length} fail-to-pass`);
      return s;
    }
    case "verify.discard":
      s.counters = { ...s.counters, discarded: s.counters.discarded + 1 };
      s.log = pushLog(s, ev.seq, "discard", `verify ${shortSha(d.sha)}  discard  ${d.reason}`);
      return s;
    case "mine.done":
      s.stats = d.stats as MinedStats;
      if (s.stats) s.counters = { scanned: s.stats.commits_scanned, candidates: s.stats.candidates, verified: s.stats.verified, discarded: s.stats.discarded };
      return s;
    case "bench.start": {
      adoptInstanceId(s, d.instance_id);
      const row = newRow(d.instance_id, d.model_id, d.model_label);
      s.bench = { ...s.bench, [row.key]: row };
      s.benchOrder = s.benchOrder.includes(row.key) ? s.benchOrder : [...s.benchOrder, row.key];
      s.log = pushLog(s, ev.seq, "bench", `bench ${d.instance_id} × ${d.model_label}  start`);
      return s;
    }
    case "bench.step": {
      adoptInstanceId(s, d.instance_id);
      const key = benchKey(d.instance_id, d.model_id);
      const prev = s.bench[key] || newRow(d.instance_id, d.model_id, d.model_id);
      s.bench = { ...s.bench, [key]: { ...prev, step: d.step, tool: d.tool, argsPreview: d.args_preview || "" } };
      if (!s.benchOrder.includes(key)) s.benchOrder = [...s.benchOrder, key];
      s.log = pushLog(s, ev.seq, "bench", `step ${d.step}  ${d.tool} ${d.args_preview || ""}`.trimEnd());
      return s;
    }
    case "bench.result": {
      const r = d.result as AgentResult;
      adoptInstanceId(s, r.instance_id);
      const key = benchKey(r.instance_id, r.model_id);
      const prev = s.bench[key] || newRow(r.instance_id, r.model_id, r.model_label);
      s.bench = { ...s.bench, [key]: { ...prev, step: r.steps_used, done: true } };
      if (!s.benchOrder.includes(key)) s.benchOrder = [...s.benchOrder, key];
      s.results = [...s.results.filter((x) => benchKey(x.instance_id, x.model_id) !== key), r];
      s.log = pushLog(s, ev.seq, r.cheated ? "discard" : "keep", `bench ${r.instance_id} × ${r.model_label}  ${flags(r)}`);
      return s;
    }
    case "scores":
      s.scores = d.scores as ModelScore[];
      return s;
    case "done":
      s.status = "done";
      if (d.report) return applyReport(s, d.report as RunReport);
      return s;
    case "failed":
      s.status = "failed";
      s.error = d.error || "run failed";
      s.log = pushLog(s, ev.seq, "discard", `failed  ${s.error}`);
      return s;
    default:
      return s;
  }
}

/**
 * Rows the table showed during benchmarking came from verify.keep in arrival order. When the report lands,
 * keep that order (report.instances may be sorted differently) and append anything the events never named.
 */
function orderInstances(verified: VerifiedInstance[], instances: Instance[]): Instance[] {
  if (!verified.length || !instances.length) return instances;
  const byId = new Map(instances.map((i) => [i.id, i]));
  const out: Instance[] = [];
  const seen = new Set<string>();
  for (const v of verified) {
    const inst = byId.get(v.id) ?? instances.find((i) => i.candidate.sha === v.sha || v.sha.startsWith(i.id));
    if (inst && !seen.has(inst.id)) {
      out.push(inst);
      seen.add(inst.id);
    }
  }
  for (const inst of instances) if (!seen.has(inst.id)) out.push(inst);
  return out;
}

function applyReport(state: UIState, report: RunReport): UIState {
  const instances = orderInstances(state.verified, report.instances);
  return {
    ...state,
    status: report.status,
    repoUrl: report.repo_url || state.repoUrl,
    repoName: report.repo_name || state.repoName,
    stats: report.stats,
    // mid-run reports only carry scan/candidate counts until mine.done; never let them zero what events already showed
    counters: {
      scanned: Math.max(report.stats.commits_scanned, state.counters.scanned),
      candidates: Math.max(report.stats.candidates, state.counters.candidates),
      verified: Math.max(report.stats.verified, state.counters.verified),
      discarded: Math.max(report.stats.discarded, state.counters.discarded),
    },
    results: report.results.length ? report.results : state.results,
    scores: report.scores.length ? report.scores : state.scores,
    report: instances === report.instances ? report : { ...report, instances },
    error: report.error,
  };
}

export function reducer(state: UIState, action: Action): UIState {
  switch (action.type) {
    case "reset":
      return { ...initialState, repoUrl: action.repoUrl || "" };
    case "event":
      return applyEvent(state, action.event);
    case "report":
      return applyReport(state, action.report);
    case "error":
      return { ...state, status: "failed", error: action.error };
  }
}

export function applyEvents(state: UIState, events: Event[]): UIState {
  return events.reduce((acc, ev) => applyEvent(acc, ev), state);
}
