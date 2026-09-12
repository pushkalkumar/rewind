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
        const openable = Boolean(first);
        return (
          <div
            key={row.id}
            role={openable ? "button" : undefined}
            tabIndex={openable ? 0 : undefined}
            className={`group grid items-center gap-4 rounded-sm border-b hairline px-3 py-3 transition-colors ${openable ? "cursor-pointer hover:bg-surface-2 focus-visible:bg-surface-2" : ""}`}
            style={{ gridTemplateColumns: cols }}
            onClick={() => first && onOpen(row.id, first.id)}
            onKeyDown={(e) => {
              if (first && (e.key === "Enter" || e.key === " ")) {
                e.preventDefault();
                onOpen(row.id, first.id);
              }
            }}
          >
            <div className="flex min-w-0 items-baseline gap-3">
              <span className="font-mono text-[12px] text-accent">{row.id}</span>
              <span className="truncate text-row">{row.subject}</span>
            </div>
            <span className="num text-right font-mono text-[12px] text-muted">{row.f2p}</span>
            {models.map((m, mi) => {
              const key = benchKey(row.id, m.id);
              const res = byKey.get(key);
              const b = bench[key];
              const last = mi === models.length - 1;
              return (
                <div
                  key={m.id}
                  className="flex min-w-0 items-center justify-between gap-3"
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
                  {last && openable && (
                    <span className="label shrink-0 text-muted opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">receipt</span>
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
