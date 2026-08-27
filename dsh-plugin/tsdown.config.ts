import { defineConfig } from "tsdown";

const clientExternals = [
  "react",
  "react/jsx-runtime",
  "react/jsx-dev-runtime",
  "react-dom",
  "react-dom/client",
];

export default defineConfig({
  entry: { client: "src/client/entry.tsx" },
  format: "cjs",
  outDir: "lib",
  platform: "browser",
  sourcemap: false,
  clean: false,
  deps: {
    neverBundle: clientExternals,
    alwaysBundle(id) {
      return !clientExternals.includes(id);
    },
  },
  outputOptions: {
    entryFileNames: "client.js",
    banner: 'window.__ModuleLoader__.load({ id: "dsh-visible-team", factory: (require) => {',
    intro: "var module = { exports: {} }; var exports = module.exports;",
    footer: "return module.exports; } });",
    codeSplitting: false,
  },
});
