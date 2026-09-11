import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'node_modules'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': 'off',
      // `any` is used deliberately at a few API boundaries where the backend
      // response is not yet typed end to end; those are annotated in place.
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // Empty catch blocks are used intentionally where a failure must not
      // break the surrounding flow (clipboard, localStorage in private mode).
      'no-empty': ['error', { allowEmptyCatch: true }],
      // Downgraded, not silenced. This rule effectively forbids effect-based
      // data fetching, which is how every page in this app loads data via the
      // shared useAsyncData hook. Satisfying it properly means adopting
      // Suspense or a query library (TanStack Query) - a real architectural
      // change that is tracked as follow-up work rather than rushed here.
      // Kept visible as a warning so the debt stays on the report.
      'react-hooks/set-state-in-effect': 'warn',
    },
  }
);
