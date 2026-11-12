/** With the real testbench (the Python package of this repository), when it can be started here: the JSON the extension
 * reads is the JSON testbench prints, and the flows work end to end - on plans that run on this machine for free, or
 * that are only estimated (nothing here starts Modal, Colab or anything that costs money). Skipped where Python or the
 * package's dependencies are missing, as in a CI job with only Node. */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';

import { APPROVE_AND_RUN, runPlan } from '../src/approval';
import { Testbench } from '../src/cli';
import { evidencePath, prepareReport } from '../src/report';
import { fakeUi } from './fakes';

const SRC = path.resolve(__dirname, '../../../../src');

function python(): string | undefined {
  const env = { ...process.env, PYTHONPATH: SRC };
  for (const exe of ['python3', 'python']) {
    const deps = spawnSync(exe, ['-c', 'import yaml, nbformat, nbclient, jupyter_client, agent_testbench.cli'], { env });
    const r = spawnSync(exe, ['-m', 'agent_testbench', '--version'], { env, encoding: 'utf8' });
    if (deps.status === 0 && r.status === 0 && r.stdout.startsWith('agent-testbench ')) {
      return exe;
    }
  }
  return undefined;
}

const PY = python();
const CONFIG = (minutes: number) => `project: ext
budget: {max_run_usd: 2.0, ask_above_usd: 0.5, max_day_usd: 10.0}
plans:
  free:
    backend: local
    steps:
      - name: hello
        run: python -c "import pathlib; pathlib.Path('out').mkdir(exist_ok=True); pathlib.Path('out/a.txt').write_text('hi'); print('hello')"
        max_minutes: 2
        artifacts: ["out/*"]
  asks:
    backend: modal
    gpu: T4
    steps: [{name: train, run: python train.py, max_minutes: ${minutes}}]
`;

test('the extension and the real testbench agree', { skip: PY ? false : 'no Python with agent-testbench here' }, async (t) => {
  const project = fs.mkdtempSync(path.join(os.tmpdir(), 'tb-ext-'));
  fs.writeFileSync(path.join(project, 'testbench.yaml'), CONFIG(60));
  const env = { ...process.env, PYTHONPATH: SRC, TESTBENCH_USER_DIR: path.join(project, 'user') };
  const cli = new Testbench({ command: [PY!, '-m', 'agent_testbench'], cwd: project, env });

  await t.test('plans --json', async () => {
    const data = await cli.plans();
    assert.deepEqual(data.plans.map((p) => [p.name, p.launch, p.worst_case_usd === 0]), [['free', 'allowed', true], ['asks', 'needs_approval', false]]);
    assert.equal(data.limits.ask_above_usd.usd, 0.5);
  });

  await t.test('Cancel in the approval dialog runs nothing', async () => {
    const { ui, shown } = fakeUi(() => undefined);
    assert.equal(await runPlan('asks', { cli, ui }), undefined);
    assert.match(shown[0].message, /^Approve up to \$0\.\d\d for plan asks\?$/);
    assert.deepEqual((await cli.runs(10)).runs, []);
  });

  await t.test('a plan that grew while the dialog was open is not run', async () => {
    const { ui, errors } = fakeUi(() => {
      fs.writeFileSync(path.join(project, 'testbench.yaml'), CONFIG(100));
      return APPROVE_AND_RUN;
    });
    assert.equal(await runPlan('asks', { cli, ui }), undefined);
    assert.match(errors[0], /could now cost up to \$1\.\d\d, more than the \$0\.\d\d you approved/);
    assert.deepEqual((await cli.runs(10)).runs, [], 'nothing was launched');
    fs.writeFileSync(path.join(project, 'testbench.yaml'), CONFIG(60));
  });

  await t.test('a free plan runs, and its report opens', async () => {
    const { ui, shown } = fakeUi(() => undefined);
    const launched = await runPlan('free', { cli, ui });
    assert.ok(launched && shown.length === 0);
    const lines: string[] = [];
    const end = await cli.wait(launched.runId, (l) => lines.push(l), new AbortController().signal);
    assert.equal(end.code, 0, end.stdout);
    const [run] = (await cli.runs(5)).runs;
    assert.equal(run.run_id, launched.runId);
    assert.equal(run.status, 'passed');
    assert.ok(run.has_report && fs.realpathSync(run.dir) === fs.realpathSync(path.join(project, '.testbench', 'runs', run.run_id)));
    const file = path.join(project, 'report.html');
    assert.deepEqual(await cli.reportHtml(run.run_id, file), { written: true, text: `wrote ${file} (one self-contained page: open it in a browser)` });
    const page = prepareReport(fs.readFileSync(file, 'utf8'), 'vscode-webview://x', 'n');
    const links = [...page.matchAll(/data-evidence='([^']+)'/g)].map((m) => m[1]);
    assert.ok(links.length > 0, 'evidence links');
    assert.ok(links.every((l) => evidencePath(run.dir, l)), links.join(', '));
    assert.equal((await cli.verify(run.run_id)).code, 0);
  });

  await t.test('spend, loop and check', async () => {
    const spend = await cli.spend();
    assert.equal(spend.project.day_limit_usd, 10);
    assert.equal(typeof spend.machine.today_usd, 'number');
    assert.deepEqual(await cli.loop(), { loop: null });
    const summary = await cli.check();
    assert.equal(summary.exit_code, 0);
    assert.deepEqual(summary.plans.map((p) => [p.plan, p.status]), [['free', 'passed'], ['asks', 'not run']]);
  });

  await t.test('an old testbench without --json says to upgrade', async () => {
    const r = await cli.run(['plans', '--no-such-option']);
    assert.equal(r.code, 2);
    assert.match(cli.failure(['plans', '--json'], { ...r, stderr: r.stderr.replace('--no-such-option', '--json') }).message, /older than this extension/);
  });
});
