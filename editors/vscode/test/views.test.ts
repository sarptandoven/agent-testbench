/** What the views, the status bar, the report page and the schema hand-off are made of. */
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';

import { COMMANDS } from '../src/commands';
import { findProjectRoot } from '../src/project';
import { csp, evidencePath, placeholder, prepareReport, runFolder } from '../src/report';
import { contributeSchema, isTestbenchYaml, SCHEMA_URI, SCHEME, type YamlApi } from '../src/schema';
import { spendStatus } from '../src/statusbar';
import { ago, loopNodes, planNodes, runNodes } from '../src/tree';
import type { LoopInfo, RunInfo, SpendJson } from '../src/types';
import { plan, plansJson } from './fakes';

const NOW = Date.UTC(2026, 10, 12, 12, 0, 0);

function run(id: string, status: string, extra: Partial<RunInfo> = {}): RunInfo {
  return {
    run_id: id, plan: id.split('-').pop() ?? null, status, backend: 'local', gpu: null, created: NOW / 1000 - 300,
    cost_usd: status === 'running' ? null : 0, worst_case_usd: 0, approved: false, step: null, dir: `/p/.testbench/runs/${id}`,
    has_report: status !== 'running', line: `${id}  ${status}  backend=local`, ...extra,
  };
}

test('plans: hardware, worst case, and what each needs before it runs', () => {
  const nodes = planNodes(plansJson(plan('smoke', 'allowed', 0, { description: 'a quick check' }), plan('gpu', 'needs_approval', 0.87),
    plan('huge', 'refused', 41.42)));
  assert.deepEqual(nodes.map((n) => [n.label, n.description, n.contextValue, n.icon?.id]), [
    ['smoke', 'local · free', 'plan.allowed', 'play-circle'],
    ['gpu', 'modal T4 · up to $0.87 · needs your approval', 'plan.needs_approval', 'shield'],
    ['huge', 'modal T4 · up to $41.42 · refused', 'plan.refused', 'circle-slash'],
  ]);
  assert.match(nodes[0].tooltip ?? '', /^a quick check\nlocal, 2 steps: train, eval\nat most 60 min, worst case \$0\.00\nWithin budget/);
  assert.equal(nodes[1].command, undefined, 'clicking a plan does nothing; its play button asks first');
  assert.deepEqual(planNodes(plansJson()).map((n) => n.label), ['testbench.yaml has no plans yet']);
});

test('runs: newest first, a status icon each, and a click opens the report', () => {
  const nodes = runNodes({ runs: [run('20261112-0900-a', 'passed'), run('20261112-0901-b', 'failed', { cost_usd: 0.4321 }),
    run('20261112-0902-c', 'running', { step: 'train', worst_case_usd: 0.87 }), run('20261112-0903-d', 'lost'),
    run('20261112-0904-e', 'error'), run('20261112-0905-f', 'cancelled'), run('20261112-0906-g', 'queued')] }, NOW);
  assert.deepEqual(nodes.map((n) => n.icon?.id), ['clock', 'circle-slash', 'warning', 'question', 'sync~spin', 'error', 'pass']);
  assert.deepEqual(nodes.map((n) => n.contextValue), ['run.going', 'run.done', 'run.done', 'run.done', 'run.going', 'run.done', 'run.done']);
  const going = nodes[4];
  assert.equal(going.label, 'c');
  assert.equal(going.description, 'running: train · 5 min ago · up to $0.87');
  assert.equal(nodes[5].description, 'failed · 5 min ago · $0.43');
  assert.deepEqual(nodes[6].command, { command: COMMANDS.openReport, title: 'Show report', arguments: ['20261112-0900-a'] });
  assert.deepEqual(runNodes({ runs: [] }, NOW).map((n) => n.id), ['message:no-runs']);
  assert.deepEqual([NOW / 1000, NOW / 1000 - 90, NOW / 1000 - 7200, NOW / 1000 - 5 * 86400, null].map((t) => ago(t, NOW)),
    ['just now', '1 min ago', '2 h ago', '5 days ago', '']);
});

const LOOP: LoopInfo = {
  id: '20261112-0800', status: 'running', plan: 'backtest', goal: 'max', metric: 'out/metrics.json:sharpe', key: 'sharpe',
  best: { value: 1.2345678, commit: 'abc123456789', try: 2 }, budget_usd: 2, spent_usd: 0.3, hours_left: 6.25,
  branch: 'testbench-loop/20261112-0800', original_branch: 'main', ended_by: null, rules: 'target sharpe >= 1.5; patience 3 (1 in a row not kept now)',
  halt: null, luck: ['best of 4 tries: deflated Sharpe 0.62 (probability the true Sharpe is above 0); not significant at 0.95 - the improvement may be luck',
    'PBO 0.50 over 4 tries: the best in-sample try falls below the median try out of sample as often as not'],
  luck_why: null, pending: 3, pending_run_ended: true,
  tries: [
    { n: 0, mark: 'baseline', value: 0.8, cost_usd: 0, worst_case_usd: 0.1, run: 'r0', description: 'baseline', verdict: 'BASELINE: sharpe 0.8' },
    { n: 1, mark: 'reverted', value: 0.7, cost_usd: 0.1, worst_case_usd: 0.1, run: 'r1', description: 'shorter window', verdict: 'REVERTED try 1' },
    { n: 2, mark: 'kept', value: 1.2345678, cost_usd: 0.1, worst_case_usd: 0.1, run: 'r2', description: 'add a stop', verdict: 'KEPT try 2' },
    { n: 3, mark: 'running', value: null, cost_usd: null, worst_case_usd: 0.1, run: 'r3', description: 'wider stop', verdict: null },
  ],
};

