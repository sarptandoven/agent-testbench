/** Every command the extension contributes. package.json declares the same ids (a test keeps them equal), and
 * extension.ts registers a handler for each: its handler table is typed by this object, so a missing one does not
 * compile. */
export const COMMANDS = {
  check: 'agentTestbench.check',
  runPlan: 'agentTestbench.runPlan',
  showLatestReport: 'agentTestbench.showLatestReport',
  openReport: 'agentTestbench.openReport',
  openDashboard: 'agentTestbench.openDashboard',
  verifyRun: 'agentTestbench.verifyRun',
  cancelRun: 'agentTestbench.cancelRun',
  showSpend: 'agentTestbench.showSpend',
  refresh: 'agentTestbench.refresh',
} as const;

export type CommandKey = keyof typeof COMMANDS;

export const VIEWS = {
  plans: 'agentTestbench.plans',
  runs: 'agentTestbench.runs',
  loop: 'agentTestbench.loop',
} as const;
