import type { BenchRow, Counters } from "../state";
import type { AgentResult } from "../types";
import { benchKey } from "../format";
import { FlagPills } from "./Pills";

const STEP_CAP = 15;

interface Props {
  rows: BenchRow[];
  results: AgentResult[];
  counters: Counters;
  subjects: Record<string, string>;
}

export default function BenchPanel({ rows, results, counters, subjects }: Props) {
  const byKey = new Map(results.map((r) => [benchKey(r.instance_id, r.model_id), r]));
  const active = rows.filter((r) => !r.done);
  const finished = rows.filter((r) => r.done);
  const ordered = [...active, ...finished.slice().reverse()];
  return (
    <section>
      <div className="mb-6 flex items-center gap-3">
        <span className="pulse inline-block h-1.5 w-1.5 rounded-full bg-accent" />
        <span className="text-[13px] text-muted">benchmarking</span>
        <span className="label num text-dim">
          · {counters.scanned} commits · {counters.candidates} candidates · {counters.verified} verified · {counters.discarded} discarded
        </span>
      </div>
      <div className="rounded-sm border hairline bg-surface">
        {ordered.length === 0 && <div className="px-4 py-6 text-[13px] text-dim">starting sandboxes</div>}
        {ordered.map((row) => {
          const res = byKey.get(row.key);
          const pct = Math.min(100, (row.step / STEP_CAP) * 100);
          return (
            <div key={row.key} className="grid grid-cols-[180px_160px_minmax(0,1fr)_200px] items-center gap-4 border-b hairline px-4 py-2.5 last:border-b-0">
              <div className="min-w-0">
                <div className="font-mono text-[12px] text-text">{row.instanceId}</div>
                <div className="truncate text-[11px] text-dim">{subjects[row.instanceId] || ""}</div>
              </div>
              <div className="text-[12.5px] text-muted">{row.modelLabel}</div>
              <div className="min-w-0">
                <div className="mb-1.5 truncate font-mono text-[12px]">
                  {res ? (
                    <span className="text-dim">{res.hit_cap ? "hit step cap" : res.final_message || "done"}</span>
                  ) : row.tool ? (
                    <>
                      <span className="text-dim">{String(row.step).padStart(2, " ")} </span>
                      <span className="text-text">{row.tool}</span> <span className="text-muted">{row.argsPreview}</span>
                    </>
                  ) : (
                    <span className="text-dim">waiting for first step</span>
                  )}
                </div>
                <div className="h-[2px] w-full bg-line">
                  <div className={`h-full transition-[width] duration-200 ${res ? "bg-line-2" : "bg-accent"}`} style={{ width: `${pct}%` }} />
                </div>
              </div>
              <div className="flex justify-end">
                {res ? <FlagPills result={res} /> : <span className="num font-mono text-[11px] text-dim">{row.step}/{STEP_CAP}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
