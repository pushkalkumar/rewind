import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getEvents, getHealth, getRun, startRun, streamRun } from "./api";
import demoFile from "./demo/run.json";
import { replay } from "./demo/replay";
import { initialState, reducer, type UIState } from "./state";
import type { DemoFile, Health } from "./types";

export type Mode = "idle" | "demo" | "live";

const demo = demoFile as unknown as DemoFile;

export interface RunController {
  state: UIState;
  mode: Mode;
  health: Health | null;
  busy: boolean;
  run: (repoUrl: string) => Promise<void>;
  replayDemo: () => void;
}

export function useRun(): RunController {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [params, setParams] = useSearchParams();
  const [health, setHealth] = useState<Health | null>(null);
  const [busy, setBusy] = useState(false);
  const [replayNonce, setReplayNonce] = useState(0);
  const cancelRef = useRef<() => void>(() => {});

  const isDemo = params.get("demo") === "1";
  const runId = params.get("run");
  const mode: Mode = isDemo ? "demo" : runId || state.status !== "idle" ? "live" : "idle";

  useEffect(() => {
    if (isDemo) return;
    getHealth()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, [isDemo]);

  // demo replay (auto-starts; re-runs when replayNonce changes)
  useEffect(() => {
    if (!isDemo) return;
    cancelRef.current();
    dispatch({ type: "reset", repoUrl: demo.report.repo_url });
    const cancel = replay(
      demo.events,
      (ev) => dispatch({ type: "event", event: ev }),
      () => dispatch({ type: "report", report: demo.report }),
    );
    cancelRef.current = cancel;
    return cancel;
  }, [isDemo, replayNonce]);

  // ?run=<id>: load history, then follow the stream if still running
  useEffect(() => {
    if (isDemo || !runId) return;
    let closed = false;
    let closeStream: () => void = () => {};
    dispatch({ type: "reset" });
    (async () => {
      try {
        const events = await getEvents(runId);
        if (closed) return;
        let lastSeq = 0;
        for (const ev of events) {
          dispatch({ type: "event", event: ev });
          lastSeq = Math.max(lastSeq, Number(ev.seq) || 0);
        }
        const report = await getRun(runId);
        if (closed) return;
        dispatch({ type: "report", report });
        if (report.status !== "done" && report.status !== "failed") {
          // Resume the stream after the history we already folded; the reducer drops any overlap anyway.
          closeStream = streamRun(runId, (ev) => dispatch({ type: "event", event: ev }), (err) => dispatch({ type: "error", error: err }), lastSeq);
        }
      } catch (e) {
        if (!closed) dispatch({ type: "error", error: `could not load run ${runId}: ${String(e)}` });
      }
    })();
    return () => {
      closed = true;
      closeStream();
    };
  }, [isDemo, runId]);

  const run = useCallback(
    async (repoUrl: string) => {
      const url = repoUrl.trim();
      if (!url) return;
      setBusy(true);
      try {
        const { id } = await startRun(url);
        setParams({ run: id }, { replace: true });
      } catch (e) {
        dispatch({ type: "reset", repoUrl: url });
        dispatch({ type: "error", error: `could not start run: ${String(e)}` });
      } finally {
        setBusy(false);
      }
    },
    [setParams],
  );

  const replayDemo = useCallback(() => setReplayNonce((n) => n + 1), []);

  return { state, mode, health, busy, run, replayDemo };
}
