# TICKET — RC3 round set 2 (imanujela/hermes-agent)

Status: **ready** · Branch: `main` @ HEAD (20 commits vs base `0670ba45`) · Fork of NousResearch/hermes-agent

## Goal
Upgrade the fork end-to-end: error-handling honesty, performance, security hardening, architecture deepening — gated by upstream's own `scripts/check --only health` ratchet, not by self-report.

## Done (20 commits, base `0670ba45` → HEAD)

### Error honesty
- [x] 1,300+ `except Exception: pass` → `with suppressed(logger, msg):` through one stdlib-only seam (`hermes_suppress.py`, re-exported by `hermes_logging`)
- [x] 853 pure swallow-and-log blocks collapsed into `suppressed()` across 404 files (AST codemod, compileall green)
- [x] 561 dead `as exc` bindings removed (F841 AST pass, 246 files)
- [x] 159 hard-violation files given module-level loggers (297 sites)
- [x] 6 F821/F823 ratchet blockers closed (2 import-order, 2 TYPE_CHECKING for lazy rich, 1 re-hoisted regex, 1 real `UnboundLocalError` in `auth_commands.py` — renamed `suppressed` → `suppressed_sources`)
- [x] 23 rationale comments + 9 `# pragma: no cover` markers restored from seam routing diff

### P0/P1 regressions (caught by simplify-code reviewers + ratchet)
- [x] `gitlock.py` `timeout=60` colliding with `**fetch_kwargs(timeout=900)` → `TypeError` on every partial-clone refetch
- [x] Blanket 60s on interactive editor, TUI launch, `codesign --deep`, `ditto`, apt/brew installs
- [x] `TimeoutExpired` missing from except tuples where timeouts landed
- [x] **P0: CLI entry point crashing on boot** — seam import never landed in `hermes_cli/main.py` (24 F821 undefined names including `main()`); `import hermes_cli.main` in a deps-complete env proves boot

### Security wave (10-child audit, risk-ladder)
- [x] Git `ext::` transport + leading-dash option injection closed ×3 (`plugins_cmd_git.py`, `plugins_updates.py`, `profile_distribution.py`)
- [x] `hmac.compare_digest` on webhook auth (`webhook.py`, `pairing.py`)
- [x] `trace_upload.py` path sanitization (`_safe_session_filename_component`)
- [x] 3 redaction bypasses closed (plain `Formatter` on debug/scratch sinks → `RedactingFormatter`)
- [x] Dashboard fs credential exposure fixed (`_is_sensitive_path` mirrors canonical `agent.file_safety.get_read_block_error` + `HOME_CREDENTIAL_DIRS`; 28 route tests pass)
- [x] Manifest 0o600, network timeouts ×5, tar-mode clamp, 4 tui_gateway duplicate-timeout TypeErrors
- [x] Goal injection + `key_cmd` shell hardening (`hermes_cli/main.py`, `config.py`)

### Architecture (improve-codebase-architecture report → TDD → landed)
- [x] **#1 Strong:** `hermes_cli/git_source_safety.py` — one deep module owns "is this source safe to hand to git?" (`reject_reason` + `canonical_git_url`); 3 inline copies collapsed to callers; 8 hostile/8 legit test cases red→green
- [x] **#4 Worth exploring:** `hermes_tiers.py` — one timeout-tier taxonomy (`FAST/PROCESS/NETWORK/BUILD/INSTALL`); `hermes_cli/_timeouts.py` + `tools/_limits.py` became thin re-exports; zero caller changes; rules-of-the-road docstring defined once

### Performance
- [x] 6 static `re.compile` hoisted to module constants (`hermes_state_search.py` ×5, `mcp_config.py` ×1); 4 dynamic patterns left inline (already memoized)
- [x] `@lru_cache` on `_canonical_model_variants`; return list → tuple on hot paths
- [x] Skill CLI bootstrap: `skills/_lib/skill_cli.py` — `setup_cli()` absorbing duplicated stdio/locale preamble from 17 pdf/pptx scripts (68 ins / 69 del); 16 files no longer need repo-root PYTHONPATH

