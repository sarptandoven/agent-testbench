/** The approval flow: a plan that needs approval runs only after a person clicked "Approve and run" in a modal dialog
 * that showed its worst case, and --approve is written in one place, reached only from there. */
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { APPROVE_AND_RUN, consume, decide, RUN, runPlan } from '../src/approval';
import { CliError, Testbench, type RunOptions, type Started } from '../src/cli';
import { fakeCli, fakeUi, ok, plan, plansJson, started } from './fakes';
import type { PlansJson } from '../src/types';

const SRC = path.resolve(__dirname, '../../src');

/** A testbench whose `plans --json` answers with plans() (read again on every call) and whose launches start runs. */
function bench(plans: () => PlansJson) {
  return fakeCli((args) => {
    if (args[0] === 'plans') {
      return ok(JSON.stringify(plans()));
    }
    if (args[0] === 'run') {
      return started(`20261112-120000-${args[args.length - 1]}`);
    }
    return ok('', 2, `unexpected ${args.join(' ')}`);
  });
}

const approving = (args: readonly string[]) => args.some((a) => a.includes('approve'));

test('what a plan needs before it runs', () => {
  assert.equal(decide(plan('free', 'allowed', 0)), 'free');
  assert.equal(decide(plan('cheap', 'allowed', 0.3)), 'confirm');
  assert.equal(decide(plan('asks', 'needs_approval', 0.87)), 'approve');
  assert.equal(decide(plan('huge', 'refused', 41)), 'refused');
});

test('a free plan runs at once, without --approve', async () => {
  const { cli, calls } = bench(() => plansJson(plan('free', 'allowed', 0)));
  const { ui, shown } = fakeUi(() => APPROVE_AND_RUN);
  const got = await runPlan('free', { cli, ui });
  assert.equal(got?.runId, '20261112-120000-free');
  assert.equal(got?.approved, false);
  assert.equal(shown.length, 0, 'no dialog for a plan that costs nothing');
  assert.deepEqual(calls.map((c) => c.args), [['plans', '--json'], ['run', '--detach', '--', 'free']]);
});

test('a plan that costs money but needs no approval shows its worst case first, and never approves', async () => {
  const p = plan('cheap', 'allowed', 0.3);
  for (const click of [undefined, 'Cancel', APPROVE_AND_RUN]) {
    const { cli, calls } = bench(() => plansJson(p));
    const { ui, shown } = fakeUi(() => click);
    assert.equal(await runPlan('cheap', { cli, ui }), undefined, `clicked ${click}`);
    assert.equal(shown[0].message, 'Run plan cheap? It could cost up to $0.30.');
    assert.deepEqual(shown[0].buttons, [RUN]);
    assert.equal(calls.length, 1, 'only `plans --json`: nothing ran');
  }
  const { cli, calls } = bench(() => plansJson(p));
  const { ui } = fakeUi(() => RUN);
  assert.equal((await runPlan('cheap', { cli, ui }))?.approved, false);
  assert.deepEqual(calls.at(-1)?.args, ['run', '--detach', '--', 'cheap']);
  assert.ok(!calls.some((c) => approving(c.args)));
});

test('a plan that needs approval shows its worst case in a modal; Cancel runs nothing', async () => {
  for (const click of [undefined, 'Cancel', RUN, 'approve and run', `${APPROVE_AND_RUN} `]) {
    const { cli, calls } = bench(() => plansJson(plan('asks', 'needs_approval', 0.87)));
    const { ui, shown } = fakeUi(() => click);
    assert.equal(await runPlan('asks', { cli, ui }), undefined, `clicked ${JSON.stringify(click)}`);
    assert.equal(shown.length, 1);
    assert.equal(shown[0].message, 'Approve up to $0.87 for plan asks?');
    assert.deepEqual(shown[0].buttons, [APPROVE_AND_RUN]);
    assert.match(shown[0].detail, /Runs on modal T4: 2 steps \(train, eval\), at most 60 min in all\./);
    assert.match(shown[0].detail, /Worst case: \$0\.87\. testbench stops the work at its time limit/);
    assert.match(shown[0].detail, /Needs approval: plan asks could cost up to \$0\.87 \(above budget\.ask_above_usd \$0\.50\)\./);
    assert.doesNotMatch(shown[0].detail, /Ask a person/, 'the person is the one asked');
    assert.match(shown[0].detail, /Committed today in this project: \$0\.12 of \$10\.00 \(budget\.max_day_usd \$10\.00\)\./);
    assert.deepEqual(calls.map((c) => c.args), [['plans', '--json']], 'nothing launched, nothing approved');
  }
});

test('"Approve and run" launches it once, with --approve, after reading the plan again', async () => {
  const { cli, calls } = bench(() => plansJson(plan('asks', 'needs_approval', 0.87)));
  const { ui, errors } = fakeUi(() => APPROVE_AND_RUN);
  const got = await runPlan('asks', { cli, ui });
  assert.deepEqual(errors, []);
  assert.deepEqual(got, { runId: '20261112-120000-asks', text: 'run 20261112-120000-asks started (Within budget: up to $0.87.)',
    plan: 'asks', approved: true, worstCaseUsd: 0.87 });
  assert.deepEqual(calls.map((c) => c.args), [['plans', '--json'], ['plans', '--json'], ['run', '--approve', '--detach', '--', 'asks']]);
});

