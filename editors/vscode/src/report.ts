/** A run's HTML report (`testbench report --format html`) made fit for a webview: a strict Content-Security-Policy, the
 * report's own stylesheet allowed by a nonce, no script but the extension's own (which only asks to open an evidence
 * file), and evidence files opened only from inside that run's folder. */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { randomBytes } from 'node:crypto';

export function nonce(): string {
  return randomBytes(18).toString('base64').replace(/[^A-Za-z0-9]/g, '');
}

/** Nothing loads but images from the run's folder (webview resources) or inline data, the page's own stylesheet and
 * the extension's own script; no frames, forms, fonts or connections; links cannot reset the base URL. */
export function csp(cspSource: string, n: string): string {
  return [
    "default-src 'none'",
    `img-src ${cspSource} data:`,
    `style-src 'nonce-${n}'`,
    `script-src 'nonce-${n}'`,
    "base-uri 'none'",
    "form-action 'none'",
  ].join('; ');
}

/** Asks the extension to open an evidence file; the extension checks it is inside the run's folder. */
function clickScript(n: string): string {
  return `<script nonce="${n}">(() => {
  const vscode = acquireVsCodeApi();
  document.addEventListener('click', (e) => {
    const a = e.target instanceof Element ? e.target.closest('a[data-evidence]') : null;
    if (a) { e.preventDefault(); vscode.postMessage({ type: 'open', path: a.getAttribute('data-evidence') }); }
  });
})();</script>`;
}

const SCHEME = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;

/** The report page with the policy in its head, a nonce on its stylesheet, any script, base, meta refresh or frame of
 * its own removed, and its links to evidence files (relative to the run folder) turned into requests to open them. */
export function prepareReport(html: string, cspSource: string, n: string): string {
  let out = html
    .replace(/<script\b[\s\S]*?<\/script\s*>/gi, '')
    .replace(/<(script|base|iframe|frame|object|embed)\b[^>]*>/gi, '')
    .replace(/<meta\b[^>]*http-equiv[^>]*>/gi, '')
    .replace(/<style\b[^>]*>/gi, `<style nonce="${n}">`)
    .replace(/<a\b([^>]*?)\bhref\s*=\s*(['"])(.*?)\2/gi, (_m, before: string, q: string, href: string) => {
      if (/^https?:/i.test(href) || href.startsWith('#')) {
        return `<a${before}href=${q}${href}${q}`;
      }
      if (SCHEME.test(href) || href.startsWith('/') || href.startsWith('\\')) {
        return `<a${before}`;                                     // any other scheme or an absolute path: no link
      }
      return `<a${before}href="#" data-evidence=${q}${href}${q}`;
    });
  const meta = `<meta http-equiv="Content-Security-Policy" content="${csp(cspSource, n)}">`;
  out = /<head\b[^>]*>/i.test(out) ? out.replace(/<head\b[^>]*>/i, (h) => `${h}${meta}`) : `${meta}${out}`;
  const script = clickScript(n);
  return /<\/body\s*>/i.test(out) ? out.replace(/<\/body\s*>/i, `${script}</body>`) : `${out}${script}`;
}

function escape(text: string): string {
  return text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

/** A page for a run that has no report yet (still going, or it never started). */
export function placeholder(cspSource: string, n: string, title: string, text: string): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp(cspSource, n)}">`
    + `<title>${escape(title)}</title><style nonce="${n}">body{font-family:var(--vscode-font-family);padding:1rem 2rem}`
    + `pre{white-space:pre-wrap}</style></head><body><h2>${escape(title)}</h2><pre>${escape(text)}</pre></body></html>`;
}

/** The file an evidence link names, if it is a file inside the run's folder (after following links); else undefined.
 * The page is the CLI's, but a report can name anything a run wrote, so the path is checked here again. */
export function evidencePath(runDir: string, rel: unknown, realpath: (p: string) => string = fs.realpathSync,
  isFile: (p: string) => boolean = (p) => fs.statSync(p).isFile()): string | undefined {
  if (typeof rel !== 'string' || !rel || rel.includes('\0') || path.isAbsolute(rel) || SCHEME.test(rel)) {
    return undefined;
  }
  try {
    const top = realpath(runDir);
    const real = realpath(path.resolve(top, rel));
    const inside = path.relative(top, real);
    if (!inside || inside.startsWith('..') || path.isAbsolute(inside) || !isFile(real)) {
      return undefined;
    }
    return real;
  } catch {
    return undefined;
  }
}

/** The run's folder from `testbench runs --json`, if it is where the project keeps runs: <root>/.testbench/runs/<id>. */
export function runFolder(root: string, runId: string, dir: string | undefined, realpath: (p: string) => string = fs.realpathSync): string | undefined {
  try {
    const want = realpath(path.join(root, '.testbench', 'runs', runId));
    return dir === undefined || realpath(dir) === want ? want : undefined;
  } catch {
    return undefined;
  }
}
