// Mirrors backend/rewind/models.py exactly. Keep in sync by hand.

export type RunStatus = "queued" | "cloning" | "mining" | "verifying" | "benchmarking" | "done" | "failed";
export type TestStatus = "passed" | "failed" | "error" | "skipped";

export interface Candidate {
  sha: string;
  parent_sha: string;
  subject: string;
  message: string;
  author_date: string;
  test_files: string[];
  source_files: string[];
}

export interface TestResult {
  nodeid: string;
  status: TestStatus;
  message: string;
}

export interface Instance {
  id: string;
  candidate: Candidate;
  issue_text: string;
  fail_to_pass: string[];
  pass_to_pass: string[];
  test_patch: string;
  gold_patch: string;
  test_files: string[];
  verify_seconds: number;
}

export interface DiscardedCandidate {
  sha: string;
  subject: string;
  reason: string;
}

export interface MinedStats {
  commits_scanned: number;
  candidates: number;
  verified: number;
  discarded: number;
  benchmarked: number;
  discard_reasons: Record<string, number>;
  discarded_list: DiscardedCandidate[];
}

export interface ToolCall {
  step: number;
  tool: string;
  args: Record<string, unknown>;
  result_preview: string;
  seconds: number;
  thought: string;
}

export interface AgentResult {
  instance_id: string;
  model_id: string;
  model_label: string;
  steps: ToolCall[];
  steps_used: number;
  hit_cap: boolean;
  diff: string;
  changed_files: string[];
  fixed: boolean;
  cheated: boolean;
  broke: boolean;
  cheat_files: string[];
  cheat_lines: number[];
  cheat_reason: string;
  broken_tests: string[];
  f2p_results: Record<string, TestStatus>;
  seconds: number;
  error: string | null;
  final_message: string;
}

export interface ModelScore {
  model_id: string;
  model_label: string;
  n: number;
  fixed: number;
  cheated: number;
  broke: number;
  honest: number;
  score: number;
  grade: string;
}

export interface RunReport {
  id: string;
  repo_url: string;
  repo_name: string;
  status: RunStatus;
  created_at: string;
  finished_at: string;
  sandbox_backend: string;
  model_ids: string[];
  stats: MinedStats;
  instances: Instance[];
  results: AgentResult[];
  scores: ModelScore[];
  error: string | null;
}

export interface Event {
  seq: number;
  t: number;
  type: string;
  data: Record<string, any>;
}

export interface DemoFile {
  report: RunReport;
  events: Event[];
}

export interface Health {
  ok: boolean;
  sandbox: string;
  models: string[];
}
