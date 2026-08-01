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
    // These two are not judgement calls — dead code is dead, and an ARIA role
    // missing its required attributes is a bug for anyone using a screen
    // reader — so they are errors rather than warnings.
    //
    // The gate itself is now `--max-warnings 0` (#1203): every remaining
    // `react-hooks/exhaustive-deps` and `no-img-element` site was triaged
    // individually and carries a per-line disable stating why the omission is
    // correct. A warning can no longer accumulate unread, and because
    // `reportUnusedDisableDirectives` is an error below, a directive that stops
    // being needed fails the build rather than lingering.
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
