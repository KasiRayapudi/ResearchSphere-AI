/**
 * Build with a type-check gate.
 *
 * The npm script used to be `tsc ; vite build`, where the `;` meant type
 * errors never failed the build. Chaining with `&&` is not portable either:
 * npm on Windows may be configured to use PowerShell 5.1, which has no `&&`.
 * Running both steps from node works the same everywhere.
 */
import { spawnSync } from 'node:child_process';

function run(command, args) {
  const result = spawnSync(command, args, { stdio: 'inherit', shell: true });
  if (result.status !== 0) {
    console.error(`\n[build] ${command} ${args.join(' ')} failed.`);
    process.exit(result.status ?? 1);
  }
}

run('tsc', ['--noEmit']);
run('vite', ['build']);
