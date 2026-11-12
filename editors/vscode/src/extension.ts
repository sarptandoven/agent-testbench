/** agent-testbench in VS Code: the project's plans, runs and loop in a view of their own, today's spend in the status
 * bar, and commands that call the same `testbench` CLI an agent uses. Money decisions stay with the person: a plan
 * that needs approval runs only after they click "Approve and run" in a modal dialog that shows its worst case
 * (approval.ts). Nothing here approves on its own, and no setting can make it. */
import * as fs from 'node:fs';
import * as path from 'node:path';

import * as vscode from 'vscode';

import { runPlan, type Launched, type Ui } from './approval';
import { CliError, outcomeOf, parseCommand, Testbench } from './cli';
import { COMMANDS, VIEWS, type CommandKey } from './commands';
import { checkName, money } from './names';
import { findProjectRoot } from './project';
import { evidencePath, nonce, placeholder, prepareReport, runFolder } from './report';
import { contributeSchema, YAML_EXTENSION, type YamlApi } from './schema';
import { spendStatus } from './statusbar';
import { GOING, loopNodes, planNodes, runNodes, type Node } from './tree';
import type { CheckPlan, RunInfo } from './types';
import { NodeView } from './views';

const SECTION = 'agentTestbench';
const VERDICT: Record<string, string> = { passed: 'PASS', failed: 'FAIL', error: 'ERROR', cancelled: 'CANCELLED', running: 'STILL RUNNING' };

type Handler = (...args: unknown[]) => unknown;

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(new TestbenchExtension(context));
}

export function deactivate(): void {
  // everything is disposed through context.subscriptions
}

/** A run named by a tree item, a run id, or `testbench runs --json`'s entry. */
function runIdOf(arg: unknown): string | undefined {
  if (typeof arg === 'string') {
    return arg;
  }
  if (arg && typeof arg === 'object') {
    const o = arg as { run_id?: unknown; id?: unknown };
    if (typeof o.run_id === 'string') {
      return o.run_id;
    }
    if (typeof o.id === 'string' && o.id.startsWith('run:')) {
      return o.id.slice(4);
    }
  }
  return undefined;
}

function planOf(arg: unknown): string | undefined {
  if (typeof arg === 'string') {
    return arg;
  }
  const id = arg && typeof arg === 'object' ? (arg as { id?: unknown }).id : undefined;
  return typeof id === 'string' && id.startsWith('plan:') ? id.slice(5) : undefined;
}

