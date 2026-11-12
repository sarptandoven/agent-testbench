/** The testbench CLI as the extension runs it: a program and an array of arguments, started directly (never through a
 * shell), its --json output parsed, and its exit code read the way every testbench command uses it:
 * 0 passed or done, 1 failed, 2 refused or bad input (nothing ran), 3 still running. */
import { spawn } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { consume, type Consent } from './approval';
import { checkName } from './names';
import type { CheckJson, LoopJson, PlansJson, RunsJson, SpendJson } from './types';

export type Outcome = 'passed' | 'failed' | 'refused' | 'running';

/** testbench's exit codes (docs/reference.md#exit-codes). Anything else means it crashed or was stopped. */
export function outcomeOf(code: number | null): Outcome | undefined {
  return ({ 0: 'passed', 1: 'failed', 2: 'refused', 3: 'running' } as Record<number, Outcome>)[code ?? -1];
}

export type ErrorKind =
  | 'not-found'   // the program is not on PATH, or not at the path given
  | 'bad-setting' // agentTestbench.command cannot be used as it is
  | 'refused'     // exit 2: refused or bad input; nothing ran
  | 'failed'      // exit 1
  | 'too-old'     // the installed testbench lacks a --json form the extension reads
  | 'bad-json'    // exit 0, but the output is not the JSON asked for
  | 'crashed'     // any other exit, or a signal
  | 'timeout'
  | 'cancelled';

export interface ExecResult {
  code: number | null;
  stdout: string;
  stderr: string;
}

export class CliError extends Error {
  constructor(readonly kind: ErrorKind, message: string, readonly result?: ExecResult) {
    super(message);
    this.name = 'CliError';
  }
}

export interface ExecOptions {
  cwd: string;
  env: NodeJS.ProcessEnv;
  /** each line of output as it comes */
  onLine?: (line: string) => void;
  signal?: AbortSignal;
  timeoutMs?: number;
  /** how to stop it when cancelled: SIGINT lets `testbench check` cancel the run it started */
  stopSignal?: NodeJS.Signals;
}

/** Starts a program with an array of arguments; the extension's only way to start anything. */
export type Exec = (file: string, args: readonly string[], opts: ExecOptions) => Promise<ExecResult>;

