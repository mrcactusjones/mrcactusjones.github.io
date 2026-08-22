#!/usr/bin/env bash
# One day's work: spend the credit budget, re-rank, snapshot.
#
# Schedule with crontab -e, 9am daily:
#   0 9 * * * /full/path/to/gap-tracker/scripts/run-daily.sh
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 run.py daily --provider ppt --log
