import { defineConfig, globalIgnores } from 'eslint/config'
import nextVitals from 'eslint-config-next/core-web-vitals'
import nextTs from 'eslint-config-next/typescript'

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([
    '.next/**',
    'out/**',
    'build/**',
    'next-env.d.ts',
  ]),
  {
    // Next's presets set these to `warn`, and `eslint src/` exits 0 on
    // warnings — so CI never saw them. A dead import sat in nav-bar.tsx until
    // CodeQL reported it, which is a slow and expensive way to learn something
    // the linter already knew.
    //
    // Promoted here rather than by flipping the whole gate to
    // `--max-warnings 0`: the remaining warnings are almost all
    // `react-hooks/exhaustive-deps`, which are genuine judgement calls and need
    // triaging one at a time (#1203). These two are not judgement calls — dead
    // code is dead, and an ARIA role missing its required attributes is a bug
    // for anyone using a screen reader.
    rules: {
      '@typescript-eslint/no-unused-vars': 'error',
      'jsx-a11y/role-has-required-aria-props': 'error',
    },
  },
  {
    // A disable directive that no longer suppresses anything is a comment
    // claiming a problem exists where none does.
    linterOptions: { reportUnusedDisableDirectives: 'error' },
  },
])

export default eslintConfig
