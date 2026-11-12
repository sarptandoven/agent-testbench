/** The JSON the CLI prints, as the extension reads it: `testbench plans --json`, `runs --json`, `spend --json`,
 * `loop status --json` and `check --json`. */

export type Launch = 'allowed' | 'needs_approval' | 'refused';

export interface Limit {
  usd: number;
  where: string;
}

export interface PlanInfo {
  name: string;
  backend: string;
  gpu: string | null;
  description: string | null;
  steps: string[];
  max_minutes: number;
  worst_case_usd: number;
  /** allowed as it is, only once a person approves it, or refused whatever anyone says */
  launch: Launch;
  /** testbench's own words: "Within budget: ...", "Needs approval: ...", "Refused: ..." */
  message: string;
}

export interface PlansJson {
  project: string;
  spent_today_usd: number;
  limits: Record<'max_run_usd' | 'ask_above_usd' | 'max_day_usd', Limit>;
  plans: PlanInfo[];
}

export type RunStatus = 'queued' | 'running' | 'passed' | 'failed' | 'cancelled' | 'error' | 'lost';

export interface RunInfo {
  run_id: string;
  plan: string | null;
  status: RunStatus | string | null;
  backend: string | null;
  gpu: string | null;
  created: number | null;
  cost_usd: number | null;
  worst_case_usd: number | null;
  approved: boolean | null;
  step: string | null;
  dir: string;
  has_report: boolean;
  line: string;
}

export interface RunsJson {
  /** oldest first, as `testbench runs` lists them */
  runs: RunInfo[];
}

export interface DayTotal {
  today_usd: number;
  day_limit_usd: number | null;
  where: string;
}

export interface SpendJson {
  project: DayTotal;
  machine: DayTotal;
  total_usd: number;
  finished: number;
}

export interface LoopTry {
  n: number;
  mark: 'baseline' | 'running' | 'kept' | 'reverted' | string;
  value: number | null;
  cost_usd: number | null;
  worst_case_usd: number | null;
  run: string | null;
  description: string | null;
  verdict: string | null;
}

export interface LoopInfo {
  id: string;
  status: string;
  plan: string;
  goal: string;
  metric: string;
  key: string;
  best: { value: number | null; commit: string; try: number | null } | null;
  budget_usd: number;
  spent_usd: number;
  hours_left: number | null;
  branch: string;
  original_branch: string;
  ended_by: string | null;
  rules: string | null;
  halt: string | null;
  /** the deflated Sharpe line and the PBO line, for a loop over a strategy */
  luck: string[];
  luck_why: string | null;
  pending: number | null;
  pending_run_ended: boolean;
  tries: LoopTry[];
}

export interface LoopJson {
  loop: LoopInfo | null;
}

export interface CheckPlan {
  plan: string;
  what: string;
  status: string | null;
  run: string | null;
  seconds: number | null;
  why: string | null;
  install: string | null;
}

export interface CheckJson {
  plans: CheckPlan[];
  passed: number;
  failed: number;
  skipped: number;
  refused: number;
  not_run: number;
  still_running: number;
  runs: string[];
  message: string | null;
  next: string;
  exit_code: number;
}