export const nodeExec: Exec = (file, args, opts) =>
  new Promise((resolve, reject) => {
    const child = spawn(file, [...args], {
      cwd: opts.cwd, env: opts.env, shell: false, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    let pending = '';
    let stopped: 'timeout' | 'cancelled' | undefined;
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (d: string) => {
      stdout += d;
      if (opts.onLine) {
        pending += d;
        let i: number;
        while ((i = pending.indexOf('\n')) >= 0) {
          opts.onLine(pending.slice(0, i).replace(/\r$/, ''));
          pending = pending.slice(i + 1);
        }
      }
    });
    child.stderr.on('data', (d: string) => {
      stderr += d;
    });
    const stop = (why: 'timeout' | 'cancelled') => {
      stopped = why;
      child.kill(opts.stopSignal ?? 'SIGTERM');
    };
    const onAbort = () => stop('cancelled');
    const timer = opts.timeoutMs ? setTimeout(() => stop('timeout'), opts.timeoutMs) : undefined;
    opts.signal?.addEventListener('abort', onAbort, { once: true });
    const done = () => {
      if (timer) {
        clearTimeout(timer);
      }
      opts.signal?.removeEventListener('abort', onAbort);
    };
    child.on('error', (err) => {
      done();
      reject(err);
    });
    child.on('close', (code) => {
      done();
      if (pending && opts.onLine) {
        opts.onLine(pending);
      }
      const result = { code, stdout, stderr };
      if (stopped === 'timeout') {
        reject(new CliError('timeout', `testbench took longer than ${Math.round((opts.timeoutMs ?? 0) / 1000)} s and was stopped`, result));
      } else if (stopped === 'cancelled') {
        reject(new CliError('cancelled', 'stopped', result));
      } else {
        resolve(result);
      }
    });
    if (opts.signal?.aborted) {
      onAbort();
    }
  });

/** agentTestbench.command as a program and its first arguments: "testbench", "uvx agent-testbench", or a list such as
 * ["C:\\Tools\\testbench.exe"] for a path with spaces. Double quotes group words in the string form; nothing else
 * is interpreted - there is no shell. */
export function parseCommand(setting: unknown): string[] {
  let words: string[] | undefined;
  if (Array.isArray(setting)) {
    words = setting.every((w) => typeof w === 'string' && w.length > 0) ? [...setting] : undefined;
  } else if (typeof setting === 'string') {
    words = [...setting.matchAll(/"([^"]*)"|(\S+)/g)].map((m) => m[1] ?? m[2]).filter((w) => w.length > 0);
  }
  if (!words || words.length === 0) {
    throw new CliError('bad-setting', 'agentTestbench.command is empty or not text: set it to "testbench", or e.g. "uvx agent-testbench", in your user settings');
  }
  return words;
}

/** The program to start, found on PATH the way a shell would - except that the current folder is never searched (a
 * project cannot put a `testbench` of its own in front of the real one). undefined when it is not there. */
export function resolveProgram(program: string, env: NodeJS.ProcessEnv, platform: NodeJS.Platform = process.platform,
  isFile: (p: string) => boolean = fileExists): string | undefined {
  const p = platform === 'win32' ? path.win32 : path.posix;
  if (program === '~' || program.startsWith('~/') || program.startsWith('~\\')) {
    program = p.join(os.homedir(), program.slice(1));
  }
  const exts = platform === 'win32'
    ? ['', ...(env.PATHEXT ?? env.Pathext ?? '.COM;.EXE;.BAT;.CMD').split(';').filter(Boolean)]
    : [''];
  const first = (base: string) => exts.map((e) => base + e).find(isFile);
  if (p.isAbsolute(program)) {
    return first(program);
  }
  if (program.includes('/') || (platform === 'win32' && program.includes('\\'))) {
    throw new CliError('bad-setting', `agentTestbench.command starts with a relative path (${program}): give a name on PATH or an absolute path`);
  }
  const dirs = (env.PATH ?? env.Path ?? '').split(p.delimiter).filter((d) => d && p.isAbsolute(d));
  for (const dir of dirs) {
    const found = first(p.join(dir, program));
    if (found) {
      return found;
    }
  }
  return undefined;
}

function fileExists(p: string): boolean {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

export interface RunOptions {
  onLine?: (line: string) => void;
  signal?: AbortSignal;
  timeoutMs?: number;
  stopSignal?: NodeJS.Signals;
  /** a person's approval of exactly this launch (approval.ts) */
  consent?: Consent;
}

export interface TestbenchOptions {
  /** agentTestbench.command, parsed */
  command: readonly string[];
  /** the project folder: where testbench.yaml is */
  cwd: string;
  exec?: Exec;
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  isFile?: (p: string) => boolean;
}

export interface Started {
  runId: string;
  /** testbench's own line about it, e.g. "run 2026... started (Within budget: up to $0.00 ...)" */
  text: string;
}

/** Anything that looks like --approve. testbench takes options only spelled out in full, but the extension refuses
 * every spelling that could reach it. */
function approves(args: readonly string[]): boolean {
  return args.some((a) => /^--?ap/i.test(a));
}

const JSON_TIMEOUT_MS = 120_000;   // a list or an estimate; uvx may first fetch the package

export class Testbench {
  /** the command as a person would type it, for messages */
  readonly display: string;

  constructor(private readonly o: TestbenchOptions) {
    this.display = o.command.join(' ');
  }

  /** Runs testbench with these arguments. Something like --approve is refused here unless it comes with a person's
   * consent to exactly this launch, which only approval.ts's launchApproved holds, right after the person clicked
   * "Approve and run". */
  async run(args: readonly string[], opts: RunOptions = {}): Promise<ExecResult> {
    if (approves(args) && !consume(opts.consent, args)) {
      throw new CliError('refused', 'Not run: the extension passes --approve only right after a person clicked "Approve and run" in the dialog that showed the plan\'s worst case.');
    }
    const env = this.o.env ?? process.env;
    const { file, prefix } = this.resolve();
    try {
      return await (this.o.exec ?? nodeExec)(file, [...prefix, ...args], {
        cwd: this.o.cwd,
        env: { ...env, PYTHONUNBUFFERED: '1', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
        onLine: opts.onLine, signal: opts.signal, timeoutMs: opts.timeoutMs, stopSignal: opts.stopSignal,
      });
    } catch (err) {
      if (err instanceof CliError) {
        throw err;
      }
      const e = err as NodeJS.ErrnoException;
      if (e.code === 'ENOENT') {
        throw new CliError('not-found', `${file} could not be started (not found)`);
      }
      throw new CliError('crashed', `${this.display} could not be started: ${e.message}`);
    }
  }

  /** The program the command names, found on PATH (never in the project folder), and the arguments that go first. */
  resolve(): { file: string; prefix: string[] } {
    const platform = this.o.platform ?? process.platform;
    const [program, ...prefix] = this.o.command;
    if (approves(prefix)) {
      throw new CliError('bad-setting', 'agentTestbench.command may not carry an approval: a person approves each run that needs it, in the dialog that shows its worst case');
    }
    const file = resolveProgram(program, this.o.env ?? process.env, platform, this.o.isFile);
    if (!file) {
      throw new CliError('not-found', `${program} was not found${/[\\/]/.test(program) ? '' : ' on PATH'}. Install agent-testbench (pip install agent-testbench, or uv tool install agent-testbench), or set agentTestbench.command in your user settings, e.g. to "uvx agent-testbench".`);
    }
    if (platform === 'win32' && /\.(cmd|bat)$/i.test(file)) {
      throw new CliError('bad-setting', `${file} is a batch file, which cannot be started without a shell; point agentTestbench.command at testbench.exe (in your Python's Scripts folder) or at uvx.exe`);
    }
    return { file, prefix };
  }

  /** Runs a --json form and parses what it prints. okCodes: the exit codes that still print JSON (loop status: 3 while
   * a try runs; check: every outcome). */
  async json<T>(args: readonly string[], okCodes: readonly number[] = [0], opts: RunOptions = {}): Promise<T> {
    const r = await this.run(args, { timeoutMs: JSON_TIMEOUT_MS, ...opts });
    if (r.code === null || !okCodes.includes(r.code)) {
      throw this.failure(args, r);
    }
    try {
      return JSON.parse(r.stdout) as T;
    } catch {
      const said = (r.stdout + r.stderr).trim().slice(0, 300);
      throw new CliError('bad-json', `\`testbench ${args.join(' ')}\` did not print JSON${said ? `: ${said}` : ''}`, r);
    }
  }

  /** The error for an exit code the caller did not expect, in testbench's own words where it gave some. */
  failure(args: readonly string[], r: ExecResult): CliError {
    const said = (r.stdout.trim() || r.stderr.trim()).replace(/^testbench: /, '');
    const cmd = args.filter((a) => !a.startsWith('-')).slice(0, 2).join(' ');
    if (/unrecognized arguments: .*--json/.test(r.stderr)) {
      return new CliError('too-old', `${this.display} is older than this extension: it has no \`testbench ${cmd} --json\`. Upgrade agent-testbench (pip install -U agent-testbench, or uv tool upgrade agent-testbench).`, r);
    }
    const kind = outcomeOf(r.code);
    if (kind === 'refused') {
      return new CliError('refused', said || `testbench ${cmd} refused (exit 2)`, r);
    }
    if (kind === 'failed') {
      return new CliError('failed', said || `testbench ${cmd} failed (exit 1)`, r);
    }
    return new CliError('crashed', `testbench ${cmd} stopped with ${r.code === null ? 'a signal' : `exit code ${r.code}`}${said ? `: ${said.slice(-500)}` : ''}`, r);
  }

  /** A plan launched in the background: `testbench run --detach` exits 3 with "run ID started". */
  async launch(args: readonly string[], opts: RunOptions = {}): Promise<Started> {
    const r = await this.run(args, { timeoutMs: JSON_TIMEOUT_MS, ...opts });
    const m = /\[testbench\] run (\S+) started(.*)/.exec(r.stdout);
    if (r.code === 3 && m) {
      return { runId: checkName(m[1], 'run'), text: `run ${m[1]} started${m[2]}` };
    }
    throw this.failure(args, r);
  }

  version(): Promise<string> {
    return this.run(['--version'], { timeoutMs: JSON_TIMEOUT_MS }).then((r) => {
      if (r.code !== 0) {
        throw this.failure(['--version'], r);
      }
      return r.stdout.trim();
    });
  }

  plans(): Promise<PlansJson> {
    return this.json<PlansJson>(['plans', '--json']);
  }

  runs(n: number): Promise<RunsJson> {
    return this.json<RunsJson>(['runs', '--json', '-n', String(Math.max(1, Math.floor(n)))]);
  }

  spend(): Promise<SpendJson> {
    return this.json<SpendJson>(['spend', '--json']);
  }

  /** The running loop, else the latest; read-only (it never keeps or reverts a try). */
  loop(): Promise<LoopJson> {
    return this.json<LoopJson>(['loop', 'status', '--json'], [0, 3]);
  }

  /** Every plan that is free and needs no approval, run now; the others only listed. */
  check(opts: RunOptions = {}): Promise<CheckJson> {
    return this.json<CheckJson>(['check', '--json'], [0, 1, 2, 3], { timeoutMs: 0, stopSignal: 'SIGINT', ...opts });
  }

  /** testbench.yaml's JSON Schema, with the installed check plug-ins' descriptions. */
  async schema(): Promise<string> {
    const r = await this.run(['schema'], { timeoutMs: JSON_TIMEOUT_MS });
    if (r.code !== 0) {
      throw this.failure(['schema'], r);
    }
    const parsed: unknown = JSON.parse(r.stdout);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new CliError('bad-json', '`testbench schema` did not print a JSON Schema');
    }
    return r.stdout;
  }

  /** A plan that needs no approval, started in the background. */
  async start(plan: string): Promise<Started> {
    return this.launch(['run', '--detach', '--', checkName(plan, 'plan')]);
  }

  /** Follows a run until it ends (exit 0 passed, 1 failed) or the signal stops the following (the run goes on). */
  async wait(run: string, onLine: (line: string) => void, signal: AbortSignal): Promise<ExecResult> {
    return this.run(['wait', '--timeout', '86400', '--', checkName(run, 'run')], { onLine, signal });
  }

  /** Writes a run's report as one HTML page to `file`. written: false when the run has no report yet; text: what
   * testbench said instead (the run's status line, or its markdown report). */
  async reportHtml(run: string, file: string): Promise<{ written: boolean; text: string }> {
    const r = await this.run(['report', '--format', 'html', '-o', file, '--', checkName(run, 'run')], { timeoutMs: JSON_TIMEOUT_MS });
    if (r.code !== 0) {
      throw this.failure(['report'], r);
    }
    return { written: /^wrote /m.test(r.stdout), text: r.stdout.trim() };
  }

  /** exit 0: every evidence file matches its sha256; 1: what changed; 2: it could not be checked. */
  async verify(run: string): Promise<ExecResult> {
    return this.run(['verify', '--', checkName(run, 'run')], { timeoutMs: JSON_TIMEOUT_MS });
  }

  async cancel(run: string): Promise<ExecResult> {
    return this.run(['cancel', '--', checkName(run, 'run')], { timeoutMs: JSON_TIMEOUT_MS });
  }

  /** `testbench spend` as text, for a person to read. */
  spendText(): Promise<ExecResult> {
    return this.run(['spend'], { timeoutMs: JSON_TIMEOUT_MS });
  }
}
