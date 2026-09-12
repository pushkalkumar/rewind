import { useCallback, useMemo, useState } from "react";
import BenchPanel from "../components/BenchPanel";
import EmptyState from "../components/EmptyState";
import InstanceTable, { type TableRow } from "../components/InstanceTable";
import MiningPanel from "../components/MiningPanel";
import Receipt from "../components/Receipt";
import ReportCard from "../components/ReportCard";
import TopBar from "../components/TopBar";
import { benchKey, prettyRepo, repoNameFromUrl } from "../format";
import { useRun } from "../useRun";

const MINING = new Set(["queued", "cloning", "mining", "verifying"]);

export default function Home() {
  const { state, mode, health, busy, run, replayDemo } = useRun();
  const [open, setOpen] = useState<{ instanceId: string; modelId: string } | null>(null);
  const close = useCallback(() => setOpen(null), []);

  const models = useMemo(() => {
    // columns exist from the first report (model_ids) so a model that has not started yet still has a column;
    // labels are upgraded as soon as any event carries the display name
    const seen = new Map<string, string>();
    for (const id of state.report?.model_ids ?? []) seen.set(id, id.split(":").pop() || id);
    for (const s of state.scores) seen.set(s.model_id, s.model_label);
    for (const r of state.results) seen.set(r.model_id, r.model_label);
    for (const k of state.benchOrder) {
      const b = state.bench[k];
      if (b) seen.set(b.modelId, b.modelLabel);
    }
    return [...seen].map(([id, label]) => ({ id, label }));
  }, [state.report, state.scores, state.results, state.bench, state.benchOrder]);

  const rows: TableRow[] = useMemo(() => {
    if (state.report?.instances.length) return state.report.instances.map((i) => ({ id: i.id, subject: i.candidate.subject, f2p: i.fail_to_pass.length }));
    return state.verified.map((v) => ({ id: v.id, subject: v.subject, f2p: v.fail_to_pass.length }));
  }, [state.report, state.verified]);

  const subjects = useMemo(() => Object.fromEntries(rows.map((r) => [r.id, r.subject])), [rows]);

  const openResult = open ? state.results.find((r) => benchKey(r.instance_id, r.model_id) === benchKey(open.instanceId, open.modelId)) : undefined;
  const openInstance = open ? state.report?.instances.find((i) => i.id === open.instanceId) ?? null : null;

  const running = state.status !== "idle" && state.status !== "done" && state.status !== "failed";
  const showMining = MINING.has(state.status);
  const showBench = state.status === "benchmarking";
  const showReport = state.scores.length > 0 || (state.status === "done" && state.stats !== null);
  const showTable = state.status === "benchmarking" || ((state.status === "done" || state.status === "failed") && rows.length > 0);
  const repoName = prettyRepo(state.repoName) || repoNameFromUrl(state.repoUrl);

  return (
    <div className="min-h-screen bg-bg text-text">
      <TopBar mode={mode} health={health} busy={busy} running={running} repoUrl={state.repoUrl} runId={state.report?.id || undefined} onRun={run} onReplay={replayDemo} />
      <main className="mx-auto max-w-[1360px] space-y-12 px-10 pb-24 pt-8">
        {state.status === "idle" && !state.error && <EmptyState />}
        {state.error && (
          <div className="rounded-sm border border-del/40 px-4 py-3 text-body text-del">
            {state.error}
          </div>
        )}
        {showReport && <ReportCard scores={state.scores} results={state.results} stats={state.stats} repoName={repoName} runId={state.report?.id || ""} />}
        {showMining && <MiningPanel status={state.status} message={state.message} counters={state.counters} log={state.log} />}
        {showBench && <BenchPanel rows={state.benchOrder.map((k) => state.bench[k])} results={state.results} counters={state.counters} subjects={subjects} />}
        {showTable && <InstanceTable rows={rows} models={models} results={state.results} bench={state.bench} onOpen={(i, m) => setOpen({ instanceId: i, modelId: m })} />}
      </main>
      {open && openResult && <Receipt result={openResult} instance={openInstance} subject={subjects[open.instanceId] || open.instanceId} onClose={close} />}
    </div>
  );
}