### Infrastructure
- [x] `agent/runtime/` package extracted from `agent_runtime_helpers.py` (5 cohesive submodules + `_runtime_ref.py`)
- [x] `hermes_cli/_timeouts.py` + `tools/_limits.py` → `hermes_tiers.py` (single owner)
- [x] `codebase-upgrade-sweep` in-repo skill (tests 9/9, docs scoped, HARDLINE frontmatter)
- [x] Slack `suppressed(logger, *fail_log)` TypeError fixed (pre-format `%` tuple to single msg)

### Ratchet (acceptance gate)
- [x] **29 blockers → 0**: 24 F821 (seam import in `main.py`), 4 FILE_LINES (all 4 caps green), 1 BLE001 (waiver)
  - `message_repair.py`: 2054 → **1737** (moved to `tool_call_integrity.py`)
  - `plugins.py`: 2265 → **2262**
  - `matrix/adapter.py`: 3269 → **3218** (moved to `_text_utils.py`)
  - `methods_tools.py`: 2013 → **2006**
- [x] 3 waiver comments in `hermes_suppress.py` (`# health: allow BLE001/S110 -- <reason>`) — repo precedent format

### Verification
- [x] `python -m compileall -q .` green at every commit
- [x] `tests/test_hermes_suppress.py` 5/5 (seam contract)
- [x] `tests/test_subprocess_timeouts_ratchet.py` green (timeout AST guard)
- [x] `tests/security/test_git_source_safety.py` 8 hostile + 8 legit (architecture #1)
- [x] `tests/security/test_git_ext_transport_guards.py` 91 passed (existing adversarial suite)
- [x] `tests/hermes_cli/test_web_server_fs.py` 28 passed (dashboard fs guard)
- [x] RC3 batch tests: 43 passed, 6 platform-skips
- [x] `import hermes_cli.main` succeeds in deps-complete env (CLI boot verified)
- [x] Context7 Python stdlib docs consulted: `@contextlib.contextmanager`, `subprocess` timeout semantics, `logging.getLogger`, `re.compile` patterns — seam and tiers match canonical docs

## Findings kept RISKY (no unilateral change — maintainer call)
- BlueBubbles guid-as-credential fallback (needs vendor-version testing)
- feishu webhook fail-open when neither token nor encrypt_key configured
- mcp_catalog bootstrap `shell=True` + its `ext::` gap (deferred: concurrent-edit window)
- bootstrap scratch-dir symlink race (fix breaks supported symlinked-scratch deployments)
- `_activation.sh` eval pattern; `xml.etree` XXE surface (needs defusedxml dep decision)
- slack plaintext token store (mitigated by 0o600 convention)
- startup blocking relay/plugin-discovery call on the event loop (copy_context threading)

## Remaining architecture candidates (not landed — report delivered)
- **#2 Strong:** redacted handler factory — 6 entry points each build their own `RedactingFormatter`; collapse to `logging.redacted_handler()`
- **#3 Worth exploring:** finish the `agent/runtime/` seam migration — 189 importers still cross the 200-name compat shim; move importers to the 5 cohesive submodules directly
- **#5 Speculative:** timing-safe compare wrapper — 3 divergent `compare_digest` precedents (webhook, pairing, dashboard auth); adopt only where shape-handling repeats

## Acceptance
`python -m compileall -q .` green + `python scripts/check --only health` zero blocking violations + sampled review clean, then final `main` push. See CHANGES.md for file-level detail.

## Full-history push (rainy-day note)
`main` on this remote is a squash of the 20-commit RC3 stack (content-identical at `git diff`);
the true ledger lives in the local clone (`Downloads/agent-hermes`, 51,011-commit history + 20 RC3 commits).
To replace the squash with the real history from a fast connection:
`git push --force-with-lease=main:<remote-sha> https://github.com/imanujela/hermes-agent.git HEAD:main`
(516 MiB pack; ~30 min at 250 KiB/s, minutes on a home line).
