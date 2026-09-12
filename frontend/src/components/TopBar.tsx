import { useEffect, useState, type FormEvent } from "react";
import type { Health } from "../types";
import type { Mode } from "../useRun";

interface Props {
  mode: Mode;
  health: Health | null;
  busy: boolean;
  running: boolean;
  repoUrl: string;
  onRun: (url: string) => void;
  onReplay: () => void;
}

export default function TopBar({ mode, health, busy, running, repoUrl, onRun, onReplay }: Props) {
  const [value, setValue] = useState(repoUrl);
  useEffect(() => {
    if (repoUrl) setValue(repoUrl);
  }, [repoUrl]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (mode === "demo") {
      onReplay();
      return;
    }
    onRun(value);
  };

  const disabled = busy || running;

  return (
    <header className="border-b hairline">
      <div className="mx-auto flex max-w-[1360px] items-center justify-between gap-8 px-10 py-5">
        <div className="flex items-baseline gap-5">
          <span className="text-[22px] font-medium tracking-tight">Rewind</span>
          <span className="hidden text-[14px] text-muted md:inline">A benchmark your repository generates about itself.</span>
          {mode === "demo" && (
            <span className="label rounded-sm border border-line px-1.5 py-0.5 text-[10px] text-dim">demo</span>
          )}
        </div>
        <form onSubmit={submit} className="flex flex-col items-end gap-1.5">
          <div className="flex items-center gap-2">
            <input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="https://github.com/owner/repo"
              spellCheck={false}
              readOnly={mode === "demo"}
              className="h-9 w-[380px] rounded-sm border border-line bg-surface px-3 font-mono text-[12.5px] text-text outline-none placeholder:text-dim focus:border-line-2"
            />
            {mode === "demo" ? (
              <button type="submit" className="h-9 rounded-sm border border-line px-4 text-[13px] font-medium text-muted hover:border-line-2 hover:text-text">
                replay
              </button>
            ) : (
              <button
                type="submit"
                disabled={disabled}
                className="h-9 rounded-sm bg-accent px-5 text-[13px] font-semibold text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                {running ? "Running" : "Run"}
              </button>
            )}
          </div>
          {mode !== "demo" && health && (
            <div className="label text-[10px] text-dim">
              sandbox {health.sandbox} · {health.models.join(" · ")}
            </div>
          )}
        </form>
      </div>
    </header>
  );
}
