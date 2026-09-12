// Unit-style checks for the demo replay schedule, the diff parser and the event reducer.
// Run from frontend/:
//   npx esbuild src/demo/replay.test.ts --bundle --platform=node --format=esm --outfile=<tmp>/replay.test.mjs && node <tmp>/replay.test.mjs
import demoFile from "./run.json";
import { BENCH_MIN_MS, MINING_MAX_MS, SCAN_GAP_MS, TOTAL_MS, phases, schedule, type Phase } from "./replay";
import { parseDiff } from "../diff";
import { instanceIdOf } from "../format";
import { applyEvents, initialState, reducer } from "../state";
import type { DemoFile, Event } from "../types";

const PHASE_ORDER: Phase[] = ["mining", "verifying", "benchmarking"];

let failures = 0;
function check(cond: boolean, msg: string): void {
  if (cond) console.log(`  ok   ${msg}`);
  else {
    failures++;
    console.log(`  FAIL ${msg}`);
  }
}

function phaseSpan(ph: Phase[], times: number[], phase: Phase): { first: number; last: number; n: number } {
  const ts = times.filter((_, i) => ph[i] === phase);
  return { first: ts.length ? ts[0] : NaN, last: ts.length ? ts[ts.length - 1] : NaN, n: ts.length };
}

function checkSchedule(name: string, events: Event[]): void {
  console.log(name);
  const times = schedule(events);
  const ph = phases(events);
  const mining = phaseSpan(ph, times, "mining");
  const verifying = phaseSpan(ph, times, "verifying");
  const bench = phaseSpan(ph, times, "benchmarking");
  const miningEnd = mining.n ? mining.last : 0;
  const verifyEnd = verifying.n ? verifying.last : miningEnd;
  const verifyLen = verifying.n ? verifying.last - miningEnd : 0;
  const benchLen = bench.n ? bench.last - verifyEnd : 0;
  const total = times[times.length - 1];
  check(times.length === events.length, `one time per event (${events.length})`);
  check(
    times.every((t, i) => i === 0 || t >= times[i - 1]),
    "times are monotonic (relative order kept)",
  );
  check(
    ph.every((p, i) => i === 0 || PHASE_ORDER.indexOf(p) >= PHASE_ORDER.indexOf(ph[i - 1])),
    "phases never go backwards",
  );
  check(miningEnd <= MINING_MAX_MS + 1e-6, `cloning+mining ends by ${MINING_MAX_MS}ms (ended ${miningEnd.toFixed(0)}ms, ${mining.n} events)`);
  const scanGaps: number[] = [];
  for (let i = 1; i < events.length; i++) if (events[i].type === "mine.scan" && ph[i] === "mining") scanGaps.push(times[i] - times[i - 1]);
  const scanCount = events.filter((e) => e.type === "mine.scan").length;
  if (scanCount * SCAN_GAP_MS <= MINING_MAX_MS) {
    check(
      scanGaps.every((g) => g >= SCAN_GAP_MS - 1e-6),
      `mine.scan gaps >= ${SCAN_GAP_MS}ms (min ${Math.min(...scanGaps).toFixed(1)}ms over ${scanGaps.length})`,
    );
  } else {
    check(
      scanGaps.every((g) => g > 0),
      `mine.scan gaps stay positive when ${scanCount} scans cannot fit at ${SCAN_GAP_MS}ms`,
    );
  }
  check(Math.abs(verifyLen - 5000) < 1, `verifying phase ~5s (${verifyLen.toFixed(0)}ms, ${verifying.n} events)`);
  check(benchLen >= BENCH_MIN_MS - 1e-6, `benchmarking phase >= ${BENCH_MIN_MS}ms (${benchLen.toFixed(0)}ms, ${bench.n} events)`);
  const benchGaps: number[] = [];
  for (let i = 1; i < events.length; i++) if (ph[i] === "benchmarking" && ph[i - 1] === "benchmarking") benchGaps.push(times[i] - times[i - 1]);
  const gmin = Math.min(...benchGaps);
  const gmax = Math.max(...benchGaps);
  check(benchGaps.length === 0 || gmax - gmin < 1e-6, `bench events evenly spaced (gap ${gmin.toFixed(1)}ms)`);
  check(benchGaps.length === 0 || gmin >= 60, `every bench.step visible (gap ${gmin.toFixed(1)}ms >= 60ms)`);
  check(total <= TOTAL_MS + 1e-6, `total <= ${TOTAL_MS}ms (${total.toFixed(0)}ms)`);
}

function fakeSha(i: number): string {
  return `${i.toString(16).padStart(8, "0")}deadbeefcafe0123456789abcdef01234567`.slice(0, 40);
}

