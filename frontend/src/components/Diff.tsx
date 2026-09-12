import { useMemo } from "react";
import Prism from "prismjs";
import "prismjs/components/prism-python";
import { diffStats, parseDiff, type DiffFile, type DiffLine } from "../diff";

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function highlight(code: string, python: boolean): string {
  if (!python) return escapeHtml(code);
  try {
    return Prism.highlight(code, Prism.languages.python, "python");
  } catch {
    return escapeHtml(code);
  }
}

interface NumberedLine extends DiffLine {
  oldNo: number | null;
  newNo: number | null;
}

function numberLines(file: DiffFile): NumberedLine[] {
  let oldNo = 0;
  let newNo = 0;
  return file.lines.map((l) => {
    if (l.kind === "hunk") {
      const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(l.text);
      if (m) {
        oldNo = parseInt(m[1], 10);
        newNo = parseInt(m[2], 10);
      }
      return { ...l, oldNo: null, newNo: null };
    }
    if (l.kind === "add") return { ...l, oldNo: null, newNo: newNo++ };
    if (l.kind === "del") return { ...l, oldNo: oldNo++, newNo: null };
    if (l.kind === "ctx") return { ...l, oldNo: oldNo++, newNo: newNo++ };
    return { ...l, oldNo: null, newNo: null };
  });
}

interface Props {
  diff: string;
  cheatLines?: number[];
  cheatFiles?: string[];
  maxLines?: number;
  light?: boolean;
}

export default function DiffView({ diff, cheatLines = [], cheatFiles = [], maxLines, light }: Props) {
  const files = useMemo(() => parseDiff(diff), [diff]);
  const cheat = useMemo(() => new Set(cheatLines), [cheatLines]);
  const cheatFileSet = useMemo(() => new Set(cheatFiles), [cheatFiles]);
  if (!diff.trim()) return <div className="text-[13px] text-dim">no changes</div>;
  let budget = maxLines ?? Infinity;
  return (
    <div className="space-y-4">
      {files.map((file, fi) => {
        if (budget <= 0) return null;
        const python = file.path.endsWith(".py");
        const lines = numberLines(file);
        const flagged = cheatFileSet.has(file.path) || lines.some((l) => cheat.has(l.idx));
        const shown = lines.slice(0, budget);
        budget -= shown.length;
        const stats = diffStats(file.lines.map((l) => l.text).join("\n"));
        return (
          <div key={`${file.path}-${fi}`} className={`overflow-hidden rounded-sm border ${flagged ? (light ? "border-[#111]" : "border-red") : light ? "border-[#ddd]" : "border-line"}`}>
            <div className={`flex items-center justify-between gap-4 border-b px-3 py-2 ${flagged ? (light ? "border-[#111] bg-[#f6f6f6]" : "border-red") : light ? "border-[#ddd] bg-[#f6f6f6]" : "border-line bg-surface-2"}`}>
              <div className="flex min-w-0 items-center gap-3">
                <span className={`truncate font-mono text-[12px] ${light ? "text-[#111]" : "text-text"}`}>{file.path || "(unnamed)"}</span>
                {flagged && <span className={`rounded-sm px-1.5 ${light ? "bg-[#111]" : "bg-red"} py-[1px] text-[9px] font-bold uppercase tracking-[0.14em] text-white`}>cheat</span>}
              </div>
              <span className="num shrink-0 font-mono text-[11px] text-dim">
                +{stats.add} −{stats.del}
              </span>
            </div>
            <div className="overflow-x-auto py-1">
              {shown.map((l) => {
                if (l.kind === "meta") return null;
                const isCheat = cheat.has(l.idx);
                const cls = `diff-line ${l.kind}${isCheat ? " cheat" : ""}`;
                if (l.kind === "hunk") {
                  return (
                    <div key={l.idx} className={cls}>
                      <span className="ln" />
                      <span className="code">{l.text}</span>
                    </div>
                  );
                }
                const sign = l.text[0] ?? " ";
                const body = l.text.slice(1);
                return (
                  <div key={l.idx} className={cls}>
                    <span className="ln">{l.kind === "del" ? l.oldNo : l.newNo}</span>
                    <span className="code">
                      <span className={isCheat ? "" : l.kind === "add" ? "text-add" : l.kind === "del" ? "text-del" : "text-dim"}>{sign}</span>
                      <span dangerouslySetInnerHTML={{ __html: highlight(body, python) }} />
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
