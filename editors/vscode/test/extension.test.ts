/** extension.ts, the glue, run in Node against a stand-in for the `vscode` module and a stand-in testbench program
 * (a Node script that answers like testbench and logs every command line it gets). This checks what the person sees
 * and clicks: the commands it registers, the views, the status bar, and the approval dialog - that it is modal, shows
 * the worst case, and that only "Approve and run" leads to --approve. */
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import Module from 'node:module';
import * as os from 'node:os';
import * as path from 'node:path';
import { after, before, test } from 'node:test';

import { COMMANDS, VIEWS } from '../src/commands';

const FAKE_TESTBENCH = `
const fs = require('fs');
const args = process.argv.slice(2);
fs.appendFileSync(process.env.FAKE_TB_LOG, JSON.stringify(args) + '\\n');
const plan = (name, launch, worst) => ({ name, backend: worst ? 'modal' : 'local', gpu: worst ? 'T4' : null, description: null,
  steps: ['train'], max_minutes: 60, worst_case_usd: worst, launch,
  message: launch === 'allowed' ? 'Within budget: up to $0.00 (today so far $0.00 of $10.00).'
    : 'Needs approval: plan asks could cost up to $0.87 (above budget.ask_above_usd $0.50). Ask a person; only they approve it, with \`testbench run asks --approve\`.' });
const out = (o, code = 0) => { process.stdout.write(typeof o === 'string' ? o : JSON.stringify(o)); process.exit(code); };
const cmd = args.filter((a) => !a.startsWith('-')).slice(0, 2).join(' ');
if (args[0] === '--version') out('agent-testbench 9.9.9\\n');
if (cmd === 'schema') out('{"type": "object"}');
if (cmd === 'plans') out({ project: 'p', spent_today_usd: 0, plans: [plan('free', 'allowed', 0), plan('asks', 'needs_approval', 0.87)],
  limits: { max_run_usd: { usd: 2, where: 'budget.max_run_usd $2.00' }, ask_above_usd: { usd: 0.5, where: 'budget.ask_above_usd $0.50' },
    max_day_usd: { usd: 10, where: 'budget.max_day_usd $10.00' } } });
if (cmd === 'runs') out({ runs: [] });
if (cmd === 'spend') out({ project: { today_usd: 8.5, day_limit_usd: 10, where: 'budget.max_day_usd in testbench.yaml' },
  machine: { today_usd: 8.5, day_limit_usd: null, where: '/u/limits.yaml' }, total_usd: 9, finished: 3, latest: [] });
if (cmd === 'loop status') out({ loop: null });
if (args[0] === 'run') out('[testbench] run 20261112-120000-' + args[args.length - 1] + ' started (Within budget.)\\n', 3);
if (args[0] === 'wait') { console.log('[testbench] step 1 started: train'); out('PASS\\n', 0); }
if (args[0] === 'report') {
  const file = args[args.indexOf('-o') + 1];
  fs.writeFileSync(file, "<!doctype html><html><head><style>main{}</style></head><body><main><script>alert(1)</script>"
    + "<a href='steps/01-train/log.txt'>log</a></main></body></html>");
  out('wrote ' + file + ' (one self-contained page: open it in a browser)\\n');
}
out('testbench: not in this stand-in: ' + args.join(' ') + '\\n', 2);
`;

interface Shown {
  kind: string;
  message: string;
  options?: { modal?: boolean; detail?: string };
  items: { title: string; isCloseAffordance?: boolean }[];
}

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tb-ext-glue-'));
const project = path.join(dir, 'project');
const log = path.join(dir, 'calls.jsonl');
const script = path.join(dir, 'fake-testbench.js');
const registered = new Map<string, (...args: unknown[]) => unknown>();
const providers = new Map<string, { getChildren(n?: unknown): unknown; getTreeItem(n: unknown): { label: string; description?: string } }>();
const shown: Shown[] = [];
const terminals: { name: string; shellPath: string; shellArgs: string[]; cwd: string }[] = [];
const status = { text: '', tooltip: '', backgroundColor: undefined as { id: string } | undefined, show() {}, dispose() {}, command: '', name: '' };
let answer: (s: Shown) => string | undefined = () => undefined;
let subscriptions: { dispose(): void }[] = [];

interface Panel {
  title: string;
  options: { enableScripts?: boolean; enableCommandUris?: boolean; localResourceRoots?: { fsPath: string }[] };
  onMessage: ((m: unknown) => void) | undefined;
  webview: { html: string; cspSource: string; onDidReceiveMessage(fn: (m: unknown) => void): unknown };
  onDidDispose(): unknown;
  reveal(): void;
  dispose(): void;
}
const panels: Panel[] = [];
const executed: unknown[][] = [];

class EventEmitter {
  private readonly listeners: ((x: unknown) => void)[] = [];
  event = (l: (x: unknown) => void) => {
    this.listeners.push(l);
    return { dispose() {} };
  };
  fire(x: unknown) {
    this.listeners.forEach((l) => l(x));
  }
  dispose() {}
}

