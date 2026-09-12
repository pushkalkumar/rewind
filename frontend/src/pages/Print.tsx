import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getRun } from "../api";
import DiffView from "../components/Diff";
import demoFile from "../demo/run.json";
import { fmtDate, prettyRepo } from "../format";
import type { AgentResult, DemoFile, ModelScore, RunReport } from "../types";

const demo = demoFile as unknown as DemoFile;

function Bar({ label, value, n, accent }: { label: string; value: number; n: number; accent?: boolean }) {
  const pct = n > 0 ? (value / n) * 100 : 0;
  return (
    <div className="grid grid-cols-[64px_minmax(0,1fr)_40px] items-center gap-3">
      <span className="label">{label}</span>
      <div className="h-[6px] w-full bg-[#e6e6e6]">
        <div className={`h-full ${accent ? "bg-[#ffb020]" : "bg-[#555]"}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="num text-right font-mono text-[11px] text-[#555]">
        {value}/{n}
      </span>
    </div>
  );
}

function Grade({ score }: { score: ModelScore }) {
  return (
    <div className="border-t border-[#111] pt-4">
      <div className="label">{score.model_label}</div>
      <div className="mt-1 flex items-end gap-6">
        <div className="text-[96px] font-light leading-[0.95] tracking-tight text-[#ffb020]">{score.grade}</div>
        <div className="mb-2 w-full space-y-2">
          <Bar label="fixed" value={score.fixed} n={score.n} accent />
          <Bar label="cheated" value={score.cheated} n={score.n} />
          <Bar label="broke" value={score.broke} n={score.n} />
        </div>
      </div>
      <div className="mt-3 text-[12px] text-[#444]">
        honest fixes <span className="font-semibold text-[#111]">{score.honest}</span> of {score.n}
      </div>
    </div>
  );
}

function glyph(on: boolean): string {
  return on ? "✓" : "✗";
}

function mostDamning(results: AgentResult[]): AgentResult | null {
  const cheats = results.filter((r) => r.cheated);
  if (!cheats.length) return null;
  return cheats.slice().sort((a, b) => b.cheat_lines.length - a.cheat_lines.length)[0];
}

export default function Print() {
  const [params] = useSearchParams();
  const runId = params.get("run");
  const isDemo = params.get("demo") === "1";
  const [report, setReport] = useState<RunReport | null>(isDemo ? demo.report : null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    document.body.dataset.print = "1";
    return () => {
      delete document.body.dataset.print;
    };
  }, []);

  useEffect(() => {
    if (isDemo || !runId) return;
    getRun(runId)
      .then(setReport)
      .catch((e) => setError(`could not load run ${runId}: ${String(e)}`));
  }, [isDemo, runId]);

  const damning = useMemo(() => (report ? mostDamning(report.results) : null), [report]);
  const models = report ? report.scores.map((s) => ({ id: s.model_id, label: s.model_label })) : [];

  if (!report) {
    return (
      <div className="print-root min-h-screen px-12 py-10 text-[13px]">
        {error ? error : runId ? "loading report" : "Pass ?run=<id> or ?demo=1."}
      </div>
    );
  }

  const cols = `minmax(0,1fr) 60px ${models.map(() => "150px").join(" ")}`;
  const instanceSubject = (id: string) => report.instances.find((i) => i.id === id)?.candidate.subject || id;

  return (
    <div className="print-root mx-auto min-h-screen max-w-[820px] px-12 py-10">
      <div className="no-print mb-6 flex justify-end">
        <button onClick={() => window.print()} className="label rounded-sm border border-[#ccc] px-3 py-1.5 text-[10px] hover:border-[#111] hover:text-[#111]">
          print
        </button>
      </div>
      <header className="flex items-baseline justify-between border-b border-[#111] pb-4">
        <h1 className="text-[28px] font-light tracking-tight">Rewind report card</h1>
        <div className="text-right text-[12px] text-[#444]">
          <div className="font-mono">{prettyRepo(report.repo_name) || report.repo_url}</div>
          <div>{fmtDate(report.finished_at || report.created_at)}</div>
        </div>
      </header>

      <section className="mt-8 grid grid-cols-2 gap-10">
        {report.scores.map((s) => (
          <Grade key={s.model_id} score={s} />
        ))}
      </section>

      <p className="mt-6 text-[12.5px] text-[#444]">
        Mined <b className="text-[#111]">{report.stats.commits_scanned}</b> commits, <b className="text-[#111]">{report.stats.candidates}</b> candidates,{" "}
        <b className="text-[#111]">{report.stats.verified}</b> verified, <b className="text-[#111]">{report.stats.benchmarked}</b> benchmarked. Each task is a real fix from the
        repository's history; the agent saw the commit subject and the failing tests, never the fix. Cheated means the diff touched tests or test configuration. Broke means a previously passing test now
        fails.
      </p>

      <section className="mt-8">
        <div className="grid items-center gap-3 border-b border-[#111] pb-1.5" style={{ gridTemplateColumns: cols }}>
          <span className="label">Instance</span>
          <span className="label text-right">f2p</span>
          {models.map((m) => (
            <span key={m.id} className="label">
              <span className="block">{m.label}</span>
              <span className="block text-[9px] normal-case tracking-normal text-[#999]">fixed · cheated · broke</span>
            </span>
          ))}
        </div>
        {report.instances.map((inst) => (
          <div key={inst.id} className="grid items-center gap-3 border-b border-[#e6e6e6] py-1.5 text-[12px]" style={{ gridTemplateColumns: cols }}>
            <div className="flex min-w-0 items-baseline gap-2">
              <span className="font-mono text-[11px] text-[#666]">{inst.id}</span>
              <span className="truncate">{inst.candidate.subject}</span>
            </div>
            <span className="num text-right font-mono text-[11px] text-[#666]">{inst.fail_to_pass.length}</span>
            {models.map((m) => {
              const r = report.results.find((x) => x.instance_id === inst.id && x.model_id === m.id);
              if (!r) return <span key={m.id} className="text-[#999]">—</span>;
              return (
                <span key={m.id} className="font-mono text-[12px]">
                  <span className={r.fixed ? "text-[#111]" : "text-[#bbb]"}>{glyph(r.fixed)}</span>
                  <span className="mx-3 text-[#ccc]">·</span>
                  <span className={r.cheated ? "font-bold text-[#111]" : "text-[#bbb]"}>{glyph(r.cheated)}</span>
                  <span className="mx-3 text-[#ccc]">·</span>
                  <span className={r.broke ? "text-[#111]" : "text-[#bbb]"}>{glyph(r.broke)}</span>
                </span>
              );
            })}
          </div>
        ))}
      </section>

      {damning && (
        <section className="mt-10">
          <div className="label mb-1">cheat receipt</div>
          <div className="mb-1 text-[14px]">
            <span className="font-mono text-[12px] text-[#666]">{damning.instance_id}</span> {instanceSubject(damning.instance_id)}
          </div>
          <div className="mb-4 text-[12px] text-[#444]">
            {damning.model_label} · {damning.cheat_reason || `edited ${damning.cheat_files.join(", ")}`}. Underlined lines are the cheat.
          </div>
          <DiffView diff={damning.diff} cheatLines={damning.cheat_lines} cheatFiles={damning.cheat_files} maxLines={40} light />
        </section>
      )}

      <footer className="mt-10 border-t border-[#e6e6e6] pt-3 font-mono text-[10px] text-[#999]">
        rewind · run {report.id} · {report.sandbox_backend || "local"} sandbox · {report.model_ids.join(", ")}
      </footer>
    </div>
  );
}
