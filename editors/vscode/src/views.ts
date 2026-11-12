/** The Testbench views: tree items from tree.ts's nodes. A view reads the CLI only when VS Code asks for its items
 * (when it is visible), and again after refresh(). */
import * as vscode from 'vscode';

import { CliError } from './cli';
import { message, type Node } from './tree';

export class NodeView implements vscode.TreeDataProvider<Node> {
  private readonly changed = new vscode.EventEmitter<Node | undefined>();
  readonly onDidChangeTreeData = this.changed.event;
  private nodes: Promise<Node[]> | undefined;

  /** load: the view's nodes, read from the CLI; it may throw a CliError, shown as the view's only item. */
  constructor(private readonly load: () => Promise<Node[]>) {}

  refresh(): void {
    this.nodes = undefined;
    this.changed.fire(undefined);
  }

  getTreeItem(n: Node): vscode.TreeItem {
    const state = n.children
      ? (n.expanded ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.Collapsed)
      : vscode.TreeItemCollapsibleState.None;
    const item = new vscode.TreeItem(n.label, state);
    item.id = n.id;
    item.description = n.description;
    item.tooltip = n.tooltip;                  // plain text, never Markdown
    item.contextValue = n.contextValue;
    if (n.icon) {
      item.iconPath = new vscode.ThemeIcon(n.icon.id, n.icon.color ? new vscode.ThemeColor(n.icon.color) : undefined);
    }
    if (n.command) {
      item.command = n.command;
    }
    return item;
  }

  getChildren(n?: Node): Promise<Node[]> | Node[] {
    if (n) {
      return n.children ?? [];
    }
    this.nodes ??= Promise.resolve().then(this.load).catch((err: unknown) => [errorNode(err)]);
    return this.nodes;
  }
}

/** What went wrong, as the view's one item, in the CLI's words. */
export function errorNode(err: unknown): Node {
  const text = err instanceof Error ? err.message : String(err);
  const kind = err instanceof CliError ? err.kind : 'error';
  const label = kind === 'refused' && /No testbench\.yaml here/.test(text)
    ? 'No testbench.yaml here: Check this project runs what testbench finds, for free'
    : text.split('\n')[0];
  return message(`error:${kind}`, label, kind === 'not-found' || kind === 'too-old' ? 'warning' : 'error', text);
}
