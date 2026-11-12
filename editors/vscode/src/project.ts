import * as fs from 'node:fs';
import * as path from 'node:path';

/** The workspace folder testbench works in: the first with a testbench.yaml, else the first with a .testbench folder
 * (`testbench check` in a folder with no testbench.yaml keeps what it found there), else undefined. */
export function findProjectRoot(folders: readonly string[], exists: (p: string) => boolean = fs.existsSync): string | undefined {
  return folders.find((f) => exists(path.join(f, 'testbench.yaml'))) ?? folders.find((f) => exists(path.join(f, '.testbench')));
}
