const { spawnSync } = require('child_process');

const args = process.argv.slice(2);
const configured = process.env.AUTOCODEX_PYTHON;
const command = configured || (process.platform === 'win32' ? 'py' : 'python3');
const prefix = !configured && process.platform === 'win32' ? ['-3'] : [];
const result = spawnSync(command, [...prefix, ...args], { stdio: 'inherit' });

if (result.error) {
  console.error(`Unable to run Python: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