function outcomeLine(p: CheckPlan): string {
  if (p.status && p.status in VERDICT) {
    return `${VERDICT[p.status]} (${(p.seconds ?? 0).toFixed(1)} s)`;
  }
  return `${p.status}: ${(p.why ?? '').replace(/[ .;]+$/, '')}${p.install ? `. Install: \`${p.install}\`` : ''}`;
}

class TestbenchExtension implements vscode.Disposable {
  private readonly out = vscode.window.createOutputChannel('Testbench');
  private readonly status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  private readonly plans = new NodeView(() => this.cli().plans().then(planNodes));
  private readonly runs = new NodeView(() => this.loadRuns());
  private readonly loop = new NodeView(() => this.cli().loop().then(loopNodes));
  private readonly panels = new Map<string, { panel: vscode.WebviewPanel; dir: string; status?: string }>();
  private readonly disposables: vscode.Disposable[] = [];
  private watcher: vscode.FileSystemWatcher | undefined;
  private timer: ReturnType<typeof setInterval> | undefined;
  private pending: ReturnType<typeof setTimeout> | undefined;
  private schemaText: string | undefined;
  private schemaRegistered = false;

  /** The modal dialogs of the approval flow. A button counts only if it is one of those offered; Cancel, Escape or
   * closing the dialog is undefined. */
  private readonly ui: Ui = {
    modal: async (message, detail, buttons) => {
      const items: vscode.MessageItem[] = [...buttons.map((title) => ({ title })), { title: 'Cancel', isCloseAffordance: true }];
      const picked = await vscode.window.showWarningMessage<vscode.MessageItem>(message, { modal: true, detail }, ...items);
      return picked && buttons.includes(picked.title) ? picked.title : undefined;
    },
    error: (text) => void vscode.window.showErrorMessage(`Testbench: ${text}`),
  };

  constructor(private readonly context: vscode.ExtensionContext) {
    this.disposables.push(
      this.out, this.status,
      vscode.window.registerTreeDataProvider(VIEWS.plans, this.plans),
      vscode.window.registerTreeDataProvider(VIEWS.runs, this.runs),
      vscode.window.registerTreeDataProvider(VIEWS.loop, this.loop),
      vscode.workspace.onDidChangeConfiguration((e) => {
        if (e.affectsConfiguration(SECTION)) {
          this.restart(e.affectsConfiguration(`${SECTION}.command`));
        }
      }),
      vscode.workspace.onDidChangeWorkspaceFolders(() => this.restart(false)),
    );
    const handlers: Record<CommandKey, Handler> = {
      check: () => this.check(),
      runPlan: (arg) => this.runPlanCommand(arg),
      showLatestReport: () => this.showLatestReport(),
      openReport: (arg) => this.openReport(arg),
      openDashboard: () => this.openDashboard(),
      verifyRun: (arg) => this.verifyRun(arg),
      cancelRun: (arg) => this.cancelRun(arg),
      showSpend: () => this.showSpend(),
      refresh: () => this.refreshAll(),
    };
    for (const key of Object.keys(COMMANDS) as CommandKey[]) {
      this.disposables.push(vscode.commands.registerCommand(COMMANDS[key], (...args: unknown[]) => this.guarded(handlers[key], args)));
    }
    this.status.command = COMMANDS.showSpend;
    this.status.name = 'Testbench spend today';
    const stored = this.schemaFile();
    if (fs.existsSync(stored)) {
      this.schemaText = fs.readFileSync(stored, 'utf8');
    }
    this.restart(true);
  }

  dispose(): void {
    clearInterval(this.timer);
    clearTimeout(this.pending);
    this.watcher?.dispose();
    for (const { panel } of this.panels.values()) {
      panel.dispose();
    }
    this.disposables.forEach((d) => d.dispose());
  }

  // ---- the project and the CLI -------------------------------------------------------------------------------------

  private folders(): string[] {
    return (vscode.workspace.workspaceFolders ?? []).filter((f) => f.uri.scheme === 'file').map((f) => f.uri.fsPath);
  }

  /** Where testbench runs: the folder with testbench.yaml (or .testbench), else the first folder. */
  private root(): string {
    const folders = this.folders();
    const root = findProjectRoot(folders) ?? folders[0];
    if (!root) {
      throw new CliError('bad-setting', 'open the project folder first (File > Open Folder)');
    }
    return root;
  }

  private cli(): Testbench {
    return new Testbench({ command: parseCommand(vscode.workspace.getConfiguration(SECTION).get('command')), cwd: this.root() });
  }

  private setting(name: string, fallback: number): number {
    const v = vscode.workspace.getConfiguration(SECTION).get<number>(name);
    return typeof v === 'number' && Number.isFinite(v) ? v : fallback;
  }

  private restart(commandChanged: boolean): void {
    this.watcher?.dispose();
    this.watcher = undefined;
    clearInterval(this.timer);
    const folders = this.folders();
    const root = findProjectRoot(folders) ?? folders[0];
    if (root) {
      this.watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(root,
        '{testbench.yaml,.testbench/ledger.jsonl,.testbench/check.json,.testbench/runs/*/state.json,.testbench/loops/*/state.json}'));
      const later = () => this.scheduleRefresh();
      this.watcher.onDidChange(later);
      this.watcher.onDidCreate(later);
      this.watcher.onDidDelete(later);
    }
    this.timer = setInterval(() => this.refreshAll(), Math.max(5, this.setting('refreshSeconds', 30)) * 1000);
    this.refreshAll();
    if (commandChanged) {
      void this.loadSchema();
      void this.logVersion();
    }
  }

  private async logVersion(): Promise<void> {
    try {
      const cli = this.cli();
      this.out.appendLine(`${await cli.version()} (${cli.display})`);
    } catch (err) {
      this.out.appendLine(err instanceof Error ? err.message : String(err));
    }
  }

  private scheduleRefresh(): void {
    clearTimeout(this.pending);
    this.pending = setTimeout(() => this.refreshAll(), 700);
  }

  private refreshAll(): void {
    this.plans.refresh();
    this.runs.refresh();
    this.loop.refresh();
    void this.updateSpend();
  }

  /** A command's errors as a message in plain words; testbench missing gets a way to the setting. */
  private async guarded(fn: Handler, args: unknown[]): Promise<void> {
    try {
      await fn(...args);
    } catch (err) {
      const text = err instanceof Error ? err.message : String(err);
      const fix = err instanceof CliError && (err.kind === 'not-found' || err.kind === 'bad-setting') ? 'Open settings' : undefined;
      const choice = await vscode.window.showErrorMessage(`Testbench: ${text}`, ...(fix ? [fix] : []));
      if (choice === 'Open settings') {
        await vscode.commands.executeCommand('workbench.action.openSettings', `${SECTION}.command`);
      }
    }
  }

  // ---- views and the status bar ------------------------------------------------------------------------------------

  private async loadRuns(): Promise<Node[]> {
    const data = await this.cli().runs(this.setting('runsShown', 20));
    for (const r of data.runs) {                     // an open report of a run that has since ended shows its report
      const open = this.panels.get(r.run_id);
      if (open && open.status !== undefined && open.status !== r.status) {
        void this.renderReport(r.run_id).catch(() => undefined);
      }
    }
    return runNodes(data, Date.now());
  }

  private async updateSpend(): Promise<void> {
    try {
      const s = spendStatus(await this.cli().spend(), this.setting('spendWarningPercent', 80));
      this.status.text = s.text;
      this.status.tooltip = s.tooltip;
      this.status.backgroundColor = s.level === 'ok' ? undefined
        : new vscode.ThemeColor(s.level === 'error' ? 'statusBarItem.errorBackground' : 'statusBarItem.warningBackground');
    } catch (err) {
      this.status.text = '$(warning) testbench';
      this.status.tooltip = `Today's spend could not be read: ${err instanceof Error ? err.message : String(err)}`;
      this.status.backgroundColor = undefined;
    }
    this.status.show();
  }

  // ---- autocomplete for testbench.yaml -----------------------------------------------------------------------------

  private schemaFile(): string {
    return path.join(this.context.globalStorageUri.fsPath, 'testbench.schema.json');
  }

  /** `testbench schema` kept in the extension's storage and given to the YAML extension, so testbench.yaml completes
   * offline with the installed version's schema; until then, or without it, package.json's yamlValidation (GitHub). */
  private async loadSchema(): Promise<void> {
    try {
      const text = await this.cli().schema();
      fs.mkdirSync(path.dirname(this.schemaFile()), { recursive: true });
      fs.writeFileSync(this.schemaFile(), text);
      this.schemaText = text;
    } catch (err) {
      this.out.appendLine(`testbench schema: ${err instanceof Error ? err.message : String(err)} (autocomplete in testbench.yaml uses ${this.schemaText ? 'the copy kept from before' : 'the schema on GitHub'})`);
    }
    const yaml = vscode.extensions.getExtension<YamlApi>(YAML_EXTENSION);
    if (!this.schemaRegistered && yaml) {
      try {
        const api = await yaml.activate();
        this.schemaRegistered = typeof api?.registerContributor === 'function' && contributeSchema(api, () => this.schemaText);
      } catch (err) {
        this.out.appendLine(`The YAML extension did not take the schema: ${err instanceof Error ? err.message : String(err)}`);
      }
    }
  }

  // ---- commands ----------------------------------------------------------------------------------------------------

  private async check(): Promise<void> {
    const cli = this.cli();
    this.out.show(true);
    this.out.appendLine(`\n$ ${cli.display} check --json`);
    const summary = await vscode.window.withProgress({
      location: vscode.ProgressLocation.Notification, title: 'Testbench: checking this project (free plans only)', cancellable: true,
    }, async (_progress, token) => {
      const stop = new AbortController();
      token.onCancellationRequested(() => stop.abort());
      try {
        return await cli.check({ signal: stop.signal });
      } catch (err) {
        if (err instanceof CliError && err.kind === 'cancelled') {
          return undefined;
        }
        throw err;
      }
    });
    this.refreshAll();
    if (!summary) {
      void vscode.window.showInformationMessage('Testbench: check stopped; the run it had started was cancelled.');
      return;
    }
    for (const p of summary.plans) {
      this.out.appendLine(`${p.plan}: ${p.what} ... ${outcomeLine(p)}`);
    }
    if (summary.message) {
      this.out.appendLine(summary.message);
    }
    this.out.appendLine(`Next: ${summary.next}`);
    const failed = summary.plans.find((p) => p.run && ['failed', 'error', 'cancelled'].includes(p.status ?? ''));
    const counts = `${summary.passed} passed, ${summary.failed} failed` + (summary.still_running ? `, ${summary.still_running} still running` : '')
      + (summary.skipped ? `, ${summary.skipped} skipped` : '') + (summary.refused ? `, ${summary.refused} refused` : '');
    if (failed?.run) {
      const choice = await vscode.window.showErrorMessage(`Testbench: plan ${failed.plan} failed (${counts}).`, 'Show report');
      if (choice) {
        await this.openReport(failed.run);
      }
    } else if (summary.exit_code === 2) {
      void vscode.window.showWarningMessage(`Testbench: ${summary.message ?? summary.next}`);
    } else {
      void vscode.window.showInformationMessage(`Testbench: ${counts}. Next: ${summary.next}`);
    }
  }

  private async runPlanCommand(arg: unknown): Promise<void> {
    const cli = this.cli();
    let name = planOf(arg);
    if (!name) {
      const data = await cli.plans();
      const pick = await vscode.window.showQuickPick(data.plans.map((p) => ({
        label: p.name,
        description: `${p.backend}${p.gpu ? ` ${p.gpu}` : ''} · ${p.worst_case_usd === 0 ? 'free' : `up to ${money(p.worst_case_usd)}`}`,
        detail: p.message,
      })), { placeHolder: 'Run which plan? One that needs approval asks you first.' });
      name = pick?.label;
    }
    if (!name) {
      return;
    }
    const launched = await runPlan(checkName(name, 'plan'), { cli, ui: this.ui });
    if (launched) {
      await this.follow(cli, launched);
    }
  }

  /** Follows a started run in a notification and the output channel; at its end, its outcome and its report. */
  private async follow(cli: Testbench, l: Launched): Promise<void> {
    this.out.appendLine(`\n[testbench] ${l.text}${l.approved ? ` - approved by you, up to ${money(l.worstCaseUsd)}` : ''}`);
    this.refreshAll();
    const result = await vscode.window.withProgress({
      location: vscode.ProgressLocation.Notification, title: `Testbench: plan ${l.plan}`, cancellable: true,
    }, async (progress, token) => {
      const stop = new AbortController();
      token.onCancellationRequested(() => stop.abort());
      progress.report({ message: `run ${l.runId}` });
      try {
        return await cli.wait(l.runId, (line) => {
          this.out.appendLine(line);
          const step = /^\[testbench\] (step .*)$/.exec(line);
          if (step) {
            progress.report({ message: step[1] });
          }
        }, stop.signal);
      } catch (err) {
        if (err instanceof CliError && err.kind === 'cancelled') {
          return undefined;
        }
        throw err;
      }
    });
    this.refreshAll();
    if (!result) {
      void vscode.window.showInformationMessage(`Testbench: no longer following run ${l.runId}; it goes on. The Runs view shows it, and Cancel run stops it.`);
      return;
    }
    const outcome = outcomeOf(result.code);
    const text = outcome === 'passed' ? `plan ${l.plan} passed` : outcome === 'failed' ? `plan ${l.plan} failed`
      : `run ${l.runId} is still going`;
    const said = `Testbench: ${text} (run ${l.runId}).`;
    const choice = outcome === 'failed' ? await vscode.window.showErrorMessage(said, 'Show report')
      : await vscode.window.showInformationMessage(said, 'Show report');
    if (choice) {
      await this.openReport(l.runId);
    }
  }

  private async pickRun(placeHolder: string, only?: (r: RunInfo) => boolean): Promise<string | undefined> {
    const runs = [...(await this.cli().runs(50)).runs].reverse().filter(only ?? (() => true));
    if (runs.length === 0) {
      void vscode.window.showInformationMessage('Testbench: no runs to choose from.');
      return undefined;
    }
    const pick = await vscode.window.showQuickPick(runs.map((r) => ({
      label: r.run_id, description: `${r.plan ?? ''} · ${r.status ?? ''}`, detail: r.line,
    })), { placeHolder });
    return pick?.label;
  }

  private async showLatestReport(): Promise<void> {
    const latest = (await this.cli().runs(1)).runs.at(-1);
    if (!latest) {
      void vscode.window.showInformationMessage('Testbench: no runs yet. Run a plan, or check this project.');
      return;
    }
    await this.openReport(latest.run_id);
  }

  /** A run's report in a webview: a strict CSP, scripts only the extension's own, local files only from that run's
   * folder. */
  private async openReport(arg?: unknown): Promise<void> {
    const runId = runIdOf(arg) ?? await this.pickRun('Show the report of which run?');
    if (!runId) {
      return;
    }
    checkName(runId, 'run');
    const dir = runFolder(this.root(), runId, undefined);
    if (!dir) {
      throw new CliError('refused', `there is no folder for run ${runId} in .testbench/runs`);
    }
    const open = this.panels.get(runId);
    if (open) {
      open.panel.reveal();
    } else {
      const panel = vscode.window.createWebviewPanel('agentTestbench.report', `Report: ${runId}`, vscode.ViewColumn.Active, {
        enableScripts: true,                   // the extension's own script only: the page's CSP allows nothing else
        enableCommandUris: false,
        enableForms: false,
        localResourceRoots: [vscode.Uri.file(dir)],
      });
      panel.webview.onDidReceiveMessage((msg: unknown) => void this.openEvidence(dir, msg));
      panel.onDidDispose(() => this.panels.delete(runId));
      this.panels.set(runId, { panel, dir });
    }
    await this.renderReport(runId);
  }

  private async renderReport(runId: string): Promise<void> {
    const open = this.panels.get(runId);
    if (!open) {
      return;
    }
    const cli = this.cli();
    const storage = (this.context.storageUri ?? this.context.globalStorageUri).fsPath;
    fs.mkdirSync(storage, { recursive: true });
    const file = path.join(storage, `report-${runId}.html`);
    const got = await cli.reportHtml(runId, file);
    const n = nonce();
    const webview = open.panel.webview;
    if (got.written) {
      webview.html = prepareReport(fs.readFileSync(file, 'utf8'), webview.cspSource, n);
      fs.rmSync(file, { force: true });
    } else {
      webview.html = placeholder(webview.cspSource, n, `Run ${runId} has no report yet`,
        `${got.text}\n\nThis page shows the report once the run ends.`);
    }
    const now = (await cli.runs(50)).runs.find((r) => r.run_id === runId);
    open.status = now?.status ?? undefined;
  }

  private async openEvidence(dir: string, msg: unknown): Promise<void> {
    const m = msg as { type?: unknown; path?: unknown } | undefined;
    if (!m || m.type !== 'open') {
      return;
    }
    const file = evidencePath(dir, m.path);
    if (!file) {
      void vscode.window.showWarningMessage('Testbench: that file is not inside the run\'s folder, so it is not opened.');
      return;
    }
    await vscode.commands.executeCommand('vscode.open', vscode.Uri.file(file));
  }

  /** `testbench ui --serve --open` in a terminal of its own: the live, read-only dashboard on 127.0.0.1. */
  private openDashboard(): void {
    const cli = this.cli();
    const { file, prefix } = cli.resolve();
    const name = 'Testbench dashboard';
    const existing = vscode.window.terminals.find((t) => t.name === name && t.exitStatus === undefined);
    const terminal = existing ?? vscode.window.createTerminal({
      name, shellPath: file, shellArgs: [...prefix, 'ui', '--serve', '--open'], cwd: this.root(),
      iconPath: new vscode.ThemeIcon('dashboard'),
    });
    terminal.show();
  }

  private async verifyRun(arg?: unknown): Promise<void> {
    const runId = runIdOf(arg) ?? await this.pickRun('Verify which run\'s evidence?', (r) => !GOING.has(String(r.status)));
    if (!runId) {
      return;
    }
    const r = await this.cli().verify(runId);
    const text = (r.stdout.trim() || r.stderr.trim()).replace(/^testbench: /, '');
    this.out.appendLine(`\n$ testbench verify ${runId}\n${text}`);
    const first = text.split('\n')[0];
    const outcome = outcomeOf(r.code);
    if (outcome === 'passed') {
      void vscode.window.showInformationMessage(`Testbench: ${first}`);
    } else if (outcome === 'failed') {
      if (await vscode.window.showErrorMessage(`Testbench: ${first}`, 'Show what changed')) {
        this.out.show(true);
      }
    } else {
      void vscode.window.showWarningMessage(`Testbench: ${text}`);
    }
  }

  private async cancelRun(arg?: unknown): Promise<void> {
    const runId = runIdOf(arg) ?? await this.pickRun('Cancel which run?', (r) => GOING.has(String(r.status)));
    if (!runId) {
      return;
    }
    const sure = await vscode.window.showWarningMessage(`Cancel run ${runId}?`, {
      modal: true, detail: 'It is stopped here and on its remote machine; what it did so far stays in its folder.',
    }, { title: 'Cancel run' }, { title: 'Keep it running', isCloseAffordance: true });
    if (sure?.title !== 'Cancel run') {
      return;
    }
    const r = await this.cli().cancel(runId);
    this.out.appendLine(`\n$ testbench cancel ${runId}\n${r.stdout.trim()}`);
    void vscode.window.showInformationMessage(`Testbench: ${r.stdout.trim().split('\n')[0] || `run ${runId} cancelled`}`);
    this.refreshAll();
  }

  private async showSpend(): Promise<void> {
    const r = await this.cli().spendText();
    this.out.appendLine(`\n$ testbench spend\n${(r.stdout + r.stderr).trim()}`);
    this.out.show(true);
    void this.updateSpend();
  }
}