test('the loop: where it stands and the luck line, read-only', () => {
  const nodes = loopNodes({ loop: LOOP });
  const by = Object.fromEntries(nodes.map((n) => [n.id, n]));
  assert.equal(by['loop:head'].description, 'running · plan backtest');
  assert.equal(by['loop:best'].description, '1.235 (try 2)');
  assert.equal(by['loop:tries'].description, '3, 1 kept');
  assert.equal(by['loop:spent'].description, '$0.30 of $2.00');
  assert.equal(by['loop:time'].description, '6.3 h');
  assert.equal(by['loop:rules'].description, LOOP.rules);
  assert.equal(by['loop:luck:0'].description, LOOP.luck[0]);
  assert.equal(by['loop:luck:1'].description, LOOP.luck[1]);
  assert.match(by['message:loop-judge'].label, /^Try 3's run has ended; `testbench loop status` keeps or reverts it\.$/);
  const tries = by['loop:list'].children ?? [];
  assert.deepEqual(tries.map((t) => t.label), ['3 running', '2 kept', '1 reverted', '0 baseline']);
  assert.equal(tries[1].description, 'sharpe 1.235 · $0.10 · add a stop');
  assert.equal(tries[0].description, 'sharpe none · up to $0.10 · wider stop');
  const commands = [...nodes, ...tries].flatMap((n) => (n.command ? [n.command.command] : []));
  assert.ok(commands.every((c) => c === COMMANDS.openReport), 'the only thing to do from the loop is to read a run');
  assert.equal(nodes.filter((n) => n.contextValue).length, 0, 'no menus: nothing to act on');
  const stopped = loopNodes({ loop: { ...LOOP, status: 'stopped', ended_by: 'target_reached', hours_left: null, luck: [], luck_why: 'no try has returns yet', pending: null } });
  const s = Object.fromEntries(stopped.map((n) => [n.id, n]));
  assert.equal(s['loop:time'].label, 'stopped');
  assert.equal(s['loop:time'].description, 'target reached');
  assert.equal(s['loop:luck'].description, 'not judged yet: no try has returns yet');
  assert.equal(s['message:loop-judge'], undefined);
  assert.deepEqual(loopNodes({ loop: null }).map((n) => n.label), ['No loop here']);
});

function spend(project: number, projectLimit: number | null, machine: number, machineLimit: number | null): SpendJson {
  return {
    project: { today_usd: project, day_limit_usd: projectLimit, where: 'budget.max_day_usd in testbench.yaml' },
    machine: { today_usd: machine, day_limit_usd: machineLimit, where: '/home/me/.testbench/limits.yaml' }, total_usd: 3, finished: 4,
  };
}

test('the status bar: today against the day limits, a warning near them', () => {
  const okay = spendStatus(spend(0.42, 10, 0.42, null), 80);
  assert.equal(okay.text, '$(credit-card) $0.42 of $10.00 today');
  assert.equal(okay.level, 'ok');
  assert.match(okay.tooltip, /This project: \$0\.42 of \$10\.00 committed today \(budget\.max_day_usd in testbench\.yaml\)/);
  assert.match(okay.tooltip, /This machine, every project: \$0\.42 committed today \(no day limit; set max_day_usd in/);
  assert.equal(spendStatus(spend(8, 10, 8, 20), 80).level, 'warning');
  assert.equal(spendStatus(spend(7.9, 10, 7.9, 20), 80).level, 'ok');
  const machine = spendStatus(spend(1, 10, 19, 20), 80);
  assert.equal(machine.text, '$(credit-card) $19.00 of $20.00 today (this machine)');
  assert.equal(machine.level, 'warning');
  const over = spendStatus(spend(10, 10, 10, null), 80);
  assert.equal(over.level, 'error');
  assert.match(over.tooltip, /A day limit is reached: testbench refuses new runs/);
  assert.equal(spendStatus(spend(0.01, 0, 0, null), 80).level, 'error', 'a limit of $0 with anything spent');
});

const REPORT = `<!doctype html><html lang=en><head><meta charset=utf-8><title>PASS smoke</title><style>body{color:red}</style>
<meta http-equiv="refresh" content="0;url=https://example.com"><base href="https://example.com/"></head><body><main>
<script>alert(1)</script><img src='data:image/png;base64,AAAA'>
<a href='steps/01-train/log.txt'>log</a> <a href='https://github.com/x'>gh</a> <a href='javascript:alert(1)'>js</a>
<a href="/etc/passwd">abs</a> <a href='command:workbench.action.terminal.new'>cmd</a></main></body></html>`;

test('the report page: a strict policy, the page\'s own style by nonce, no script but the extension\'s', () => {
  const page = prepareReport(REPORT, 'vscode-webview://src', 'N0NCE');
  assert.match(page, /<head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src vscode-webview:\/\/src data:; style-src 'nonce-N0NCE'; script-src 'nonce-N0NCE'; base-uri 'none'; form-action 'none'">/);
  assert.match(page, /<style nonce="N0NCE">body\{color:red\}<\/style>/);
  assert.doesNotMatch(page, /alert\(1\)|http-equiv="refresh"|<base/);
  assert.match(page, /<a href="#" data-evidence='steps\/01-train\/log\.txt'>log<\/a>/);
  assert.match(page, /<a href='https:\/\/github\.com\/x'>gh<\/a>/);
  assert.match(page, /<a >js<\/a>\n<a >abs<\/a> <a >cmd<\/a>/, 'other schemes and absolute paths lose their link');
  const scripts = [...page.matchAll(/<script([^>]*)>/g)].map((m) => m[1]);
  assert.deepEqual(scripts, [' nonce="N0NCE"'], 'one script: the extension\'s, with the nonce');
  assert.match(page, /postMessage\(\{ type: 'open', path: a\.getAttribute\('data-evidence'\) \}\)/);
  assert.match(placeholder('src', 'n', 'Run <x>', 'a & b'), /<h2>Run &#60;x&#62;<\/h2><pre>a &#38; b<\/pre>/);
  assert.match(csp('src', 'n'), /^default-src 'none';/);
});

test('an evidence file opens only from inside its run\'s folder', () => {
  const top = fs.mkdtempSync(path.join(os.tmpdir(), 'tb-run-'));
  const runDir = path.join(top, '.testbench', 'runs', '20261112-0900-a');
  fs.mkdirSync(path.join(runDir, 'steps', '01-a'), { recursive: true });
  fs.writeFileSync(path.join(runDir, 'steps', '01-a', 'log.txt'), 'ok');
  fs.writeFileSync(path.join(top, 'secret.txt'), 'no');
  const real = (p: string) => fs.realpathSync(p);
  assert.equal(evidencePath(runDir, 'steps/01-a/log.txt'), real(path.join(runDir, 'steps', '01-a', 'log.txt')));
  for (const bad of ['../../../secret.txt', path.join(top, 'secret.txt'), 'steps/01-a', 'steps/nope.txt', '', 'file:///etc/passwd', 3, undefined]) {
    assert.equal(evidencePath(runDir, bad), undefined, String(bad));
  }
  try {
    fs.symlinkSync(path.join(top, 'secret.txt'), path.join(runDir, 'steps', '01-a', 'link.txt'));
    assert.equal(evidencePath(runDir, 'steps/01-a/link.txt'), undefined, 'a link out of the folder');
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== 'EPERM') {
      throw err;                                         // Windows without the right to make links: skip that part
    }
  }
  assert.equal(runFolder(top, '20261112-0900-a', undefined), real(runDir));
  assert.equal(runFolder(top, '20261112-0900-a', runDir), real(runDir));
  assert.equal(runFolder(top, '20261112-0900-a', top), undefined, 'a folder that is not the run\'s');
  assert.equal(runFolder(top, 'nope', undefined), undefined);
});

test('the schema goes to the YAML extension for testbench.yaml only, once it is there', () => {
  let content: string | undefined;
  const got: Parameters<YamlApi['registerContributor']>[] = [];
  const api: YamlApi = { registerContributor: (...args) => (got.push(args), true) };
  assert.equal(contributeSchema(api, () => content), true);
  const [scheme, requestSchema, requestContent] = got[0];
  assert.equal(scheme, SCHEME);
  assert.equal(requestSchema('file:///p/testbench.yaml'), undefined, 'not before `testbench schema` answered');
  content = '{"type": "object"}';
  assert.equal(requestSchema('file:///p/testbench.yaml'), SCHEMA_URI);
  assert.equal(requestSchema('file:///p/sub/testbench.yaml'), SCHEMA_URI);
  assert.equal(requestSchema('file:///p/mytestbench.yaml'), undefined);
  assert.equal(requestSchema('file:///p/other.yaml'), undefined);
  assert.equal(requestContent(SCHEMA_URI), content);
  assert.equal(requestContent('agent-testbench://other'), undefined);
  assert.ok(isTestbenchYaml('c:\\p\\testbench.yaml') && isTestbenchYaml('file:///p/test%62ench.yaml'));
});

test('the project is the folder with testbench.yaml, else with .testbench', () => {
  const has = (...paths: string[]) => (p: string) => paths.includes(p);
  assert.equal(findProjectRoot(['/a', '/b'], has(path.join('/b', 'testbench.yaml'))), '/b');
  assert.equal(findProjectRoot(['/a', '/b'], has(path.join('/a', '.testbench'), path.join('/b', 'testbench.yaml'))), '/b');
  assert.equal(findProjectRoot(['/a', '/b'], has(path.join('/a', '.testbench'))), '/a');
  assert.equal(findProjectRoot(['/a'], has()), undefined);
});
