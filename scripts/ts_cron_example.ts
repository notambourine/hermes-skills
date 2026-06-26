// Demo TypeScript cron body. Invoked via ts_cron_example.sh (NOT directly — Hermes
// runs non-.sh scripts through Python, which can't execute TypeScript).
//
// Whatever this prints to stdout is the no_agent job's output, delivered verbatim.
// Print nothing to stay silent. `process` is a Node global at runtime; the editor
// may flag it without @types/node, which doesn't affect strip-types execution.

const now = new Date().toISOString();
const uptimeMinutes = Math.round(process.uptime() / 60);

console.log(`hermes TS cron ok — ${now} (node uptime ${uptimeMinutes}m)`);
