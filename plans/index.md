# Plans index

Flat summary of every plan. The folder a plan lives in (`active/` or `completed/`) is the source of truth for status — don't repeat it here. Numbers are stable across moves.

To locate a plan by number: `fd "^001" plans/`.

Tech debt tracked separately in [tech-debt-tracker.md](tech-debt-tracker.md).

## Plans

- **001-v1-implementation.md** — CLI tool that transcribes `.m4a` voice memos from iCloud Drive via Groq Whisper into a single markdown file per run. Includes incremental state tracking, `--all` / `--list-runs` / `--regenerate` modes, and a real-API smoke test with fixtures.
- **002-output-path-obsidian.md** — Default run output to the Obsidian vault's `Voice/` folder, prefix filenames with `vm-` to avoid colliding with daily notes, and add `-o / --output-dir` flag to override per invocation.
