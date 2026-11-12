/** What the Testbench views show, built from the CLI's JSON: plain data, turned into tree items by views.ts. Every text
 * here is plain text (labels, descriptions and tooltips are never Markdown), so a plan name or a loop try's description
 * written by an agent cannot become a link or a command. */
import { COMMANDS } from './commands';
import { money } from './names';
import type { LoopJson, PlanInfo, PlansJson, RunInfo, RunsJson } from './types';

export interface Icon {
  /** a codicon id, e.g. "pass" or "sync~spin" */
  id: string;
  /** a theme color id, e.g. "testing.iconPassed" */
  color?: string;
}

export interface Node {
  id: string;
  label: string;
  description?: string;
  tooltip?: string;
  icon?: Icon;
  /** for the views' menus: plan.allowed, plan.needs_approval, plan.refused, run.going, run.done */
  contextValue?: string;
  children?: Node[];
  expanded?: boolean;
  command?: { command: string; title: string; arguments: unknown[] };
}

export function message(id: string, label: string, icon = 'info', tooltip?: string): Node {
  return { id: `message:${id}`, label, tooltip: tooltip ?? label, icon: { id: icon } };
}

function hardware(x: { backend: string | null; gpu: string | null }): string {
  return (x.backend ?? '?') + (x.gpu ? ` ${x.gpu}` : '');
}

const PLAN_ICONS: Record<PlanInfo['launch'], Icon> = {
  allowed: { id: 'play-circle' },
  needs_approval: { id: 'shield', color: 'list.warningForeground' },
  refused: { id: 'circle-slash', color: 'list.errorForeground' },
};

export function planNode(p: PlanInfo): Node {
  const cost = p.worst_case_usd === 0 ? 'free' : `up to ${money(p.worst_case_usd)}`;
  const state = p.launch === 'needs_approval' ? ' · needs your approval' : p.launch === 'refused' ? ' · refused' : '';
  return {
    id: `plan:${p.name}`,
    label: p.name,
    description: `${hardware(p)} · ${cost}${state}`,
    tooltip: [
      p.description,
      `${hardware(p)}, ${p.steps.length} step${p.steps.length === 1 ? '' : 's'}: ${p.steps.join(', ')}`,
      `at most ${Math.round(p.max_minutes)} min, worst case ${money(p.worst_case_usd)}`,
      p.message,
    ].filter(Boolean).join('\n'),
    icon: PLAN_ICONS[p.launch] ?? { id: 'question' },
    contextValue: `plan.${p.launch}`,
  };
}

export function planNodes(data: PlansJson): Node[] {
  if (data.plans.length === 0) {
    return [message('no-plans', 'testbench.yaml has no plans yet', 'info', '`testbench recipes` lists ready plans for common goals')];
  }
  return data.plans.map(planNode);
}

const RUN_ICONS: Record<string, Icon> = {
  passed: { id: 'pass', color: 'testing.iconPassed' },
  failed: { id: 'error', color: 'testing.iconFailed' },
  error: { id: 'warning', color: 'testing.iconErrored' },
  cancelled: { id: 'circle-slash' },
  queued: { id: 'clock' },
  running: { id: 'sync~spin' },
  lost: { id: 'question', color: 'list.warningForeground' },
};

export const GOING = new Set(['queued', 'running']);

/** "just now", "5 min ago", "3 h ago", "2 days ago" */
export function ago(createdSeconds: number | null, nowMs: number): string {
  if (typeof createdSeconds !== 'number' || !Number.isFinite(createdSeconds)) {
    return '';
  }
  const min = Math.max(0, (nowMs / 1000 - createdSeconds) / 60);
  if (min < 1) {
    return 'just now';
  }
  if (min < 60) {
    return `${Math.floor(min)} min ago`;
  }
  if (min < 48 * 60) {
    return `${Math.floor(min / 60)} h ago`;
  }
  return `${Math.floor(min / 1440)} days ago`;
}

export function runNode(r: RunInfo, nowMs: number): Node {
  const status = String(r.status ?? '?');
  const going = GOING.has(status);
  const cost = typeof r.cost_usd === 'number' ? money(r.cost_usd)
    : typeof r.worst_case_usd === 'number' ? `up to ${money(r.worst_case_usd)}` : '';
  return {
    id: `run:${r.run_id}`,
    label: r.plan ?? r.run_id,
    description: [status + (going && r.step ? `: ${r.step}` : ''), ago(r.created, nowMs), cost].filter(Boolean).join(' · '),
    tooltip: `${r.line}${r.approved ? '\napproved by a person' : ''}`,
    icon: RUN_ICONS[status] ?? { id: 'circle-outline' },
    contextValue: going ? 'run.going' : 'run.done',
    command: { command: COMMANDS.openReport, title: 'Show report', arguments: [r.run_id] },
  };
}

