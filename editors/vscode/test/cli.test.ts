/** The CLI wrapper: arguments always as an array (never a shell string), the program found on PATH but never in the
 * project folder, --json parsed, and testbench's exit codes read as testbench means them. */
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';

import { CliError, nodeExec, outcomeOf, parseCommand, resolveProgram, Testbench } from '../src/cli';
import { BadName, checkName, money } from '../src/names';
import { fakeCli, ok } from './fakes';

function rejects(p: Promise<unknown>, kind: string, says?: RegExp) {
  return assert.rejects(p, (err: unknown) => {
    assert.ok(err instanceof CliError, `a CliError, not ${String(err)}`);
    assert.equal(err.kind, kind, err.message);
    if (says) {
      assert.match(err.message, says);
    }
    return true;
  });
}

test('the exit codes mean what they mean everywhere in testbench', () => {
  assert.deepEqual([0, 1, 2, 3, 4, null].map(outcomeOf), ['passed', 'failed', 'refused', 'running', undefined, undefined]);
});

test('agentTestbench.command is a program and its first arguments, with no shell in between', () => {
  assert.deepEqual(parseCommand('testbench'), ['testbench']);
  assert.deepEqual(parseCommand('  uvx   agent-testbench '), ['uvx', 'agent-testbench']);
  assert.deepEqual(parseCommand('"C:\\Program Files\\tb\\testbench.exe" --x'), ['C:\\Program Files\\tb\\testbench.exe', '--x']);
  assert.deepEqual(parseCommand(['uvx', 'agent-testbench@0.2.0']), ['uvx', 'agent-testbench@0.2.0']);
  assert.deepEqual(parseCommand('testbench; rm -rf ~'), ['testbench;', 'rm', '-rf', '~'], 'words, not a shell line');
  for (const bad of ['', '   ', [], ['uvx', ''], ['uvx', 3], 5, null, undefined]) {
    assert.throws(() => parseCommand(bad), (e: unknown) => e instanceof CliError && e.kind === 'bad-setting', JSON.stringify(bad));
  }
});

test('the program is found on PATH, never in the current folder', () => {
  const files = new Set(['/home/me/.local/bin/testbench', '/project/testbench', '/opt/tb/testbench']);
  const isFile = (p: string) => files.has(p);
  assert.equal(resolveProgram('testbench', { PATH: '.:bin:/home/me/.local/bin' }, 'linux', isFile), '/home/me/.local/bin/testbench');
  assert.equal(resolveProgram('testbench', { PATH: '.:/nowhere' }, 'linux', isFile), undefined, 'relative PATH entries are skipped');
  assert.equal(resolveProgram('/opt/tb/testbench', {}, 'linux', isFile), '/opt/tb/testbench');
  assert.equal(resolveProgram('/opt/tb/missing', {}, 'linux', isFile), undefined);
  assert.throws(() => resolveProgram('./testbench', { PATH: '/x' }, 'linux', isFile), /relative path/);
  assert.equal(resolveProgram('~/bin/testbench', {}, 'linux', (p) => p === path.join(os.homedir(), 'bin/testbench')),
    path.join(os.homedir(), 'bin/testbench'));
});

test('on Windows, PATHEXT names the files to try', () => {
  const isFile = (p: string) => p === 'C:\\Py\\Scripts\\testbench.EXE';
  const env = { Path: '.;C:\\Py\\Scripts', PATHEXT: '.COM;.EXE;.BAT' };
  assert.equal(resolveProgram('testbench', env, 'win32', isFile), 'C:\\Py\\Scripts\\testbench.EXE');
  const batch = new Testbench({ command: ['testbench'], cwd: 'C:\\p', env: { Path: 'C:\\Py', PATHEXT: '.CMD' }, platform: 'win32',
    isFile: (p) => p === 'C:\\Py\\testbench.CMD', exec: async () => ok('') });
  return rejects(batch.run(['plans']), 'bad-setting', /batch file/);
});

test('arguments go to the program as an array, after the command\'s own, in the project folder', async () => {
  const { cli, calls } = fakeCli((args) => (args[0] === 'run' ? ok('[testbench] run 1-smoke started', 3) : ok('{"runs": []}')));
  await cli.runs(20);
  await cli.plans();
  await cli.spend();
  await cli.start('smoke');
  await cli.verify('20261112-0916-smoke');
  await cli.cancel('last');
  assert.deepEqual(calls.map((c) => c.args), [
    ['runs', '--json', '-n', '20'], ['plans', '--json'], ['spend', '--json'], ['run', '--detach', '--', 'smoke'],
    ['verify', '--', '20261112-0916-smoke'], ['cancel', '--', 'last'],
  ]);
  assert.ok(calls.every((c) => c.file === '/usr/bin/testbench' && c.opts.cwd === '/project'));
  assert.equal(calls[0].opts.env.PYTHONUTF8, '1');
  const uvx = fakeCli(() => ok('{"loop": null}'), ['testbench', 'x']);
  await uvx.cli.loop();
  assert.deepEqual(uvx.calls[0].args, ['x', 'loop', 'status', '--json']);
});

test('a name that is not a name testbench uses never reaches the command line', async () => {
  const { cli, calls } = fakeCli(() => ok(''));
  for (const bad of ['--approve', '-x', '', 'a b', 'a/b', '../x']) {
    await assert.rejects(cli.start(bad), BadName);
    await assert.rejects(cli.verify(bad), BadName);
  }
  assert.equal(calls.length, 0);
  assert.equal(checkName('smoke_2.v1:x', 'plan'), 'smoke_2.v1:x');
});