function synthetic(scans: number): Event[] {
  const ev: Event[] = [];
  let seq = 0;
  let t = 0;
  const push = (type: string, data: Record<string, unknown>, dt: number) => {
    t += dt;
    ev.push({ seq: ++seq, t, type, data });
  };
  push("status", { status: "queued" }, 0);
  push("status", { status: "cloning" }, 0.1);
  push("clone.done", { repo_name: "x/y", commits: scans }, 40);
  push("status", { status: "mining" }, 0.1);
  for (let i = 0; i < scans; i++) {
    const cand = i % 40 === 0;
    push("mine.scan", { sha: fakeSha(i), subject: `commit ${i}`, candidate: cand }, 0.02);
    if (cand) push("mine.candidate", { sha: fakeSha(i), subject: `commit ${i}`, test_files: [], source_files: [] }, 0.01);
  }
  push("status", { status: "verifying" }, 0.5);
  for (let i = 0; i < 10; i++) {
    push("verify.start", { sha: fakeSha(i * 40), subject: `commit ${i * 40}` }, 0.3);
    if (i % 2 === 0) push("verify.keep", { sha: fakeSha(i * 40), subject: "s", fail_to_pass: ["t"], pass_to_pass_count: 3, seconds: 8 }, 8);
    else push("verify.discard", { sha: fakeSha(i * 40), subject: "s", reason: "timeout" }, 6);
  }
  push("mine.done", { stats: {} }, 0.2);
  push("status", { status: "benchmarking" }, 0.1);
  for (let i = 0; i < 5; i++) {
    const id = instanceIdOf(fakeSha(i * 80));
    push("bench.start", { instance_id: id, model_id: "m", model_label: "M" }, 0.5);
    for (let s = 1; s <= 12; s++) push("bench.step", { instance_id: id, model_id: "m", step: s, tool: "read_file", args_preview: "a.py", result_preview: "" }, 3);
    push("bench.result", { result: { instance_id: id, model_id: "m", model_label: "M", steps_used: 12, cheated: false, fixed: true, broke: false } }, 2);
  }
  push("scores", { scores: [] }, 0.2);
  push("status", { status: "done" }, 0.1);
  push("done", { report: null }, 0.05);
  return ev;
}

const demo = demoFile as unknown as DemoFile;
checkSchedule("schedule: current run.json", demo.events);
const synth = synthetic(400);
check(synth.filter((e) => e.type === "mine.scan").length === 400, "synthetic list has 400 mine.scan events");
checkSchedule("schedule: synthetic 400 scans", synth);
checkSchedule("schedule: synthetic 1200 scans (over budget, still capped)", synthetic(1200));

console.log("diff parser");
{
  const d = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-a\n+b\n c\n";
  const rows = d.split("\n");
  const files = parseDiff(d);
  const all = files.flatMap((f) => f.lines);
  check(files.length === 1 && files[0].path === "x.py", "one file, path parsed");
  // rows: 8 = "diff --git" header (consumed) + 6 content lines + trailing "" from the final newline.
  check(all.length === rows.length - 2 && !all.some((l) => l.idx === rows.length - 1), `trailing newline adds no empty row (${all.length} rows)`);
  check(
    all.every((l) => rows[l.idx] === l.text),
    "line idx indexes into diff.split(newline)",
  );
  check(all.find((l) => l.text === "+b")?.idx === 5, "cheat_lines index 5 maps to +b");
  const noTrail = parseDiff(d.trimEnd()).flatMap((f) => f.lines);
  check(noTrail.length === all.length, "same rows without trailing newline");
  check(parseDiff("").length === 0, "empty diff has no files");
}

console.log("reducer");
{
  const s1 = applyEvents(initialState, demo.events);
  const s2 = applyEvents(s1, demo.events);
  check(s1.counters.scanned === demo.report.stats.commits_scanned, `scanned counter matches stats (${s1.counters.scanned})`);
  check(
    s2.counters.scanned === s1.counters.scanned && s2.log.length === s1.log.length && s2.verified.length === s1.verified.length,
    "re-applying the same events is a no-op",
  );
  const fresh = reducer(s1, { type: "reset" });
  check(fresh.lastSeq === 0, "reset clears lastSeq");
  const sha = "0123456789abcdef0123456789abcdef01234567";
  const keep: Event = { seq: 1, t: 0, type: "verify.keep", data: { sha, subject: "s", fail_to_pass: ["t"], pass_to_pass_count: 0, seconds: 1 } };
  const v = applyEvents(initialState, [keep]).verified[0];
  check(v.id === "0123456789" && v.id === instanceIdOf(sha), `verify.keep row id is sha[:10] (${v.id})`);
  const bench: Event = { seq: 2, t: 1, type: "bench.start", data: { instance_id: "0123456789", model_id: "m", model_label: "M" } };
  const b = applyEvents(initialState, [keep, bench]);
  check(b.verified[0].id === "0123456789" && b.benchOrder[0] === "0123456789|m", "bench row key matches verified row id");
  const done = applyEvents(initialState, demo.events);
  const keepOrder = demo.events.filter((e) => e.type === "verify.keep").map((e) => e.data.sha as string);
  const ids = done.report!.instances.map((i) => i.id);
  check(
    ids.every((id, k) => keepOrder[k].startsWith(id)),
    `report.instances follow verify.keep order (${ids.join(",")})`,
  );
  check(
    done.verified.every((vv, k) => vv.id === ids[k]),
    "verified rows and report rows share order and ids",
  );
}

if (failures) {
  console.log(`\n${failures} check(s) failed`);
  throw new Error(`${failures} check(s) failed`);
}
console.log("\nall checks passed");
