/** Today's spend in the status bar: what is committed today (finished runs and sessions at their cost, those still
 * going at their worst case) against the day limits, the project's and this machine's. */
import { money } from './names';
import type { DayTotal, SpendJson } from './types';

export type Level = 'ok' | 'warning' | 'error';

export interface SpendStatus {
  text: string;
  tooltip: string;
  level: Level;
}

function share(d: DayTotal): number {
  return typeof d.day_limit_usd === 'number' && d.day_limit_usd > 0 ? d.today_usd / d.day_limit_usd
    : d.day_limit_usd === 0 && d.today_usd > 0 ? Infinity : 0;
}

function line(what: string, d: DayTotal): string {
  return typeof d.day_limit_usd === 'number'
    ? `${what}: ${money(d.today_usd)} of ${money(d.day_limit_usd)} committed today (${d.where})`
    : `${what}: ${money(d.today_usd)} committed today (no day limit; set max_day_usd in ${d.where})`;
}

/** warnPercent: the share of a day limit at which it turns to the warning color; at the limit it is an error. */
export function spendStatus(s: SpendJson, warnPercent: number): SpendStatus {
  const project = share(s.project);
  const machine = share(s.machine);
  const worst = Math.max(project, machine);
  const level: Level = worst >= 1 ? 'error' : worst >= warnPercent / 100 ? 'warning' : 'ok';
  const shown = machine > project ? s.machine : s.project;
  const limit = typeof shown.day_limit_usd === 'number' ? ` of ${money(shown.day_limit_usd)}` : '';
  return {
    text: `$(credit-card) ${money(shown.today_usd)}${limit} today${shown === s.machine ? ' (this machine)' : ''}`,
    tooltip: [
      line('This project', s.project),
      line('This machine, every project', s.machine),
      level === 'error' ? 'A day limit is reached: testbench refuses new runs that could go over it.'
        : level === 'warning' ? `Over ${warnPercent}% of a day limit.` : '',
      'Click for `testbench spend`.',
    ].filter(Boolean).join('\n'),
    level,
  };
}
