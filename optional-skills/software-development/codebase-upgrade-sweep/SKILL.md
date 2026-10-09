---
name: codebase-upgrade-sweep
description: "Repo-wide upgrade sweeps gated by the repo's lint ratchet."
version: 0.1.0
author: imanujela, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [upgrades, refactoring, delegation, code-review, ratchet]
    related_skills: [simplify-code, requesting-code-review, test-driven-development]
---

# Codebase Upgrade Sweep

Mechanical, high-volume upgrades across a whole repository (error handling,
performance, docs) run as parallel delegated sweeps, then gated by the
project's own lint/health CI — not by vibes. A sweep that makes the repo's
`scripts/check` (or equivalent) redder is a regression, however many files it
"improved."

## When to Use

- User asks to upgrade/enhance an entire codebase or fork, not one module.
- A mechanical pattern (silent swallows, per-call `re.compile`, missing
  timeouts) repeats across hundreds of files.
- The fork must pass the upstream's CI standards before push.
- Don't use for: single-file features (`/tdd`), small PRs (`/code-review`),
  or architecture *decisions* (that's `/grill-with-docs`).

## Prerequisites

- A local clone of the target repo + `git` available in `terminal`.
- The repo's standards file (CONTRIBUTING.md / AGENTS.md / CODING_STANDARDS.md)
  read before editing — it defines the bar.
- The repo's own health/lint gate command, discovered from its CI config
  (e.g. hermes-agent: `python scripts/check --only health`).

## Procedure

1. **Read the standards first.** Extract the exact rules (exception handling,
   logging level, comment policy, file-size caps) and paste them verbatim
   into every sub-agent dispatch. Every dispatch, not just the first.
   Done when each goal string contains the rule text.
2. **Discover the ratchet baseline.** Run the repo's health check on the
   *pristine* checkout; record the violation count. Done when you know what
   "no new violations" means for this repo.
3. **Slice by directory, not by file count.** One sub-agent per coherent tree
   (root modules, `agent/`, `tools/`, `cli/`, …). Cap ~4 concurrent children.
   Every dispatch states: edit in place, never read whole large files
   (`search_files` + `read_file(offset, limit)`), patch + `py_compile`,
   report every file touched. Done when all slices dispatched.
4. **Freeze the tree during sweeps.** Do not hand-edit while children run —
   they share the working tree. Stage/commit only paths you own.
5. **Run two review axes after the sweeps land:** `/code-review` (Standards +
   Spec vs the fork's fixed point) and `/simplify-code` (reuse, quality,
   efficiency, altitude). Done when both reports are in hand.
6. **Re-run the health check; diff against the baseline.** Every new blocking
   violation is yours to fix before commit. Fix mechanically where provably
   safe (dead bindings), surgically where not (crash regressions).
7. **Commit, then push the fork.** Done when the health check exits clean or
   only with baseline debt.

## Quick reference

```bash
python scripts/check --only health          # hermes-agent ratchet
python -c "import py_compile; py_compile.compile('f.py', doraise=True)"
git diff <fixed-point>...HEAD --stat        # review scope
```

## Pitfalls

- **Module-level `logger` order.** Replacing `except Exception: pass` with
  `logger.debug(...)` at module scope NameError-crashes if the `except` can
  fire before `logger = logging.getLogger(__name__)` is reached. Keep `pass`
  with a comment at those sites. The repo's ratchet flags these as F821.
- **Never log inside guaranteed-exit functions.** A function whose job is
  `logging.shutdown(); os._exit()` must keep silent `pass` on shutdown
  failure — `logger.debug` after handlers are closed can raise and skip the
  exit entirely.
- **Timeout collision with `**kwargs`.** Setting `timeout=60` on a call
  that also spreads `**kwargs` containing `timeout=900` silently uses the
  wrong value or raises `TypeError`. Check dict-spreads before adding
  explicit kwargs.
- **Blanket timeouts mis-size the call.** One constant (60s) applied to
  interactive editors, `npm run build`, large `git fetch --refetch`, and
  sub-second probes breaks all four differently. Tier by call type;
  `TimeoutExpired` is not an `OSError` — add it to except tuples.
- **Codemod shadow bugs.** Mechanical sweeps cause silent regressions:
  dead `as exc` bindings (F841), stripped `# noqa` / `# type: ignore`
  pragmas, and local var shadowing of outer names. Diff for pragma loss
  and re-run the linter after every batch.
- **File-line caps freeze.** Over-target files may only shrink; adding an
  import + logger line to one breaks the ratchet. Offset growth or leave
  silent sites untouched there.
- **`asyncio.gather` over sequential awaits changes failure semantics** —
  siblings keep running after the first raises. Verify independence
  (id-multiplexed protocols qualify; ordered setup does not).
- **Sub-agents reading whole 2,000-line files burn their budget before
  editing anything.** Forbid it in the dispatch; require
  `search_files`-first, and small slices (3-10 files).
- **Reviewers report, you decide.** Their findings are self-reports; verify
  before applying, and drop findings without `file:line` evidence.
- **Shallow-clone push failure.** Pushing from a `--depth=1` clone fails
  with `index-pack` errors. Run `git fetch --unshallow` before push to
  materialize full history.
- **The ratchet is the only honest report.** Sub-agent self-reports and
  `py_compile` are necessary but not sufficient. The repo's health/lint
  check is the sole authority on whether the sweep improved or regressed.

## Verification

- The repo's health check exits with zero *new* blocking violations vs the
  pristine baseline.
- Every edited file passes `py_compile` (or the language's syntax gate).
- `git diff --stat <fixed-point>...HEAD` lists every touched file — the
  final report matches it exactly, from the diff, not from memory.
- Sample ≥30 edited files across directories and re-check them yourself.
