import type { BenchRow } from "../state";
import type { AgentResult } from "../types";
import { benchKey } from "../format";
import { FlagPills } from "./Pills";

export interface TableRow {
  id: string;
  subject: string;
  f2p: number;
}

interface Props {
  rows: TableRow[];
  models: { id: string; label: string }[];
  results: AgentResult[];
  bench: Record<string, BenchRow>;
  onOpen: (instanceId: string, modelId: string) => void;
}

export default function InstanceTable({ rows, models, results, bench, onOpen }: Props) {
  const byKey = new Map(results.map((r) => [benchKey(r.instance_id, r.model_id), r]));
  const cols = `minmax(0,1fr) 110px ${models.map(() => "220px").join(" ")}`;
  return (
    <section>
      <div className="grid items-center gap-4 border-b hairline px-3 pb-2" style={{ gridTemplateColumns: cols }}>
        <span className="label">Instance</span>
        <span className="label text-right">fail-to-pass</span>
        {models.map((m) => (
          <span key={m.id} className="label">
            {m.label}
          </span>
        ))}
      </div>
      {rows.map((row) => {
        const first = models.find((m) => byKey.get(benchKey(row.id, m.id)));
        return (
          <div
            key={row.id}
            className="grid cursor-pointer items-center gap-4 border-b hairline px-3 py-3 transition-colors hover:bg-surface"
            style={{ gridTemplateColumns: cols }}
            onClick={() => first && onOpen(row.id, first.id)}
          >
            <div className="flex min-w-0 items-baseline gap-3">
              <span className="font-mono text-[12px] text-accent">{row.id}</span>
              <span className="truncate text-[13.5px]">{row.subject}</span>
            </div>
            <span className="num text-right font-mono text-[12px] text-muted">{row.f2p}</span>
            {models.map((m) => {
              const key = benchKey(row.id, m.id);
              const res = byKey.get(key);
              const b = bench[key];
              return (
                <div
                  key={m.id}
                  className="min-w-0"
                  onClick={(e) => {
                    if (!res) return;
                    e.stopPropagation();
                    onOpen(row.id, m.id);
                  }}
                >
                  {res ? (
                    <FlagPills result={res} />
                  ) : b ? (
                    <span className="truncate font-mono text-[11px] text-dim">
                      step {b.step} · {b.tool || "starting"}
                    </span>
                  ) : (
                    <span className="text-dim">—</span>
                  )}
                </div>
              );
            })}
          </div>
        );
      })}
      {rows.length === 0 && <div className="px-3 py-6 text-[13px] text-dim">no verified instances yet</div>}
    </section>
  );
}
