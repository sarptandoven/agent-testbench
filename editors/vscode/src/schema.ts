/** Autocomplete for testbench.yaml from the installed testbench: `testbench schema` (which adds the installed check
 * plug-ins' descriptions) is kept in the extension's storage and handed to the YAML extension (redhat.vscode-yaml) for
 * every testbench.yaml, so it works offline and matches the version installed. Without it, package.json's
 * yamlValidation points the YAML extension at the schema on GitHub. A `# yaml-language-server: $schema=...` first
 * line (which `testbench init` writes) takes precedence over both. */

export const SCHEME = 'agent-testbench';
export const SCHEMA_URI = `${SCHEME}://schema/testbench.schema.json`;
export const YAML_EXTENSION = 'redhat.vscode-yaml';

/** The part of the YAML extension's API this uses. */
export interface YamlApi {
  registerContributor(
    schema: string,
    requestSchema: (resource: string) => string | undefined,
    requestSchemaContent: (uri: string) => string | undefined,
    label?: string,
  ): boolean;
}

/** A testbench.yaml anywhere: the last segment of the document's URI. */
export function isTestbenchYaml(resource: string): boolean {
  const last = resource.split(/[\\/]/).pop() ?? '';
  try {
    return decodeURIComponent(last) === 'testbench.yaml';
  } catch {
    return last === 'testbench.yaml';
  }
}

/** Registers the schema with the YAML extension; content() is the schema text kept from `testbench schema`, undefined
 * until it is there (then the YAML extension falls back to package.json's yamlValidation). */
export function contributeSchema(api: YamlApi, content: () => string | undefined): boolean {
  return api.registerContributor(
    SCHEME,
    (resource) => (isTestbenchYaml(resource) && content() !== undefined ? SCHEMA_URI : undefined),
    (uri) => (uri === SCHEMA_URI ? content() : undefined),
    'agent-testbench',
  );
}
