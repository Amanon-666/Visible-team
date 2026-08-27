import { readFile } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";
import { transform } from "lightningcss";
import { defineConfig } from "tsdown";

// The bundle id is also the ownership key used by DSH's style inventory.
const MODULE_ID = "dsh-visible-team";
const clientExternals = [
  "react",
  "react/jsx-runtime",
  "react/jsx-dev-runtime",
  "react-dom",
  "react-dom/client",
  "@deepseek-ai/dsh-client-ui-primitives",
];

const CSS_VIRTUAL_PREFIX = "\0dsh-css:";
const CSS_VIRTUAL_SUFFIX = ".mjs";

/**
 * Emit a style tag from inside the classic module-loader factory.
 *
 * This follows the MIT-licensed dsh-market tsdown virtual CSS module pattern
 * (https://github.com/dsh-market/dsh-market/blob/main/tsdown.config.ts), with
 * an explicit disposer because DSH 0.1.1-rc.1's loader unload is not yet a
 * complete fiber teardown path. The tag remains plugin-owned for the loader's
 * style inventory and the effect disposer removes it on plugin teardown.
 */
function styleInjectionModule(fileId: string, source: string, classMap: Record<string, string>): string {
  const tagId = `${MODULE_ID}/${basename(fileId)}`;
  const styleSelector = `style[data-plugin=${JSON.stringify(MODULE_ID)}][data-plugin-css=${JSON.stringify(tagId)}]`;
  return [
    `const css = ${JSON.stringify(source)};`,
    `const tagId = ${JSON.stringify(tagId)};`,
    `const styleSelector = ${JSON.stringify(styleSelector)};`,
    "let styleTag = typeof document === \"undefined\" ? null : document.querySelector(styleSelector);",
    "if (styleTag === null && typeof document !== \"undefined\") {",
    "  const tag = document.createElement(\"style\");",
    `  tag.dataset.plugin = ${JSON.stringify(MODULE_ID)};`,
    "  tag.dataset.pluginCss = tagId;",
    "  tag.textContent = css;",
    "  document.head.appendChild(tag);",
    "  styleTag = tag;",
    "}",
    "export function dispose() { styleTag?.remove(); styleTag = null; }",
    `export default ${JSON.stringify(classMap)};`,
  ].join("\n");
}

const cssModulesPlugin = {
  name: "dsh-visible-team-css-modules",
  resolveId(source: string, importer?: string) {
    if (!source.endsWith(".module.css")) return null;
    const fileId = importer === undefined ? resolve(source) : resolve(dirname(importer), source);
    return `${CSS_VIRTUAL_PREFIX}${fileId}${CSS_VIRTUAL_SUFFIX}`;
  },
  async load(virtualId: string) {
    if (!virtualId.startsWith(CSS_VIRTUAL_PREFIX)) return null;
    const fileId = virtualId.slice(CSS_VIRTUAL_PREFIX.length, -CSS_VIRTUAL_SUFFIX.length);
    this.addWatchFile(fileId);
    const source = await readFile(fileId);
    const transformed = transform({
      filename: fileId,
      code: source,
      cssModules: { pattern: "[hash]_[local]" },
      minify: true,
      // Match the desktop-compatible browser floor used by dsh-market. The
      // explicit targets keep lightningcss from dropping compatibility forms
      // while still producing a compact CSS module for the desktop shell.
      targets: { chrome: 90 << 16, firefox: 100 << 16, safari: 13 << 16, edge: 90 << 16 },
    });
    const classMap: Record<string, string> = {};
    for (const [local, exported] of Object.entries(transformed.exports ?? {}).sort(([left], [right]) => (
      left < right ? -1 : left > right ? 1 : 0
    ))) {
      classMap[local] = (exported as { name: string }).name;
    }
    return styleInjectionModule(fileId, transformed.code.toString(), classMap);
  },
};

export default defineConfig({
  entry: { client: "src/client/entry.tsx" },
  format: "cjs",
  outDir: "lib",
  platform: "browser",
  target: "es2022",
  sourcemap: false,
  clean: false,
  deps: {
    neverBundle: clientExternals,
    alwaysBundle(id) {
      return !clientExternals.includes(id);
    },
  },
  plugins: [cssModulesPlugin],
  outputOptions: {
    entryFileNames: "client.js",
    banner: `window.__ModuleLoader__.load({ id: ${JSON.stringify(MODULE_ID)}, factory: (require) => {`,
    intro: "var module = { exports: {} }; var exports = module.exports;",
    footer: "return module.exports; } });",
    codeSplitting: false,
  },
});
