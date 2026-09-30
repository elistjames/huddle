import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "eslint.config.js"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.strictTypeChecked],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    plugins: { "react-hooks": reactHooks, "react-refresh": reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["error", { allowConstantExport: true }],
      "@typescript-eslint/restrict-template-expressions": ["error", { allowNumber: true }],
      // Rendering untrusted chat text as HTML is forbidden (Deep Dive §8: plain text and code only).
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: "Messages render as plain text and code blocks only; no raw HTML.",
        },
      ],
      "no-restricted-properties": [
        "error",
        { property: "innerHTML", message: "No raw HTML (Deep Dive §8)." },
      ],
    },
  },
);
