const STEPS: [string, string][] = [
  ["mine", "Walk the git history for commits that touch tests and source and read like bug fixes."],
  ["verify by construction", "Parent commit plus only the test hunks. The target test must fail there and pass at the fix."],
  ["run agents, grade", "Each model gets four tools and fifteen steps. Fixed, cheated, broke. The diff is the receipt."],
];

export default function EmptyState() {
  return (
    <section className="pt-24">
      <h1 className="max-w-3xl text-[44px] font-light leading-[1.1] tracking-tight">
        Point it at a repository. It mines its own bug fixes, rebuilds them as tasks, and grades coding agents on them.
      </h1>
      <ol className="mt-16 grid max-w-4xl grid-cols-1 gap-8 md:grid-cols-3">
        {STEPS.map(([title, body], i) => (
          <li key={title} className="border-t hairline pt-4">
            <div className="label mb-3">
              <span className="text-accent">0{i + 1}</span> {title}
            </div>
            <p className="text-[13.5px] leading-relaxed text-muted">{body}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}
