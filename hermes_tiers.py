"""One owner for subprocess/network timeout tiers across the whole tree.

Introduced by RC3: hermes_cli and tools/ each carried a near-identical tier
module (FAST/NETWORK/BUILD identical; INSTALL vs PROCESS a genuine per-tree
class). Both are now thin re-exports of this file — the taxonomy is defined
once and a retune (e.g. NETWORK 300->240) is one edit verified in one place.

Blanket one-size timeouts are a banned pattern: every bounded call gets the
class that matches what it actually does. Pick by what the call IS:

  FAST    — local, sub-second probes: config reads, ``git rev-parse``/status/
            restore, cache/``which``/keychain lookups, single-file signing.
  PROCESS — spawning a local command: rg/grep search, docker/cgroup probes.
  NETWORK — remote fetch or service control: HTTP/urlopen, requests, sandbox
            APIs, ``git ls-remote``/push/fetch, systemctl/launchctl ops.
  BUILD   — heavyweight local work: ``npm run build``, ``ditto``,
            ``codesign --deep``, ffmpeg/image processing, full snapshots.
  INSTALL — package/dependency install: ``npm install``, venv sync, catalog
            bootstrap, a full supervised update child run.

Rules of the road:

- Only APIs that *accept* a timeout take a tier: ``subprocess.run``,
  ``check_output``, ``check_call``, ``call``. ``subprocess.Popen`` has NO
  timeout kwarg — never pass one; bound a Popen via ``wait(timeout=...)``.
- Interactive / session-blocking calls (launching an editor, a desktop app, a
  login or pairing flow, a supervised child meant to run to the end) get NO
  tier — they must not be timed out at all.
- ``**kwargs`` that may already carry a timeout must be setdefault'd, not
  passed alongside an explicit one: duplicate keyword is a per-call TypeError.
- Tiers are integer seconds so they interpolate anywhere a timeout is used.
"""

FAST = 30
PROCESS = 120
NETWORK = 300
BUILD = 900
INSTALL = 1800

__all__ = ["FAST", "PROCESS", "NETWORK", "BUILD", "INSTALL"]
