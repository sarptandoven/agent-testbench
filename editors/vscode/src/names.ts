/** Plan and run names as the extension passes them to testbench: the shape testbench gives them, never an option (a
 * plan named like the approval flag in a testbench.yaml must not approve anything). The MCP server checks names the same way. */
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,80}$/;

export class BadName extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'BadName';
  }
}

export function checkName(value: string, what: 'plan' | 'run'): string {
  if (typeof value !== 'string' || !NAME.test(value)) {
    throw new BadName(`${what} ${JSON.stringify(value)} is not a name testbench uses (letters, digits, - _ . : and no leading -)`);
  }
  return value;
}

/** Dollars as testbench prints them: two decimals, and a cost below a cent is not shown as free. */
export function money(usd: number | null | undefined): string {
  if (typeof usd !== 'number' || !Number.isFinite(usd)) {
    return '?';
  }
  if (usd > 0 && usd < 0.005) {
    return '<$0.01';
  }
  return `$${usd.toFixed(2)}`;
}
