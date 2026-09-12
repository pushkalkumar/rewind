import type { Counters, LogLine } from "../state";
import type { RunStatus } from "../types";

interface Props {
  status: RunStatus | "idle";
  message: string;
  counters: Counters;
  log: LogLine[];
}

const KIND_CLASS: Record<LogLine["kind"], string> = {
  scan: "text-dim",
  candidate: "text-text",
  keep: "text-accent",
  discard: "text-muted",
  info: "text-muted",
  bench: "text-muted",
};

function Counter({ label, value, live }: { label: string; value: number; live?: boolean }) {
  return (
    <div>
      <div className="label mb-2">{label}</div>
      <div className={`num text-[64px] font-light leading-none tracking-tight ${live ? "text-text" : "text-muted"}`}>{value}</div>
    </div>
  );
}

export default function MiningPanel({ status, message, counters, log }: Props) {
  const tail = log.slice(-8);
  const phase = status === "cloning" ? "cloning" : status === "verifying" ? "verifying by construction" : "mining git history";
  return (
    <section className="grid grid-cols-1 gap-10 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div>
        <div className="mb-8 flex items-center gap-3">
          <span className="pulse inline-block h-1.5 w-1.5 rounded-full bg-accent" />
          <span className="text-[13px] text-muted">
            {phase}
            {message ? <span className="text-dim"> · {message}</span> : null}
          </span>
        </div>
        <div className="grid grid-cols-3 gap-6">
          <Counter label="Candidates found" value={counters.candidates} live={status === "mining" || status === "cloning"} />
          <Counter label="Verified" value={counters.verified} live={status === "verifying"} />
          <Counter label="Discarded" value={counters.discarded} live={status === "verifying"} />
        </div>
        <div className="label mt-6 text-dim">{counters.scanned} commits scanned</div>
      </div>
      <div className="min-w-0 rounded-sm border hairline bg-surface p-4">
        <div className="label mb-3 text-dim">log</div>
        <div className="flex h-[196px] flex-col justify-end overflow-hidden font-mono text-[12px] leading-[22px]">
          {tail.map((l) => (
            <div key={l.seq} className={`log-line ${KIND_CLASS[l.kind]}`}>
              {l.text}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