test('after the click, a plan that now costs more than the dialog showed is not run', async () => {
  let worst = 0.87;
  const { cli, calls } = bench(() => plansJson(plan('asks', 'needs_approval', worst)));
  const { ui, errors } = fakeUi(() => {
    worst = 1.9;                                      // testbench.yaml changed while the dialog was open
    return APPROVE_AND_RUN;
  });
  assert.equal(await runPlan('asks', { cli, ui }), undefined);
  assert.match(errors[0], /could now cost up to \$1\.90, more than the \$0\.87 you approved/);
  assert.ok(!calls.some((c) => approving(c.args)));
});

test('after the click, a plan refused meanwhile (the day budget went) is not run', async () => {
  let launch: 'needs_approval' | 'refused' = 'needs_approval';
  const { cli, calls } = bench(() => plansJson(plan('asks', launch, 0.87)));
  const { ui, errors } = fakeUi(() => {
    launch = 'refused';
    return APPROVE_AND_RUN;
  });
  assert.equal(await runPlan('asks', { cli, ui }), undefined);
  assert.match(errors[0], /^Refused: plan asks/);
  assert.ok(!calls.some((c) => approving(c.args)));
});

test('a refused plan is never offered to approve, and a plan that is gone is not run', async () => {
  const { cli, calls } = bench(() => plansJson(plan('huge', 'refused', 41)));
  const { ui, shown, errors } = fakeUi(() => APPROVE_AND_RUN);
  assert.equal(await runPlan('huge', { cli, ui }), undefined);
  assert.equal(shown.length, 0);
  assert.match(errors[0], /^Refused: plan huge could cost up to \$41\.00/);
  assert.equal(await runPlan('nope', { cli, ui }), undefined);
  assert.match(errors[1], /no plan nope in testbench\.yaml/);
  assert.ok(!calls.some((c) => c.args[0] === 'run'));
});

test('a consent is used once, for the arguments it was given for, and cannot be made up', async () => {
  class Spy extends Testbench {
    seen: { args: readonly string[]; opts: RunOptions }[] = [];
    override launch(args: readonly string[], opts: RunOptions = {}): Promise<Started> {
      this.seen.push({ args, opts });
      return super.launch(args, opts);
    }
  }
  const replies = (args: readonly string[]) => (args[0] === 'plans' ? ok(JSON.stringify(plansJson(plan('asks', 'needs_approval', 0.87))))
    : started('20261112-120000-asks'));
  const exec = async (_f: string, args: readonly string[]) => replies(args);
  const cli = new Spy({ command: ['testbench'], cwd: '/p', exec, env: { PATH: '/usr/bin' }, platform: 'linux', isFile: () => true });
  const { ui } = fakeUi(() => APPROVE_AND_RUN);
  assert.equal((await runPlan('asks', { cli, ui }))?.approved, true);
  const { args, opts } = cli.seen[0];
  assert.ok(opts.consent && Object.isFrozen(opts.consent) && Object.isFrozen(args));
  await assert.rejects(cli.run(args, opts), (e: unknown) => e instanceof CliError && e.kind === 'refused', 'used once');
  assert.equal(consume(opts.consent, args), false);
  assert.equal(consume({ plan: 'asks', worstCaseUsd: 0.87 }, args), false, 'a consent made up elsewhere is not one');
  assert.equal(consume(undefined, args), false);
});

test('--approve is written in one function, reached only from the "Approve and run" handler', () => {
  const sources = fs.readdirSync(SRC).filter((f) => f.endsWith('.ts')).map((f) => [f, fs.readFileSync(path.join(SRC, f), 'utf8')]);
  const literal = /(['"`])--approve\1/g;
  const found = sources.flatMap(([f, text]) => [...text.matchAll(literal)].map(() => f));
  assert.deepEqual(found, ['approval.ts'], 'one --approve in the extension, in approval.ts');
  const approval = sources.find(([f]) => f === 'approval.ts')![1];
  const body = (name: string) => {
    const start = approval.indexOf(`function ${name}(`);
    assert.ok(start >= 0, name);
    return approval.slice(start, approval.indexOf('\n}\n', start));
  };
  assert.match(body('launchApproved'), /'--approve'/, 'launchApproved holds it');
  const calls = (name: string) => [...approval.matchAll(new RegExp(`\\b${name}\\(`, 'g'))].length;
  assert.equal(calls('launchApproved'), 2, 'defined once, called once');
  assert.match(body('onApproved'), /launchApproved\(d\.cli, consent\)/, 'called from onApproved');
  assert.equal(calls('onApproved'), 2, 'defined once, called once');
  assert.equal(calls('grant'), 2, 'a consent is made in one place');
  const handler = body('runPlan');
  const click = handler.indexOf('if (choice !== APPROVE_AND_RUN)');
  assert.ok(click > 0 && click < handler.indexOf('onApproved(d, grant(plan))'), 'only after the click is checked');
  assert.doesNotMatch(approval, /^export (async )?function (launchApproved|onApproved|grant)/m, 'none of them is exported');
  for (const [f, text] of sources) {
    if (f !== 'approval.ts' && f !== 'cli.ts') {
      assert.doesNotMatch(text, /\bconsume\(|\bconsent\b/, `${f} has nothing to do with consent`);
    }
  }
});

test('no setting approves for the person', () => {
  const pkg = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../package.json'), 'utf8'));
  const props = Object.entries(pkg.contributes.configuration.properties as Record<string, { markdownDescription?: string }>);
  assert.ok(props.length > 0);
  for (const [key, spec] of props) {
    assert.doesNotMatch(key, /approv|confirm|auto/i, key);
    assert.doesNotMatch(spec.markdownDescription ?? '', /approv/i, key);
  }
});
