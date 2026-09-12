import type { AgentResult } from "../types";

interface PillProps {
  label: string;
  on: boolean;
  tone: "accent" | "red" | "gray";
  size?: "xs" | "sm";
}

const FILL: Record<PillProps["tone"], string> = {
  accent: "bg-accent text-bg border-accent",
  red: "bg-red text-white border-red",
  gray: "bg-muted text-bg border-muted",
};

export function Pill({ label, on, tone, size = "xs" }: PillProps) {
  const dims = size === "xs" ? "px-1.5 py-[1px] text-[9px]" : "px-2 py-[2px] text-[10px]";
  const cls = on ? FILL[tone] : "border-line-2 text-dim";
  return <span className={`inline-block rounded-sm border font-semibold uppercase tracking-[0.12em] ${dims} ${cls}`}>{label}</span>;
}

export function FlagPills({ result, size = "xs" }: { result: AgentResult; size?: "xs" | "sm" }) {
  return (
    <span className="inline-flex gap-1">
      <Pill label="fixed" on={result.fixed} tone="accent" size={size} />
      <Pill label="cheated" on={result.cheated} tone="red" size={size} />
      <Pill label="broke" on={result.broke} tone="gray" size={size} />
    </span>
  );
}