test('--approve is refused without a person\'s consent, in every spelling', async () => {
  const { cli, calls } = fakeCli(() => ok('', 3));
  for (const args of [['run', 'smoke', '--approve'], ['run', '--approve', '--detach', '--', 'smoke'], ['run', 'x', '--approve=1'],
    ['run', 'x', '--appr'], ['loop', 'start', 'x', '-ap'], ['estimate', 'x', '--APPROVE']]) {
    await rejects(cli.run(args), 'refused', /only right after a person clicked "Approve and run"/);
    await rejects(cli.run(args, { consent: { plan: 'smoke', worstCaseUsd: 1 } }), 'refused');   // a forged consent
  }
  assert.equal(calls.length, 0, 'nothing was started');
  const sneaky = fakeCli(() => ok('{}'), ['testbench', '--approve']);
  await rejects(sneaky.cli.plans(), 'bad-setting', /may not carry an approval/);
  assert.equal(sneaky.calls.length, 0, 'not even in agentTestbench.command');
});

test('--json output is parsed; anything else is an error in plain words', async () => {
  const replies: Record<string, ReturnType<typeof ok>> = {
    plans: ok('{"project": "p", "plans": []}'),
    runs: ok('not json'),
    spend: ok('', 2, 'testbench: No testbench.yaml here or in any parent of /x. Run `testbench check` ...'),
    loop: ok('{"loop": {"id": "a"}}', 3),
    old: ok('', 2, 'usage: testbench plans [-h]\ntestbench plans: error: unrecognized arguments: --json\n'),
    crash: ok('', 137, 'Killed'),
    fail: ok('it failed', 1),
  };
  let key = '';
  const { cli } = fakeCli(() => replies[key]);
  key = 'plans';
  assert.deepEqual(await cli.plans(), { project: 'p', plans: [] });
  key = 'runs';
  await rejects(cli.runs(5), 'bad-json', /did not print JSON: not json/);
  key = 'spend';
  await rejects(cli.spend(), 'refused', /^No testbench\.yaml here/);
  key = 'loop';
  assert.deepEqual(await cli.loop(), { loop: { id: 'a' } }, 'exit 3 while a try runs is still an answer');
  key = 'old';
  await rejects(cli.plans(), 'too-old', /no `testbench plans --json`. Upgrade agent-testbench/);
  key = 'crash';
  await rejects(cli.plans(), 'crashed', /exit code 137: Killed/);
  key = 'fail';
  await rejects(cli.json(['history', 'x', '--json']), 'failed', /it failed/);
});

test('a launch is "run ID started" with exit 3; a refusal is its message', async () => {
  let reply = ok('[testbench] run 20261112-091500-smoke started (Within budget: up to $0.00).\nFollow it with ...', 3);
  const { cli } = fakeCli(() => reply);
  assert.deepEqual(await cli.start('smoke'), {
    runId: '20261112-091500-smoke', text: 'run 20261112-091500-smoke started (Within budget: up to $0.00).',
  });
  reply = ok('Refused: $9.90 already committed today; plan smoke could add $0.44, over budget.max_day_usd $10.00.', 2);
  await rejects(cli.start('smoke'), 'refused', /^Refused: \$9\.90 already committed today/);
  reply = ok('[testbench] run --approve started', 3);
  await assert.rejects(cli.start('smoke'), BadName, 'a run id from the output is checked too');
});

test('a program that is not there says how to install it or set the command', async () => {
  const cli = new Testbench({ command: ['testbench-not-here'], cwd: os.tmpdir(), env: { PATH: os.tmpdir() } });
  await rejects(cli.plans(), 'not-found', /testbench-not-here was not found on PATH. Install agent-testbench .* agentTestbench.command/);
});

test('really started: arguments arrive as they are, with no shell to read them', async () => {
  const cli = new Testbench({ command: [process.execPath, '-e', 'console.log(JSON.stringify(process.argv.slice(1)))'], cwd: os.tmpdir() });
  const tricky = ['a b', '$(echo hi)', '`id`', '; rm -rf /', '"q"', '%PATH%', '*'];
  const r = await cli.run(tricky);
  assert.equal(r.code, 0);
  assert.deepEqual(JSON.parse(r.stdout), tricky);
});

test('really started: lines as they come, a time limit, and cancelling', async () => {
  const script = (code: string) => new Testbench({ command: [process.execPath, '-e', code], cwd: os.tmpdir() });
  const lines: string[] = [];
  const r = await script('console.log("one"); setTimeout(() => console.log("two\\r\\nthree"), 50)').run([], { onLine: (l) => lines.push(l) });
  assert.equal(r.code, 0);
  assert.deepEqual(lines, ['one', 'two', 'three']);
  await rejects(script('setTimeout(() => {}, 20000)').run([], { timeoutMs: 300 }), 'timeout');
  const stop = new AbortController();
  setTimeout(() => stop.abort(), 200);
  await rejects(script('setTimeout(() => {}, 20000)').run([], { signal: stop.signal }), 'cancelled');
  const exit = await script('process.exit(3)').run([]);
  assert.equal(outcomeOf(exit.code), 'running');
});

test('nodeExec turns a missing file into an error, not a hang', async () => {
  const missing = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tb-')), 'nope');
  await assert.rejects(nodeExec(missing, [], { cwd: os.tmpdir(), env: process.env }), (e: NodeJS.ErrnoException) => e.code === 'ENOENT');
});

test('money is shown as testbench shows it', () => {
  assert.deepEqual([0, 0.4367, 41.4168, 0.001, null, NaN].map(money), ['$0.00', '$0.44', '$41.42', '<$0.01', '?', '?']);
});
