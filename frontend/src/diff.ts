// Unified diff parser. Line indexes are 0-based into diff.split("\n"), matching backend cheat_lines.

export type LineKind = "meta" | "hunk" | "add" | "del" | "ctx";

export interface DiffLine {
  idx: number;
  kind: LineKind;
  text: string;
}

export interface DiffFile {
  path: string;
  lines: DiffLine[];
}

function pathOf(line: string): string {
  const m = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
  if (m) return m[2];
  return line.replace(/^diff --git /, "");
}

export function parseDiff(diff: string): DiffFile[] {
  const files: DiffFile[] = [];
  let cur: DiffFile | null = null;
  const rows = diff.split("\n");
  if (rows.length && rows[rows.length - 1] === "") rows.pop();
  rows.forEach((raw, idx) => {
    if (raw.startsWith("diff --git")) {
      cur = { path: pathOf(raw), lines: [] };
      files.push(cur);
      return;
    }
    if (!cur) {
      cur = { path: "", lines: [] };
      files.push(cur);
    }
    if (raw.startsWith("+++ ") && !cur.path) cur.path = raw.slice(4).replace(/^[ab]\//, "");
    let kind: LineKind = "ctx";
    if (/^(\+\+\+ |--- |index |new file|deleted file|similarity|rename)/.test(raw)) kind = "meta";
    else if (raw.startsWith("@@")) kind = "hunk";
    else if (raw.startsWith("+")) kind = "add";
    else if (raw.startsWith("-")) kind = "del";
    cur.lines.push({ idx, kind, text: raw });
  });
  return files;
}

export function diffStats(diff: string): { add: number; del: number } {
  let add = 0;
  let del = 0;
  for (const l of diff.split("\n")) {
    if (l.startsWith("+") && !l.startsWith("+++")) add++;
    else if (l.startsWith("-") && !l.startsWith("---")) del++;
  }
  return { add, del };
}
