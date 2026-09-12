import { Link } from "react-router-dom";
import demoFile from "../demo/run.json";
import { prettyRepo } from "../format";
import type { DemoFile } from "../types";
import DiffView from "./Diff";

const STEPS: [string, string][] = [
  ["mine", "Walk the git history for commits that touch tests and source and read like bug fixes."],
  ["verify by construction", "Parent commit plus only the test hunks. The target test must fail there and pass at the fix."],
  ["run agents, grade", "Each model gets four tools and fifteen steps. Fixed, cheated, broke. The diff is the receipt."],
];

// The specimen is a real receipt from the captured run: the one-line URL-validator fix, as Sonnet wrote it.
const SPECIMEN_INSTANCE = "902f99c415";
const SPECIMEN_MODEL = "claude-cli:sonnet";
const demo = demoFile as unknown as DemoFile;
const specimen = demo.report.results.find((r) => r.instance_id === SPECIMEN_INSTANCE && r.model_id === SPECIMEN_MODEL) ?? null;
const specimenRepo = prettyRepo(demo.report.repo_name);

function focusRepoInput() {
  const el = document.getElementById("repo-url") as HTMLInputElement | null;
  el?.focus();
  el?.select();
}

export default function EmptyState() {
  return (
    <section className="pt-6">
      <h1 className="hero max-w-[980px]">
        Point it at a repository. It mines its own bug fixes, rebuilds them as tasks, and grades coding agents on them.
      </h1>
      <div className="mt-8 flex items-center gap-6">
        <Link
          to={{ pathname: "/", search: "?demo=1" }}
          className="inline-flex h-10 items-center rounded-sm bg-accent px-5 text-[13px] font-semibold text-bg transition-opacity hover:opacity-90"
        >
          Play the demo run
        </Link>
        <button type="button" onClick={focusRepoInput} className="rounded-sm text-body text-muted transition-colors hover:text-text">
          or paste a GitHub URL above
        </button>
      </div>

      <div className="mt-14 grid grid-cols-[minmax(0,5fr)_minmax(0,7fr)] gap-16">
        <ol className="space-y-6">
          {STEPS.map(([title, body], i) => (
            <li key={title} className="border-t hairline pt-4">
              <div className="label mb-2">
                <span className="num text-accent">0{i + 1}</span> {title}
              </div>
              <p className="max-w-[420px] text-body text-muted">{body}</p>
            </li>
          ))}
        </ol>

        {specimen && (
          <figure className="min-w-0">
            <figcaption className="label mb-3 flex flex-wrap items-baseline gap-x-2">
              <span>receipt</span>
              <span className="text-dim">·</span>
              <span>{specimenRepo}</span>
              <span className="text-dim">·</span>
              <span className="font-mono normal-case tracking-normal text-accent">{specimen.instance_id}</span>
              <span className="text-dim">·</span>
              <span>{specimen.model_label}</span>
              <span className="text-dim">·</span>
              <span className="num">
                {specimen.fixed ? "fixed" : "not fixed"} in {specimen.steps_used} steps
              </span>
            </figcaption>
            <DiffView diff={specimen.diff} cheatLines={specimen.cheat_lines} cheatFiles={specimen.cheat_files} />
          </figure>
        )}
      </div>
    </section>
  );
}
