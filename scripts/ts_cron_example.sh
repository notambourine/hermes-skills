#!/usr/bin/env bash
# Bash wrapper for a TypeScript cron body.
#
# Hermes runs .sh/.bash scripts via bash and EVERY other extension via Python, so a
# .ts file pointed at directly would be handed to Python and fail. Point the cron
# job's `script` at THIS wrapper, never at the .ts. The image ships Node 22.
#
# Node 22.6+ can run TypeScript directly via --experimental-strip-types (no tsx, no
# install, no network). If your Node is older, swap the exec line for:
#     exec npx --yes tsx "$here/ts_cron_example.ts"
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec node --experimental-strip-types "$here/ts_cron_example.ts"
