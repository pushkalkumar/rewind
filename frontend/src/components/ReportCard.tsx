import type { AgentResult, MinedStats, ModelScore } from "../types";

export function FlagBar({ label, value, n, tone, light }: { label: string; value: number; n: number; tone: "accent" | "red" | "gray"; light?: boolean }) {
  const pct = n > 0 ? (value / n) * 100 : 0;
  const fill = tone === "accent" ? "bg-accent" : tone === "red" ? "bg-red" : light ? "bg-[#9a9a9a]" : "bg-muted";
  return (
    <div className="grid grid-cols-[72px_minmax(0,1fr)_44px] items-center gap-4">
      <span className="label">{label}</span>
      <div className={`h-[6px] w-full ${light ? "bg-[#e6e6e6]" : "bg-line"}`}>
        <div className={`h-full ${fill} transition-[width] duration-500`} style={{ width: `${pct}%` }} />
      </div>
      <span className="num text-right font-mono text-[12px] text-muted">
        {value}/{n}
      </span>
    </div>
  );
}

/** How hard the model worked for its grade: mean steps and seconds per task, and how often it ran out of steps. */
export function effortLine(results: AgentResult[]): string {
  if (!results.length) return "";
  const steps = results.reduce((a, r) => a + r.steps_used, 0) / results.length;
  const secs = results.reduce((a, r) => a + r.seconds, 0) / results.length;
  const capped = results.filter((r) => r.hit_cap).length;
  const parts = [`${steps.toFixed(1)} steps`, `${Math.round(secs)} s per task`];
  if (capped) parts.push(`step cap hit ${capped} of ${results.length}`);
  return parts.join(" · ");
}

export function ScoreCard({ score, results = [] }: { score: ModelScore; results?: AgentResult[] }) {
  const effort = effortLine(results);
  return (
    <div className="rounded-sm border hairline bg-surface px-8 pb-7 pt-6">
      <div className="label">{score.model_label}</div>
      <div className="mt-1 flex items-end justify-between gap-6">
        <div className="grade text-accent">{score.grade}</div>
        <div className="mb-2 w-full max-w-[420px] space-y-3">
          <FlagBar label="fixed" value={score.fixed} n={score.n} tone="accent" />
          <FlagBar label="cheated" value={score.cheated} n={score.n} tone="red" />
          <FlagBar label="broke" value={score.broke} n={score.n} tone="gray" />
        </div>
      </div>
      <div className="mt-6 text-body text-muted">
        <div className="num">
          honest fixes <span className="text-text">{score.honest}</span> of {score.n}
        </div>
        {effort && <div className="num mt-1 text-[12px] text-dim">{effort}</div>}
      </div>
    </div>
  );
}

interface Props {
  scores: ModelScore[];
  results?: AgentResult[];
  stats: MinedStats | null;
  repoName: string;
  runId: string;
}

/** One line on why a finished run has nothing to grade. */
export function explainEmpty(stats: MinedStats): string {
  if (stats.commits_scanned === 0) return "No commits were scanned, so there was nothing to mine.";
  if (stats.candidates === 0) return "No commit touched both a test file and source files while reading like a bug fix, so there was nothing to verify.";
  if (stats.verified === 0) {
    const reasons = Object.entries(stats.discard_reasons || {})
      .sort((a, b) => b[1] - a[1])
      .map(([r, n]) => `${r}: ${n}`)
      .join(", ");
    return `Every candidate was discarded during verification${reasons ? ` (${reasons})` : ""}: none had a test that fails at the parent commit and passes at the fix.`;
  }
  return "Instances were verified but no model produced a result, so there is nothing to grade.";
}

export default function ReportCard({ scores, results = [], stats, repoName, runId }: Props) {
  return (
    <section>
      {scores.length > 0 ? (
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          {scores.map((s) => (
            <ScoreCard key={s.model_id} score={s} results={results.filter((r) => r.model_id === s.model_id)} />
          ))}
        </div>
      ) : (
        <div className="rounded-sm border hairline bg-surface px-8 py-6">
          <div className="label">no grade</div>
          <div className="mt-2 text-[15px] text-text">Nothing to benchmark.</div>
          {stats && <p className="mt-2 max-w-3xl text-[13px] leading-relaxed text-muted">{explainEmpty(stats)}</p>}
        </div>
      )}
      <div className="mt-4 flex items-baseline justify-between gap-6 text-body text-muted">
        <div className="num">
          {stats ? (
            <>
              Mined <span className="text-text">{stats.commits_scanned}</span> commits, <span className="text-text">{stats.candidates}</span> candidates,{" "}
              <span className="text-text">{stats.verified}</span> verified, <span className="text-text">{stats.benchmarked}</span> benchmarked.
            </>
          ) : null}
        </div>
        <div className="font-mono text-[11px] text-dim">
          {repoName} {runId ? `· run ${runId.slice(0, 8)}` : ""}
        </div>
      </div>
    </section>
  );
}
