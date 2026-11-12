/** Stand-ins for the CLI and the editor's dialogs, so the flows can be tested without VS Code or testbench. */
import { Testbench, type Exec, type ExecOptions, type ExecResult } from '../src/cli';
import type { Ui } from '../src/approval';
import type { PlanInfo, PlansJson } from '../src/types';

export interface Call {
  file: string;
  args: readonly string[];
  opts: ExecOptions;
}

/** An Exec that records every start and answers from `reply` (by the testbench arguments, after the command's own). */
export function fakeExec(reply: (args: readonly string[]) => ExecResult | Promise<ExecResult>): { exec: Exec; calls: Call[] } {
  const calls: Call[] = [];
  const exec: Exec = async (file, args, opts) => {
    calls.push({ file, args, opts });
    return reply(args);
  };
  return { exec, calls };
}

export const ok = (stdout: string, code = 0, stderr = ''): ExecResult => ({ code, stdout, stderr });

/** A Testbench on a fake exec; `testbench` is found at /usr/bin/testbench. */
export function fakeCli(reply: (args: readonly string[]) => ExecResult | Promise<ExecResult>, command = ['testbench']) {
  const { exec, calls } = fakeExec(reply);
  const cli = new Testbench({
    command, cwd: '/project', exec, env: { PATH: '/usr/bin' }, platform: 'linux', isFile: (p) => p === '/usr/bin/testbench',
  });
  return { cli, calls };
}

export function plan(name: string, launch: PlanInfo['launch'], worst: number, extra: Partial<PlanInfo> = {}): PlanInfo {
  const messages = {
    allowed: `Within budget: up to $${worst.toFixed(2)} (today so far $0.00 of $10.00).`,
    needs_approval: `Needs approval: plan ${name} could cost up to $${worst.toFixed(2)} (above budget.ask_above_usd $0.50). Ask a person; only they approve it, with \`testbench run ${name} --approve\`.`,
    refused: `Refused: plan ${name} could cost up to $${worst.toFixed(2)}, over budget.max_run_usd $2.00.`,
  };
  return {
    name, backend: worst ? 'modal' : 'local', gpu: worst ? 'T4' : null, description: null, steps: ['train', 'eval'],
    max_minutes: 60, worst_case_usd: worst, launch, message: messages[launch], ...extra,
  };
}

export function plansJson(...plans: PlanInfo[]): PlansJson {
  return {
    project: 'demo', spent_today_usd: 0.12, plans,
    limits: {
      max_run_usd: { usd: 2, where: 'budget.max_run_usd $2.00' },
      ask_above_usd: { usd: 0.5, where: 'budget.ask_above_usd $0.50' },
      max_day_usd: { usd: 10, where: 'budget.max_day_usd $10.00' },
    },
  };
}

/** A dialog stand-in: it records what was shown and clicks `click` (undefined: Cancel). */
export function fakeUi(click: (message: string, detail: string, buttons: string[]) => string | undefined | Promise<string | undefined>) {
  const shown: { message: string; detail: string; buttons: string[] }[] = [];
  const errors: string[] = [];
  const ui: Ui = {
    modal: async (message, detail, buttons) => {
      shown.push({ message, detail, buttons });
      return click(message, detail, buttons);
    },
    error: (text) => {
      errors.push(text);
    },
  };
  return { ui, shown, errors };
}

/** The testbench a launch answers with. */
export function started(runId: string): ExecResult {
  return ok(`[testbench] run ${runId} started (Within budget: up to $0.87.)\nFollow it with \`testbench wait ${runId}\`.\n`, 3);
}
