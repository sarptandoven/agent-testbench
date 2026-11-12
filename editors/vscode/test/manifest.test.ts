/** package.json and the code agree: every command declared is registered and every one registered is declared, menus
 * name only those commands and views, and the extension type-checks. No VS Code is downloaded for this. */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { COMMANDS, VIEWS } from '../src/commands';

const ROOT = path.resolve(__dirname, '../..');
const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
const contributes = pkg.contributes;

test('every command is declared once, with a title, and registered', () => {
  const declared = (contributes.commands as { command: string; title: string; category: string }[]);
  assert.deepEqual(declared.map((c) => c.command).sort(), Object.values(COMMANDS).sort());
  assert.equal(new Set(declared.map((c) => c.command)).size, declared.length);
  assert.ok(declared.every((c) => c.title && c.category === 'Testbench'));
  const titles = declared.map((c) => `${c.category}: ${c.title}`);
  for (const wanted of ['Testbench: Check this project', 'Testbench: Run plan...', 'Testbench: Show latest report',
    'Testbench: Open dashboard', 'Testbench: Verify run']) {
    assert.ok(titles.includes(wanted), wanted);
  }
  const extension = fs.readFileSync(path.join(ROOT, 'src', 'extension.ts'), 'utf8');
  assert.match(extension, /const handlers: Record<CommandKey, Handler> = \{/, 'a handler for each command, or it does not compile');
  assert.match(extension, /for \(const key of Object\.keys\(COMMANDS\) as CommandKey\[\]\) \{\n\s+this\.disposables\.push\(vscode\.commands\.registerCommand\(COMMANDS\[key\]/);
  assert.equal([...extension.matchAll(/registerCommand\(/g)].length, 1, 'commands are registered in that one loop');
});

test('menus name declared commands and views; the views live in the Testbench container', () => {
  const commands = new Set(Object.values(COMMANDS));
  const views: string[] = (contributes.views.agentTestbench as { id: string }[]).map((v) => v.id);
  assert.deepEqual(views, Object.values(VIEWS));
  assert.deepEqual(contributes.viewsContainers.activitybar.map((c: { id: string }) => c.id), ['agentTestbench']);
  assert.ok(fs.existsSync(path.join(ROOT, contributes.viewsContainers.activitybar[0].icon)));
  for (const [where, items] of Object.entries(contributes.menus as Record<string, { command: string; when?: string }[]>)) {
    for (const item of items) {
      assert.ok(commands.has(item.command as never), `${where}: ${item.command}`);
      for (const view of item.when?.match(/view == ([\w.]+)/g) ?? []) {
        assert.ok(new Set<string>(Object.values(VIEWS)).has(view.slice(8)), `${where}: ${view}`);
      }
    }
  }
  const inline = contributes.menus['view/item/context'].filter((m: { group?: string }) => m.group === 'inline');
  assert.deepEqual(inline.map((m: { command: string }) => m.command), [COMMANDS.runPlan, COMMANDS.cancelRun]);
});

test('it starts in a testbench project, in trusted folders only, and needs nothing at run time', () => {
  assert.deepEqual(pkg.activationEvents, ['workspaceContains:testbench.yaml', 'workspaceContains:.testbench']);
  assert.equal(pkg.capabilities.untrustedWorkspaces.supported, false, 'checking a project runs its code');
  assert.equal(pkg.main, './out/src/extension.js');
  assert.ok(fs.existsSync(path.join(ROOT, pkg.main)), 'compiled (npm test compiles first)');
  assert.equal(pkg.dependencies, undefined, 'no runtime dependencies beyond the VS Code API');
  const engine = /^\^(\d+\.\d+\.\d+)$/.exec(pkg.engines.vscode);
  assert.ok(engine, pkg.engines.vscode);
  assert.equal(pkg.devDependencies['@types/vscode'], engine[1], 'the API types of the oldest VS Code it says it runs on');
  const command = contributes.configuration.properties['agentTestbench.command'];
  assert.equal(command.scope, 'machine', 'a workspace cannot choose the program the extension starts');
  assert.equal(command.default, 'testbench');
});

test('testbench.yaml gets the schema on GitHub until the installed one is read', () => {
  const [v] = contributes.yamlValidation;
  assert.equal(v.fileMatch, 'testbench.yaml');
  const m = /^https:\/\/raw\.githubusercontent\.com\/sarptandoven\/agent-testbench\/main\/(.+)$/.exec(v.url);
  assert.ok(m, v.url);
  assert.ok(fs.existsSync(path.resolve(ROOT, '..', '..', m[1])), `${m[1]} is in the repository`);
});

test('the extension type-checks (tsc --noEmit)', () => {
  const tsc = require.resolve('typescript/bin/tsc', { paths: [ROOT] });
  const r = spawnSync(process.execPath, [tsc, '-p', ROOT, '--noEmit'], { encoding: 'utf8' });
  assert.equal(r.status, 0, r.stdout + r.stderr);
});
