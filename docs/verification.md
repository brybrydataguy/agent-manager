# Recovery verification, 2026-09-13

The regression suite reproduces the independent review's confirmed defects:
malformed MCP arguments, missing validator output, missing snapshot directories,
wrong worker identity, uncertain split and prompt recovery, delayed revision
acknowledgments, stale revision retries, old submission replays, and uncertain
exit recovery. Additional tests cover concurrent cleanup and adopting a bare
pane. Unit tests use isolated temporary SQLite stores and mocked session effects.

A separate read-only Codex review reproduced two remaining defects during this
change: bare-pane adoption required an already running agent, and overlapping
cleanup calls could both send exit. Both now have regression tests.

## Live smoke test

Inside a Herdr-managed session, a new Claude worker was launched with the runtime's
MCP configuration and subscription checks. The assignment used a synthetic JSON
fixture, not internet research. The placeholder source was explicitly unverified.

- The initial folder-trust UI interrupted startup. Once it cleared in the UI,
  `assign` resumed the same saved pane without creating another worker.
- Claude read the example skill and called the MCP submission tool using the
  required string packet, operation ID, and generation 0.
- The runtime observed the durable event and validated version 1.
- A version-pinned revision was delivered to the same worker.
- Claude submitted version 2 using generation 1; validation and manager review
  confirmed the requested text changes.
- Version 2 was accepted. Cleanup saved the terminal snapshot and delivered
  `/exit`; a subsequent check returned Herdr's `agent_not_found` JSON error and
  marked cleanup confirmed. The pane was retained.

This also exposed and fixed an integration mismatch: Herdr terminal reads return
plain text, while metadata operations return JSON. MCP used newline-delimited
JSON and the unprefixed `submit_artifact` name on the wire.

## Limits

The installed Claude CLI reported 2.1.270 after an update notification. The live
worker displayed Opus 5 and auto permission mode after the folder-trust interaction,
despite the launch requesting plan mode. Thus this test proves live MCP submission
in that observed mode, not automatic permission approval in plan mode. No source
edits or real research were requested. Strict plan-mode submission needs a separate
verification. No billing credentials or transcripts are committed.

The missing-dependency, lost-response, and concurrency failures were reproduced
with tests, not by disrupting a live user's session. This record does not claim
all provider versions, arbitrary shell startup customizations, or crash windows
were exercised live.

## Execution-mode follow-up

A second live test on the same date used the corrected launch configuration:
`--permission-mode default`, built-in tools limited to Read/Glob/Grep/WebFetch/
WebSearch, and the scoped submission tool allowlisted. The UI displayed manual
mode. Claude read the example skill and submitted the synthetic packet directly
through MCP without a plan-approval prompt. The packet validated and was accepted.
This tests the actual supported configuration; strict plan mode is no longer a
research-worker launch option. Folder trust was already established for this
checkout. Automatic handling of a new folder-trust UI was not exercised in this
follow-up.
