# CHANGES — ALL WORK ON THIS DIRECTORY

`Downloads/agent-hermes` — fork workspace for **imanujela/hermes-agent** (upstream NousResearch/hermes-agent @ `0670ba45`).
Complete record, from the git objects — not from memory. Generated 2026-10-09.

**Totals vs upstream base: 819 files changed, 9052 insertions(+), 8316 deletions(-)** across 9 commits (round set 1 = RC2-era prep in sibling dirs; see `hermes-agent-upstream`, `hermes-agent-rc2` — this dir holds RC3-final).

## Commit ledger (oldest first)

| sha | date | files | +/- | what |
|---|---|---|---|---|
| `2b7632c6` | 2026-10-09 | 22 | +50/−43 | RC3: Hermes Agent upgrade — error handling, performance, docs, stability |
| `e4ad1dd2` | 2026-10-09 | 456 | +1558/−1471 | RC3: full codebase upgrade — 456 files, error handling + performance + stability |
| `ad741d02` | 2026-10-09 | 159 | +593/−297 | fix: add logging to 159 files with silent except Exception:pass (hard violation) |
| `1020e19c` | 2026-10-09 | 5 | +309/−0 | feat(skills): codebase-upgrade-sweep — repo-wide upgrade discipline with ratchet gate |
| `c993eae0` | 2026-10-09 | 303 | +957/−788 | RC3-fix: P0/P1 timeout regressions + 561 dead as-exc bindings (246 files) |
| `73c5cd59` | 2026-10-09 | 545 | +6896/−5757 | RC3: security wave + infrastructure switch (538 files) |
| `44b8688f` | 2026-10-09 | 1 | +28/−0 | docs: TICKET-RC3 — round set 2 acceptance ticket with RISKY-findings list |
| `1c15220c` | 2026-10-09 | 406 | +1307/−2637 | refactor: collapse 853 pure swallow-and-log blocks into the suppressed() seam (404 files) |
| `cae3ab9c` | 2026-10-09 | 1 | +33/−2 | fix(security): dashboard fs guard now mirrors canonical file_safety (audit HIGH #3) |

## Narrative by wave

**W1 — RC3 base (`2b7632c6`, `e4ad1dd2`, `ad741d02`).** Repo-wide error-handling honesty per CONTRIBUTING.md: `except Exception: pass` → `logger.debug('Suppressed exception: …', exc_info=True)`; hot-path `re.compile` hoisting; `@lru_cache` on pure helpers; module loggers added where missing (159 files). Docs: SOUL.md rewrite, README tightening, launcher exit-code propagation, setup hardening, version bump. New in-repo skill `codebase-upgrade-sweep` + 9 tests + scoped docs regen (`1020e19c`).

**W2 — Regression cleanup (`c993eae0`).** Found by the 4-angle simplify-code review + the repo's own health ratchet, fixed: gitlock duplicate-timeout `TypeError` (partial-clone refetch crash), blanket 60s on interactive editor/TUI-launch/installs (session killers), `TimeoutExpired` uncaught in except tuples, 561 dead `as exc` bindings removed by AST pass (246 files).

**W3 — Security + infrastructure (`73c5cd59`, 538 files).** Security wave (10 auditors): git `ext::` transport + leading-dash option **injection closed** in 3 plugin-install paths; 4 fresh duplicate-timeout TypeErrors removed in tui_gateway; timing-unsafe webhook compare → `hmac.compare_digest` (BlueBubbles); trace_upload remote-path traversal sanitized via canonical component helper; migration manifest 0600; 3 redaction bypasses → `RedactingFormatter`; CI/release network timeouts; tar-mode clamp. Infrastructure: `hermes_suppress.py` leaf — the single `suppressed()` seam; `hermes_logging` re-exports it; `hermes_cli/_timeouts.py` + `tools/_limits.py` named timeout classes; `cli.py` hand-rolled double-checked lock → `@lru_cache(1)`; F821 NameError sites reverted to documented `pass`; 6 line-capped files netted under frozen caps; TDD safety-net tests.

**W4 — The collapse (`1c15220c`, 404 files).** AST codemod folded 853 pure swallow-and-log blocks into `with suppressed(logger, msg):` — semantics verified identical (with-body ≡ try-body control flow; handler ≡ helper log; bare-except and typed handlers deliberately untouched; 13 splice-resistant files hand-scheduled). compileall green.

**W5 — Dashboard auth fix (`cae3ab9c`).** HIGH finding #3 closed: `_is_sensitive_path` now genuinely mirrors the canonical `agent.file_safety` guards (`get_read_block_error` + `HOME_CREDENTIAL_DIRS` trees + loose-home credential basenames) — unlocked dashboard deployments can no longer serve `~/.ssh`, `.aws`, `.kube`, `.docker`, `.config/gh`, `.netrc/.npmrc/.pgpass`. Guard suite: 28 passed.

## Outstanding (in-flight this hour, deleg_1a25435e)

fs-mirror regression test · hand-route 13 skipped files · message-variant convergence (tui/plugins/cron/skills/scripts/evals) · seam `*fmt_args` extension (+84 percent-formatted sites) · RedactingFormatter on 2 debug handlers · install.sh tmp hardening · eval grep de-shelling · caps re-verify · git-guard + webhook TDD tests · CONTRIBUTING seam section.

## Awaiting owner decision (RISKY — auth semantics)

1. **Finding #1:** slash-admin fail-open (`/goal gate add` → `shell=True` by any chat-permitted remote user). Evidence test lands this wave; flip = fail-closed default + operator bootstrap path, breaks the documented opt-in model until configured.
2. **Finding #2:** `config.yaml` agent-writable while `key_cmd`/`quick_commands.exec` execute with shell + credential env (config→exec persistence). Fix = content-aware approval on shell-bearing key changes; touches deliberate #45947 decision.
3. Findings #4–#7 (mcp_catalog bootstrap `shell=True`, feishu fail-open webhook, global `ssl_verify:false` scope, dashboard `?token=` URL) — diffs drafted in TICKET-RC3 review stream.

## Verification ledger

- `python -m compileall -q .` → exit 0 (after W4 and W5).
- Tests green at last full run: fs suite 28p/1s · timeouts ratchet 3p · error-path smoke 35p/6s · skill tests 9p · seam batch 43p/10s (uv, PYTHONPATH cleared).
- Health ratchet (`scripts/check --only health`): re-run in flight by hermes-agent skill child; verdict lands with batch.