/** Newest first. */
export function runNodes(data: RunsJson, nowMs: number): Node[] {
  if (data.runs.length === 0) {
    return [message('no-runs', 'No runs yet: run a plan, or check this project')];
  }
  return [...data.runs].reverse().map((r) => runNode(r, nowMs));
}

function num(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? String(Number(v.toPrecision(4))) : 'none';
}

/** The loop, read-only: where it stands, its rules, whether the best try is better than luck, and its tries. */
export function loopNodes(data: LoopJson): Node[] {
  const s = data.loop;
  if (!s) {
    return [message('no-loop', 'No loop here', 'info', 'An agent starts one with `testbench loop start PLAN --metric FILE:KEY --budget-usd N`.')];
  }
  const active = s.status === 'running';
  const tries = s.tries.filter((t) => t.n > 0);
  const best = s.best;
  const bestAt = best?.try == null ? 'no passing run yet' : best.try === 0 ? 'the baseline' : `try ${best.try}`;
  const out: Node[] = [
    {
      id: 'loop:head', label: `loop ${s.id}`, description: `${s.status} · plan ${s.plan}`,
      tooltip: `${s.goal} ${s.key} (${s.metric}) on branch ${s.branch}, from ${s.original_branch}`,
      icon: { id: active ? 'sync' : 'debug-stop' },
    },
    { id: 'loop:best', label: `best ${s.key}`, description: `${num(best?.value)} (${bestAt})`, icon: { id: 'star-empty' } },
    {
      id: 'loop:tries', label: 'tries', description: `${tries.length}, ${tries.filter((t) => t.mark === 'kept').length} kept`,
      icon: { id: 'list-ordered' },
    },
    {
      id: 'loop:spent', label: 'spent', description: `${money(s.spent_usd)} of ${money(s.budget_usd)}`,
      tooltip: 'every run of the loop together, against the budget a person approved', icon: { id: 'credit-card' },
    },
    active
      ? { id: 'loop:time', label: 'time left', description: `${(s.hours_left ?? 0).toFixed(1)} h`, icon: { id: 'watch' } }
      : { id: 'loop:time', label: 'stopped', description: (s.ended_by ?? '').replace(/_/g, ' '), icon: { id: 'watch' } },
  ];
  if (s.rules) {
    out.push({ id: 'loop:rules', label: 'stopping rules', description: s.rules, tooltip: s.rules, icon: { id: 'law' } });
  }
  if (s.halt) {
    out.push({ id: 'loop:halt', label: s.halt, tooltip: s.halt, icon: { id: 'warning', color: 'list.warningForeground' } });
  }
  s.luck.forEach((line, i) => {
    out.push({ id: `loop:luck:${i}`, label: 'luck', description: line, tooltip: line, icon: { id: 'graph-line' } });
  });
  if (s.luck.length === 0 && s.luck_why) {
    out.push({ id: 'loop:luck', label: 'luck', description: `not judged yet: ${s.luck_why}`, tooltip: s.luck_why, icon: { id: 'graph-line' } });
  }
  if (active && s.pending !== null && s.pending_run_ended) {
    const text = `Try ${s.pending}'s run has ended; \`testbench loop status\` keeps or reverts it.`;
    out.push(message('loop-judge', text, 'info'));
  }
  out.push({
    id: 'loop:list', label: 'Tries', description: String(s.tries.length), icon: { id: 'history' }, expanded: false,
    children: [...s.tries].reverse().map((t) => ({
      id: `loop:try:${t.n}`,
      label: `${t.n} ${t.mark}`,
      description: [`${s.key} ${num(t.value)}`,
        typeof t.cost_usd === 'number' ? money(t.cost_usd) : `up to ${money(t.worst_case_usd)}`,
        t.description ?? ''].filter(Boolean).join(' · '),
      tooltip: [t.description, t.verdict, t.run ? `run ${t.run}` : ''].filter(Boolean).join('\n'),
      icon: t.mark === 'kept' || t.mark === 'baseline' ? { id: 'pass', color: 'testing.iconPassed' }
        : t.mark === 'running' ? { id: 'sync~spin' } : { id: 'discard' },
      command: t.run ? { command: COMMANDS.openReport, title: 'Show report', arguments: [t.run] } : undefined,
    })),
  });
  return out;
}