const nothing = { dispose() {} };
const message = (kind: string) => async (msg: string, ...rest: unknown[]) => {
  const options = rest[0] && typeof rest[0] === 'object' && !('title' in (rest[0] as object)) ? rest.shift() as Shown['options'] : undefined;
  const items = rest.map((r) => (typeof r === 'string' ? { title: r } : r as Shown['items'][number]));
  const s: Shown = { kind, message: msg, options, items };
  shown.push(s);
  const title = answer(s);
  const item = items.find((i) => i.title === title);
  return item && typeof rest[items.indexOf(item)] === 'string' ? item.title : item;
};

const vscode = {
  EventEmitter,
  TreeItem: class { constructor(public label: string, public collapsibleState: number) {} },
  ThemeIcon: class { constructor(public id: string, public color?: unknown) {} },
  ThemeColor: class { constructor(public id: string) {} },
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  StatusBarAlignment: { Left: 1, Right: 2 },
  ProgressLocation: { Notification: 15 },
  ViewColumn: { Active: -1 },
  Uri: { file: (p: string) => ({ fsPath: p, scheme: 'file', path: p }) },
  RelativePattern: class { constructor(public base: string, public pattern: string) {} },
  window: {
    createOutputChannel: () => ({ appendLine() {}, show() {}, dispose() {} }),
    createStatusBarItem: () => status,
    registerTreeDataProvider: (id: string, p: never) => (providers.set(id, p), nothing),
    showWarningMessage: message('warning'),
    showErrorMessage: message('error'),
    showInformationMessage: message('info'),
    showQuickPick: async () => undefined,
    withProgress: async (_o: unknown, task: (p: unknown, t: unknown) => Promise<unknown>) =>
      task({ report() {} }, { onCancellationRequested: () => nothing }),
    createWebviewPanel: (_type: string, title: string, _column: number, options: Panel['options']) => {
      const panel: Panel = {
        title, options, onMessage: undefined,
        webview: { html: '', cspSource: 'vscode-webview://fake', onDidReceiveMessage: (fn) => ((panel.onMessage = fn), nothing) },
        onDidDispose: () => nothing, reveal() {}, dispose() {},
      };
      panels.push(panel);
      return panel;
    },
    terminals: [] as unknown[],
    createTerminal: (o: (typeof terminals)[number]) => (terminals.push(o), { show() {}, dispose() {} }),
  },
  workspace: {
    workspaceFolders: [{ uri: { scheme: 'file', fsPath: project } }],
    getConfiguration: () => ({ get: (k: string) => ({ command: [process.execPath, script], runsShown: 20, refreshSeconds: 3600, spendWarningPercent: 80 } as Record<string, unknown>)[k] }),
    onDidChangeConfiguration: () => nothing,
    onDidChangeWorkspaceFolders: () => nothing,
    createFileSystemWatcher: () => ({ onDidChange() {}, onDidCreate() {}, onDidDelete() {}, dispose() {} }),
  },
  commands: {
    registerCommand: (id: string, fn: (...a: unknown[]) => unknown) => (registered.set(id, fn), nothing),
    executeCommand: async (...args: unknown[]) => void executed.push(args),
  },
  extensions: { getExtension: () => undefined },
};

function calls(): string[][] {
  return fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)) : [];
}

async function until(what: () => boolean, label: string): Promise<void> {
  for (let i = 0; i < 200 && !what(); i++) {
    await new Promise((r) => setTimeout(r, 25));
  }
  assert.ok(what(), label);
}

before(() => {
  fs.mkdirSync(project);
  fs.writeFileSync(path.join(project, 'testbench.yaml'), 'plans: {}\n');
  fs.writeFileSync(script, FAKE_TESTBENCH);
  process.env.FAKE_TB_LOG = log;
  const load = (Module as unknown as { _load: (r: string, ...a: unknown[]) => unknown })._load;
  (Module as unknown as { _load: unknown })._load = function (request: string, ...rest: unknown[]) {
    return request === 'vscode' ? vscode : load.call(this, request, ...rest);
  };
  const ext = require('../src/extension') as { activate(c: unknown): void };
  const context = { subscriptions: [] as { dispose(): void }[], globalStorageUri: { fsPath: path.join(dir, 'global') }, storageUri: { fsPath: path.join(dir, 'ws') } };
  ext.activate(context);
  subscriptions = context.subscriptions;
});

after(() => subscriptions.forEach((s) => s.dispose()));

test('every command is registered, and the views and the status bar fill in', async () => {
  assert.deepEqual([...registered.keys()].sort(), Object.values(COMMANDS).sort());
  assert.deepEqual([...providers.keys()], Object.values(VIEWS));
  const plans = providers.get(VIEWS.plans)!;
  const nodes = await plans.getChildren() as unknown[];
  assert.deepEqual(nodes.map((n) => [plans.getTreeItem(n).label, plans.getTreeItem(n).description]),
    [['free', 'local · free'], ['asks', 'modal T4 · up to $0.87 · needs your approval']]);
  await until(() => status.text !== '', 'the status bar shows the spend');
  assert.equal(status.text, '$(credit-card) $8.50 of $10.00 today');
  assert.equal(status.backgroundColor?.id, 'statusBarItem.warningBackground', 'over 80% of the day limit');
  await until(() => fs.existsSync(path.join(dir, 'global', 'testbench.schema.json')), 'the schema is kept in storage');
});

