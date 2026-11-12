/** Running a plan from the editor, and the one way the extension approves a cost.
 *
 * A plan that is free runs at once. A plan that costs money but needs no approval runs after a dialog that shows its
 * worst case. A plan whose worst case is above the approval threshold runs only after a person clicked "Approve and
 * run" in a modal dialog that showed that worst case: the click is the approval. Only then does the extension pass
 * --approve, and only here: launchApproved is the one function that writes it, it is reached only from the dialog's
 * handler, and cli.ts refuses --approve from anywhere else (it asks consume() below, which accepts only a consent this
 * module issued after the click, for exactly that launch, once). There is no setting that approves for the person. */
import type { Started, Testbench } from './cli';
import { checkName, money } from './names';
import type { PlanInfo, PlansJson } from './types';

export const APPROVE_AND_RUN = 'Approve and run';
export const RUN = 'Run';

/** What the approval flow needs from the editor. modal: a modal dialog with a message, the detail under it, and buttons
 * besides Cancel; it resolves to the button clicked, or undefined for Cancel, Escape or a closed dialog. */
export interface Ui {
  modal(message: string, detail: string, buttons: string[]): Promise<string | undefined>;
  error(message: string): void;
}

export interface Deps {
  cli: Testbench;
  ui: Ui;
}

export interface Launched extends Started {
  plan: string;
  approved: boolean;
  worstCaseUsd: number;
}

/** free: run at once; confirm: show the worst case first; approve: only after "Approve and run"; refused: never. */
export type Decision = 'free' | 'confirm' | 'approve' | 'refused';

export function decide(p: PlanInfo): Decision {
  if (p.launch === 'refused') {
    return 'refused';
  }
  if (p.launch === 'needs_approval') {
    return 'approve';
  }
  return p.launch === 'allowed' && p.worst_case_usd === 0 ? 'free' : 'confirm';
}

/** A person's approval of one launch: issued only by runPlan, right after "Approve and run" was clicked. */
export interface Consent {
  readonly plan: string;
  readonly worstCaseUsd: number;
}

const issued = new WeakSet<Consent>();
const launches = new WeakMap<Consent, readonly string[]>();

function grant(plan: PlanInfo): Consent {
  const consent: Consent = Object.freeze({ plan: plan.name, worstCaseUsd: plan.worst_case_usd });
  issued.add(consent);
  return consent;
}

/** Whether `args` carry a person's approval: a consent issued here after the click, for the very arguments
 * launchApproved built for it, not used before. cli.ts asks this before it passes anything like --approve on. */
export function consume(consent: Consent | undefined, args: readonly string[]): boolean {
  if (!consent || !issued.has(consent)) {
    return false;
  }
  const built = launches.get(consent);
  issued.delete(consent);
  launches.delete(consent);
  return built !== undefined && built === args;
}

/** The only place the extension writes --approve. Called only by onApproved, which runPlan calls only after the person
 * clicked "Approve and run". */
function launchApproved(cli: Testbench, consent: Consent): Promise<Started> {
  const args = Object.freeze(['run', '--approve', '--detach', '--', checkName(consent.plan, 'plan')]);
  launches.set(consent, args);
  return cli.launch(args, { consent });
}

/** The plan as testbench sees it now (`testbench plans --json`): what the dialog shows, and what a launch checks. */
async function current(cli: Testbench, name: string): Promise<{ plan: PlanInfo | undefined; data: PlansJson }> {
  const data = await cli.plans();
  return { plan: data.plans.find((p) => p.name === name), data };
}

function hardware(p: PlanInfo): string {
  return p.backend + (p.gpu ? ` ${p.gpu}` : '');
}

/** The dialog's detail: where it runs, for how long at most, what that can cost, and testbench's own verdict. */
export function detail(p: PlanInfo, data: PlansJson): string {
  const day = data.limits.max_day_usd;
  const verdict = p.message.replace(/ Ask a person; only they approve it.*$/s, '');
  return [
    `Runs on ${hardware(p)}: ${p.steps.length} step${p.steps.length === 1 ? '' : 's'} (${p.steps.join(', ')}), at most ${Math.round(p.max_minutes)} min in all.`,
    `Worst case: ${money(p.worst_case_usd)}. testbench stops the work at its time limit, on the machine itself, so it cannot cost more.`,
    verdict,
    `Committed today in this project: ${money(data.spent_today_usd)} of ${money(day.usd)} (${day.where}).`,
  ].join('\n\n');
}

function missing(ui: Ui, name: string): undefined {
  ui.error(`There is no plan ${name} in testbench.yaml any more; nothing ran.`);
  return undefined;
}

/** Runs a plan the way its cost allows; undefined when nothing was started (cancelled, refused or gone). */
export async function runPlan(name: string, d: Deps): Promise<Launched | undefined> {
  checkName(name, 'plan');
  const { plan, data } = await current(d.cli, name);
  if (!plan) {
    return missing(d.ui, name);
  }
  switch (decide(plan)) {
    case 'refused':
      d.ui.error(plan.message);
      return undefined;
    case 'free':
      return { ...(await d.cli.start(name)), plan: name, approved: false, worstCaseUsd: plan.worst_case_usd };
    case 'confirm': {
      const choice = await d.ui.modal(`Run plan ${name}? It could cost up to ${money(plan.worst_case_usd)}.`,
        detail(plan, data), [RUN]);
      if (choice !== RUN) {
        return undefined;
      }
      return { ...(await d.cli.start(name)), plan: name, approved: false, worstCaseUsd: plan.worst_case_usd };
    }
    case 'approve': {
      const choice = await d.ui.modal(`Approve up to ${money(plan.worst_case_usd)} for plan ${name}?`,
        `${detail(plan, data)}\n\nApprove only if you want to spend up to ${money(plan.worst_case_usd)} on this run.`,
        [APPROVE_AND_RUN]);
      if (choice !== APPROVE_AND_RUN) {
        return undefined;                 // Cancel, Escape, or the dialog closed: nothing runs, nothing is approved
      }
      return onApproved(d, grant(plan));
    }
  }
}

/** After the click: the plan is read again, and launched with --approve only while its worst case is still at most what
 * the dialog showed (testbench.yaml may have changed meanwhile). */
async function onApproved(d: Deps, consent: Consent): Promise<Launched | undefined> {
  const { plan } = await current(d.cli, consent.plan);
  if (!plan) {
    return missing(d.ui, consent.plan);
  }
  if (plan.launch === 'refused') {
    d.ui.error(plan.message);
    return undefined;
  }
  if (!(plan.worst_case_usd <= consent.worstCaseUsd)) {
    d.ui.error(`Not run: plan ${consent.plan} could now cost up to ${money(plan.worst_case_usd)}, more than the ${money(consent.worstCaseUsd)} you approved (testbench.yaml changed). Run it again to see the new estimate.`);
    return undefined;
  }
  return { ...(await launchApproved(d.cli, consent)), plan: consent.plan, approved: true, worstCaseUsd: consent.worstCaseUsd };
}
