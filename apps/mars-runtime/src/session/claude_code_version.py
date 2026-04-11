"""Pinned Claude Code CLI version for the Mars runtime.

Upgrading this is a DELIBERATE act. When bumping:

1. Re-run ``spikes/02-stream-json-capture.sh`` to refresh
   ``tests/contract/fixtures/stream_json_sample.jsonl``.
2. Run ``pytest tests/runtime/test_claude_code_stream.py`` and fix any
   parser drift.
3. Run ``pytest tests/contract/test_claude_code_stream.py`` with a
   ``claude`` CLI of the new version installed to confirm the contract
   holds end-to-end.
4. Bump the version in ``apps/mars-runtime/Dockerfile`` (Epic 3).
5. Update this constant.
6. Commit all of the above together in one PR so the contract stays
   internally consistent.

Version format: matches ``claude --version`` output (semver-ish string).
"""

PINNED_CLAUDE_CODE_VERSION = "2.1.101"
