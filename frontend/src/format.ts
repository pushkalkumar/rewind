export function shortSha(sha: string, n = 7): string {
  return (sha || "").slice(0, n);
}

/** Instance id as the backend derives it (Instance.id = sha[:10]). Display may still shorten it. */
export const INSTANCE_ID_LEN = 10;
export function instanceIdOf(sha: string): string {
  return (sha || "").slice(0, INSTANCE_ID_LEN);
}

export function fraction(a: number, b: number): string {
  return `${a}/${b}`;
}

export function fmtDate(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

export function repoNameFromUrl(url: string): string {
  const m = /github\.com[/:]([^/]+)\/([^/.]+)/.exec(url || "");
  return m ? `${m[1]}/${m[2]}` : url;
}

/** The backend names clones owner__repo; show owner/repo. */
export function prettyRepo(name: string): string {
  return name ? name.replace("__", "/") : name;
}

export function benchKey(instanceId: string, modelId: string): string {
  return `${instanceId}|${modelId}`;
}

export function argsPreview(args: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(args)) {
    if (k === "content" && typeof v === "string") parts.push(`content=<${v.length} chars>`);
    else if (k === "summary" && typeof v === "string") parts.push(JSON.stringify(v.length > 80 ? v.slice(0, 80) + "…" : v));
    else parts.push(String(v));
  }
  return parts.join(" ");
}
