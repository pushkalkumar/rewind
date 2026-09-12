import { useEffect, useState } from "react";
import type { AgentResult, Instance } from "../types";
import { argsPreview } from "../format";
import DiffView from "./Diff";
import { FlagPills } from "./Pills";

interface Props {
  result: AgentResult;
  instance: Instance | null;
  subject: string;
  onClose: () => void;
}

function Section({ title, children, aside }: { title: string; children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <section className="border-t hairline px-8 py-6">
      <div className="mb-4 flex items-center justify-between">
        <div className="label">{title}</div>
        {aside}
      </div>
      {children}
    </section>
  );
}

export default function Receipt({ result, instance, subject, onClose }: Props) {
  const [showGold, setShowGold] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  useEffect(() => setShowGold(false), [result]);

  const issue = instance?.issue_text || subject;
  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/50" onClick={onClose} />
      <aside className="fixed inset-y-0 right-0 z-50 flex w-[55%] min-w-[520px] flex-col border-l hairline bg-surface shadow-2xl">
        <header className="flex items-start justify-between gap-6 px-8 pb-5 pt-6">
          <div className="min-w-0">
            <div className="label mb-2">
              receipt · <span className="normal-case tracking-normal text-accent">{result.instance_id}</span> · {result.model_label}
            </div>
            <h2 className="truncate text-[20px] font-medium leading-tight tracking-tight">{subject}</h2>
            <div className="mt-3 flex items-center gap-4">
              <FlagPills result={result} size="sm" />
              <span className="num font-mono text-[11px] text-dim">
                {result.steps_used} steps{result.hit_cap ? " · hit cap" : ""} · {Math.round(result.seconds)}s
              </span>
            </div>
            {result.cheated && result.cheat_reason && <div className="mt-3 text-[12.5px] text-red">{result.cheat_reason}</div>}
            {result.error && <div className="mt-3 font-mono text-[12px] text-red">{result.error}</div>}
          </div>
          <button onClick={onClose} className="label shrink-0 rounded-sm border border-line px-2 py-1 text-[10px] hover:border-line-2 hover:text-text" title="Close (Esc)">
            close
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <Section title="issue">
            <pre className="whitespace-pre-wrap font-sans text-[13.5px] leading-relaxed text-text">{issue}</pre>
            {instance && (
              <div className="mt-4 space-y-1 font-mono text-[11.5px] text-dim">
                {instance.fail_to_pass.map((t) => (
                  <div key={t} className="break-all">
                    <span className={result.f2p_results[t] === "passed" ? "text-accent" : "text-red"}>{result.f2p_results[t] === "passed" ? "pass" : "fail"}</span>{" "}
                    {t}
                  </div>
                ))}
              </div>
            )}
          </Section>
          <Section title="agent steps">
            <ol className="space-y-1 font-mono text-[12px]">
              {result.steps.map((s) => (
                <li key={s.step} className="grid grid-cols-[28px_92px_minmax(0,1fr)] gap-2">
                  <span className="num text-dim">{s.step}.</span>
                  <span className={s.tool === "write_file" ? "text-accent" : "text-text"}>{s.tool}</span>
                  <span className="truncate text-muted" title={s.thought || undefined}>
                    {argsPreview(s.args)}
                  </span>
                </li>
              ))}
              {result.steps.length === 0 && <li className="text-dim">no steps recorded</li>}
            </ol>
            {result.final_message && <p className="mt-4 text-[12.5px] leading-relaxed text-muted">{result.final_message}</p>}
          </Section>
          <Section title="diff" aside={<span className="num font-mono text-[11px] text-dim">{result.changed_files.length} files</span>}>
            <DiffView diff={result.diff} cheatLines={result.cheat_lines} cheatFiles={result.cheat_files} />
          </Section>
          {result.broken_tests.length > 0 && (
            <Section title="broken tests">
              <ul className="space-y-1 font-mono text-[11.5px] text-muted">
                {result.broken_tests.map((t) => (
                  <li key={t}>{t}</li>
                ))}
              </ul>
            </Section>
          )}
          {instance && (
            <Section
              title="maintainer's fix (hidden from agent)"
              aside={
                <button onClick={() => setShowGold((v) => !v)} className="label rounded-sm border border-line px-2 py-1 text-[10px] hover:border-line-2 hover:text-text">
                  {showGold ? "hide" : "show gold patch"}
                </button>
              }
            >
              {showGold ? <DiffView diff={instance.gold_patch} /> : <div className="text-[12.5px] text-dim">collapsed</div>}
            </Section>
          )}
        </div>
      </aside>
    </>
  );
}
