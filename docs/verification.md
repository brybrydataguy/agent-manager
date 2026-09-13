# Recovery verification, 2026-09-13

## Current four-provider lifecycle verification

The corrected runtime completed the following live, interactive Herdr cases.
Each listed run acknowledged generations 0 and 1, fetched the assigned source,
submitted version 1, received a version-pinned revision, submitted version 2,
passed the example validator and correction-marker check, and finished with
`cleanup=confirmed` and `tab_removed=true`. No manual prompt resend or UI action
was used in these listed runs. Earlier diagnostic attempts below are not passes.

| Provider | Model | Successful run |
| --- | --- | --- |
| Claude | `claude-opus-5` | `996ec74ac2994b1b` |
| Claude | `claude-sonnet-5` | `30d17fb386394901` |
| Grok | `grok-4.6` | `adbd5e7139ba4adc` |
| Grok | `grok-4.5` | `f842ea761cc54ba5` |
| Codex | `gpt-5.6-sol` | `b89971fa8e9b4984` |
| Codex | `gpt-5.6-terra` | `4e14cb5944e84664` |
| Agy | `gemini-3.8-flash-high` | `9d5d71b42a3c47d8` |
| Agy | `gemini-3.1-pro-high` | `0a04c2c8e219496a` |

The task requested two sentences about https://www.python.org/doc/ with sources
and limitations. The revision added `integration-smoke-v2` to limitations.
Acceptance in this test is protocol acceptance, **not** editorial approval or
proof that all source claims are correct. Private SQLite receipts and bounded
snapshots are retained locally, not committed. Models use subscription quota.

Reproduce inside Herdr with the opt-in runner; it services at most two workers
concurrently and never auto-publishes:

```sh
python3 scripts/live_smoke.py --live --root /private/path/to/test-runs \
  --case claude:claude-opus-5 --case claude:claude-sonnet-5
```

Use the other exact model IDs above with repeated `--case` arguments. Each run
saves a private `smoke-result.json`; the root `matrix.json` describes the most
recent invocation only. Keep failed attempts when evaluating reliability.

Fixes exercised include delayed startup dispatch, durable assignment receipt,
Agy MCP-panel initialization and lazy tool calling, Codex nested-tool discovery,
explicit end-of-turn instructions, scoped approvals, safe token argument parsing,
and waiting for the final provider turn before snapshot/exit. Unknown shell
approval menus are intentionally not auto-approved. A repeated Flash probe tried
a curl pipeline after incomplete web extraction and stopped at that boundary;
the brief now explicitly requires reporting incomplete extraction as a limitation.
Another repeat exposed Herdr refusing a new tab before its shell was ready.
Only that explicit pre-launch refusal has a bounded same-pane retry; uncertain
launch outcomes still require inspection. These are successful test cases, not
a claim of zero intermittent failures or support for every provider CLI version.

Authentication now uses repo-local `auth.py`, with no global skill or wrapper
dependency. All four real CLI preflight checks passed. Unit tests additionally
reject billing environment variables, unknown authentication output, CLI errors,
and timeouts without printing credentials. Provider-managed authentication and
shell startup configuration remain trusted inputs.

Post-removal lifecycle checks also passed: Claude Sonnet
`be3c892182df4d76`, Grok 4.6 `d8be5c8e56fc4552`, Codex Sol
`6c6e798f451c467f`, and Agy Pro `ccc04f7511f14b31`. The Claude/Agy pair
used Python 3.11.15. The full 48-test stdlib suite passed on Python 3.11.15;
`git diff --check` passed. Native CLI versions were Claude 2.1.270, Grok 1.0.30,
Codex 0.154.0, and Agy 1.2.2. The optional development skill-validator script
could not run because its PyYAML dependency is not installed; the repo runtime
and unit suite require no such package.

The historical records below describe earlier implementations and limitations.

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

## Four-provider interactive matrix (2026-09-13)

Live Herdr tests used a dedicated `Provider Verification` workspace and separate
named worker tabs. Subscription authentication checks passed for all four
providers. No API keys or global MCP configurations were added.

| Provider | Explicit model | Result |
| --- | --- | --- |
| Claude | `claude-opus-5` | Full lifecycle passed |
| Claude | `claude-sonnet-5` | Full lifecycle passed |
| Grok | `grok-4.6` | Full lifecycle passed with initial-prompt recovery |
| Grok | `grok-4.5` | Full lifecycle passed with initial-prompt recovery |
| Codex | `gpt-5.6-sol` | Full lifecycle passed with approval and idle-loop recovery |
| Codex | `gpt-5.6-terra` | Full lifecycle passed with approval and idle-loop recovery |
| Agy | `gemini-3.8-flash-high` | Failed: submission tool absent, including one configuration retry |
| Agy | `gemini-3.1-pro-high` | Failed: submission tool absent, including one configuration retry |

Model choices were checked against the live worker UI, not merely the requested
command line. Full lifecycle means: read the assigned example skill, fetch
`https://www.python.org/doc/`, submit a source/summary/limitations JSON packet,
validate v1, receive a version-pinned revision, submit v2 with an explicit
integration-smoke-test limitation, validate and accept v2, request native exit,
confirm `agent_not_found`, and remove the owned single-pane tab. Acceptance was
only for this integration test, not publication or a factual quality benchmark.

Observed recovery requirements:

- Grok and Agy initially stayed on their welcome screens despite Herdr reporting
  successful prompt delivery. Inspection established no task was running, then
  the coordinator resent the assignment once. The launcher does not yet detect
  or recover this automatically; `initial_delivery: sent` is not proof of receipt.
- Grok requested permission to fetch the explicitly assigned URL. The coordinator
  selected allow-once, not always-approve mode.
- Codex requested trust for the exact project directory and permission for the
  per-run submission tool. The coordinator granted directory trust and allowed
  that tool for the session. Both models interpreted "wait for manager feedback"
  as calling native agent-wait tools, which prevented normal revision delivery.
  After inspecting that idle loop, the coordinator interrupted it and delivered
  feedback. The runtime prompt now explicitly says to end the turn instead.
- Agy could read the skill and fetch the URL after narrowly scoped approvals,
  but project-local plugin discovery did not expose `submit_artifact`. Adding an
  explicit `.agents/plugins.json` registration and restarting once did not fix it.
  Both models reported the tool absent. Sandbox-bypass requests to search for a
  submission executable were declined. No alternate submission path was used to
  manufacture a pass. These adapters remain experimental and unsuitable for
  unattended research. Failed sessions were exited and their tabs removed too.

Raw packets and bounded terminal snapshots remain in the private local run store;
none are committed. The manager workspace remains open, without worker tabs.
Local regression suite after these changes: 34 tests passed. New tests cover
provider/model arguments, private config permissions, provider identity, native
exit commands, workspace reuse/uncertain creation, and refusing to close a tab
that acquired another pane. They do not replace the live matrix above or prove
that Agy MCP works.