test('the approval dialog is modal, shows the worst case, and Cancel runs nothing', async () => {
  const plans = providers.get(VIEWS.plans)!;
  const asks = (await plans.getChildren() as unknown[])[1];
  for (const click of [undefined, 'Cancel']) {
    shown.length = 0;
    answer = () => click;
    await registered.get(COMMANDS.runPlan)!(asks);
    const dialog = shown.find((s) => s.options?.modal);
    assert.ok(dialog, 'a modal dialog');
    assert.equal(dialog.message, 'Approve up to $0.87 for plan asks?');
    assert.match(dialog.options?.detail ?? '', /Worst case: \$0\.87\./);
    assert.deepEqual(dialog.items, [{ title: 'Approve and run' }, { title: 'Cancel', isCloseAffordance: true }]);
  }
  assert.ok(!calls().some((c) => c[0] === 'run'), 'nothing ran');
});

test('"Approve and run" runs it with --approve once, and follows it to its end', async () => {
  shown.length = 0;
  answer = (s) => (s.options?.modal ? 'Approve and run' : undefined);
  await registered.get(COMMANDS.runPlan)!('asks');
  const runs = calls().filter((c) => c[0] === 'run');
  assert.deepEqual(runs, [['run', '--approve', '--detach', '--', 'asks']]);
  assert.deepEqual(calls().at(-1), ['wait', '--timeout', '86400', '--', '20261112-120000-asks']);
  assert.ok(shown.some((s) => s.message === 'Testbench: plan asks passed (run 20261112-120000-asks).'), shown.map((s) => s.message).join('\n'));
});

test('a free plan runs without a dialog and without --approve', async () => {
  shown.length = 0;
  answer = () => 'Approve and run';
  const before = calls().length;
  await registered.get(COMMANDS.runPlan)!('free');
  assert.ok(!shown.some((s) => s.options?.modal));
  assert.deepEqual(calls().slice(before).filter((c) => c[0] === 'run'), [['run', '--detach', '--', 'free']]);
});

test('the dashboard opens in a terminal, with arguments as a list', async () => {
  await registered.get(COMMANDS.openDashboard)!();
  assert.deepEqual(terminals.map((t) => [t.name, t.shellPath, t.shellArgs, t.cwd]),
    [['Testbench dashboard', process.execPath, [script, 'ui', '--serve', '--open'], project]]);
});

test('a report opens in a webview locked to its run folder; evidence opens only from inside it', async () => {
  const runDir = path.join(project, '.testbench', 'runs', '20261112-120000-free');
  const log = path.join(runDir, 'steps', '01-train', 'log.txt');
  fs.mkdirSync(path.dirname(log), { recursive: true });
  fs.writeFileSync(log, 'hello\n');
  await registered.get(COMMANDS.openReport)!('20261112-120000-free');
  const [panel] = panels;
  assert.equal(panel.title, 'Report: 20261112-120000-free');
  assert.equal(panel.options.enableCommandUris, false);
  assert.deepEqual(panel.options.localResourceRoots?.map((u) => u.fsPath), [fs.realpathSync(runDir)]);
  assert.match(panel.webview.html, /<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src vscode-webview:\/\/fake data:;/);
  assert.doesNotMatch(panel.webview.html, /alert\(1\)/);
  assert.match(panel.webview.html, /data-evidence='steps\/01-train\/log\.txt'/);
  assert.deepEqual(fs.readdirSync(path.join(dir, 'ws')), [], 'the page written for it is removed once read');
  panel.onMessage?.({ type: 'open', path: 'steps/01-train/log.txt' });
  await until(() => executed.length === 1, 'the evidence file is opened');
  const real = fs.realpathSync(log);
  assert.deepEqual(executed[0], ['vscode.open', { fsPath: real, scheme: 'file', path: real }]);
  shown.length = 0;
  panel.onMessage?.({ type: 'open', path: '../../../testbench.yaml' });
  await until(() => shown.length === 1, 'a refusal');
  assert.match(shown[0].message, /not inside the run's folder, so it is not opened/);
  assert.equal(executed.length, 1);
  shown.length = 0;
  await registered.get(COMMANDS.openReport)!('--approve');
  assert.match(shown[0]?.message ?? '', /^Testbench: run "--approve" is not a name testbench uses/, 'a bad name is a message');
});

test('every --approve the stand-in testbench got came after a click on "Approve and run"', () => {
  const approved = calls().filter((c) => c.some((a) => a.includes('approve')));
  assert.equal(approved.length, 1);
});
