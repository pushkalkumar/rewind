import type { Event, Health, RunReport } from "./types";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

export async function getHealth(): Promise<Health> {
  return json<Health>(await fetch("/api/health"));
}

export async function startRun(repoUrl: string): Promise<{ id: string }> {
  const res = await fetch("/api/runs", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ repo_url: repoUrl }),
  });
  return json<{ id: string }>(res);
}

export async function getRun(id: string): Promise<RunReport> {
  return json<RunReport>(await fetch(`/api/runs/${encodeURIComponent(id)}`));
}

export async function getEvents(id: string, after = 0): Promise<Event[]> {
  return json<Event[]>(await fetch(`/api/runs/${encodeURIComponent(id)}/events?after=${after}`));
}

/** Subscribe to a run's SSE stream, skipping events with seq <= `after`. Returns a closer. */
export function streamRun(id: string, onEvent: (ev: Event) => void, onError: (err: string) => void, after = 0): () => void {
  const es = new EventSource(`/api/runs/${encodeURIComponent(id)}/stream?after=${after}`);
  let finished = false;
  es.onmessage = (msg) => {
    try {
      const ev = JSON.parse(msg.data) as Event;
      onEvent(ev);
      if (ev.type === "done" || ev.type === "failed") {
        finished = true;
        es.close();
      }
    } catch (e) {
      onError(`bad event: ${String(e)}`);
    }
  };
  es.onerror = () => {
    if (!finished) onError("stream disconnected");
    es.close();
  };
  return () => es.close();
}
