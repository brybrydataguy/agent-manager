# Agent Manager

Development status: experimental. All four providers have passed live
submission, revision, acceptance, exit, and tab-removal tests with two models each.
See the [verification record](docs/verification.md) for scope and limitations.

A local manager skill and runtime for delegated work you can watch, review, and
recover. Research, editing, and engineering supply their own skills and contracts.

Version 0.2 is an experimental vertical slice: an interactive manager supervises
one or more visible Claude, Grok, Codex, or AntiGravity workers in Herdr. Each manager
root owns a workspace; each worker gets a labeled tab. A worker submits an artifact through
a scoped MCP tool. SQLite stores the raw result and completion event together.
The manager validates and reviews that exact version, asks the same worker for
changes, or accepts it and requests session exit.

## Start a manager

Requirements: Python 3.10+, Herdr, and the subscription-authenticated CLI for each
provider you use. No Python packages, global skills, or external authentication
wrapper are required. Authentication checks live in `auth.py` in this checkout:

```sh
python3 auth.py --check all  # or claude, grok, codex, agy
```

Claude and Codex must report OAuth/ChatGPT authentication. Grok and Agy must return
an authenticated model catalog. All checks reject known API credential/routing
environment variables. Provider settings and shell startup files are trusted:
the catalog checks are not independent billing audits. Never configure API billing.

Open your preferred interactive manager CLI **inside Herdr**, in this checkout,
and give it this instruction:

> Read skills/manager/SKILL.md and use examples/research/config.json. Research
> how to choose a primary source when comparing product specifications. Start a
> visible researcher, review its submitted packet, request changes if needed,
> and report the accepted result. Keep supervising until the assignment is done.

The manager uses these operations internally; you do not need to run them one by
one:

```sh
python3 manager.py create --config examples/research/config.json --task 'Research question'
python3 manager.py start RUN_ID
python3 manager.py service RUN_ID
python3 manager.py wait --after 0 --timeout 3
python3 manager.py validate RUN_ID
python3 manager.py artifact RUN_ID --version 1
python3 manager.py revise RUN_ID --version 1 --reason 'Explain the missing evidence'
python3 manager.py deliver RUN_ID
python3 manager.py accept RUN_ID --version 2 --reason 'Verified sources and scope'
python3 manager.py close RUN_ID
```

Repeat `service` for active workers every 1-3 seconds. It handles narrowly granted
startup prompts, assignment handshakes, pending revisions, and accepted-worker
cleanup. Unknown permission requests need inspection, not blanket approval.
`wait` returns durable events and their numeric cursors. The manager passes the
last ID on the next call. Submission wakes that foreground wait, including when
submission preceded the wait. There is no daemon that injects prompts into a
manager that has stopped supervising. On restart, use `list`, `status`, and `wait`
against the same `--root` (default `.manager`).

## Configure another project

Copy the example configuration into your project. `project` resolves relative to
the configuration file; `skill` resolves relative to that project. `validator`
and `preflight` are trusted argv arrays, executed in the project without a shell.
`{artifact}` expands to the candidate file. Set `model` to an exact model ID your
selected provider supports, or omit it for the CLI default. Set `provider` explicitly
to `claude`, `grok`, `codex`, or `agy`. `create --provider ... --model ...` overrides
the config for one worker. `review_criteria`
guides the manager's judgment. `max_revisions` defaults to three.

For Myth & Mint, point `skill` at its article research skill and `validator` at
its research contract checker. Use a preflight to verify its Node dependencies
before delegating research. Supply article identity and expected evidence path
in the task. The runtime itself does not depend on Myth & Mint or its editorial
policy. Add `.manager/` to the consuming project's ignore file before use, or
choose a private root outside that repository.

See [cross-project integration](docs/integration.md) for directory ownership,
a portable configuration example, and the manager startup prompt.

## Durability and permissions

Raw submissions, hashes, versions, review decisions, and events are authoritative
in `.manager/state.sqlite`. Candidate JSON files are reproducible validator inputs.
The accepted version is an immutable database reference. `artifact` exports a
version to stdout for downstream consumption. Missing validators or malformed
JSON leave the original bytes intact. Fix prerequisites and retry validation;
research does not need to run again.

Each submission carries a worker-generated `operation_id` and the assignment's
`generation`. A transport retry reuses both and identical packet bytes; it returns
the original receipt even after review has advanced. A new operation from an old
generation is rejected. Revisions name an immutable artifact version. Retrying
the same version/reason is idempotent and does not consume another revision slot.
Old delivery acknowledgments cannot overwrite a newer feedback item.

The v0.2 submission tool requires these two fields. Existing SQLite stores are
preserved and extended automatically. Existing v0.1 workers need to reconnect
their MCP server and receive the updated submission instructions before continuing;
historical v0.1 submissions have no operation receipts to replay. The `revise`
command now requires `--version`.

Workers receive a per-run MCP capability that can submit but cannot approve.
Claude starts in normal execution mode with only Read, Glob, Grep, WebFetch,
WebSearch, and the per-run submission MCP tool available and allowlisted. It has
no shell, file-write, or subagent tools. Plan mode is intentionally not used:
its execution prohibition also blocks artifact submission. New Herdr
panes explicitly clear API credential and third-party-routing environment variables
so they cannot inherit different billing credentials from the Herdr server.
Subscription authentication is checked before starting. Shell startup scripts and
provider-managed settings remain trusted configuration; do not configure them to
reintroduce API credentials in a subscription-only workflow.

The example config grants the manager `approvals.workspace_trust` for the exact
worker directory and `approvals.artifact_submission` for its scoped MCP tool. The manager
skill uses these grants to handle matching permission prompts without asking
again. `read_hosts` grants only recognized fetch prompts for those exact hosts,
including a narrowly matched header-only curl request. It retains normal
permissions and never blanket-approves plans. `inspect RUN` provides the live
worker and terminal state. The manager must still be actively supervising;
this is not an unattended background UI approval service.
This is not OS-level isolation: another process running as your user can read or
modify your files, including the SQLite store. Use isolated users/containers for
untrusted workers. Configuration is trusted executable input.

The runtime never merges, deploys, or publishes an accepted artifact. Publication
belongs to a separately authorized project workflow. Runtime state is private and
Gitignored. Do not commit run stores, transcripts, or generated MCP credentials.

## Verification and known limits

```sh
python3 -m unittest discover -s tests -v
```

The tests exercise failure recovery after a missing validator, manager restart,
MCP submission from a separate process, revision, version-pinned acceptance,
duplicate submission rejection, malformed-call recovery, delayed-delivery races,
and simulated Herdr startup ordering/failure. A live Herdr/Claude smoke test on
2026-09-13 also exercised submission, revision, acceptance, and confirmed exit.
See [the verification record](docs/verification.md) for its precise scope.

External session operations cannot be atomic with SQLite. A startup failure
preserves the known pane; uncertain prompt delivery requires inspection before
retrying. The manager skill explains recovery. A crash between pane creation and
recording its ID requires locating the pane before reconciling:

| Observed condition | Recovery operation |
| --- | --- |
| Split outcome uncertain; inspection proves no pane was created | `recover-start RUN --no-pane-created`, then `start RUN` |
| Found the bare pane or ready worker left by that split | `recover-start RUN --pane ID`; use `launch RUN` for a bare pane or `assign RUN` for a ready worker |
| Worker launch failed; recorded pane is still agent-free | `launch RUN --retry-launch` |
| Worker was blocked at startup and is now ready | `assign RUN` |
| Initial prompt uncertain; inspection establishes it was not delivered | `assign RUN --retry-undelivered` |
| Current revision message uncertain and not delivered | `deliver RUN --retry-undelivered` |
| Exit failed or is uncertain; same worker still present | `close RUN --retry-exit` |

These explicit retries record a new attempt; they are not automatic retries of
uncertain external side effects. `close` atomically claims cleanup, saves a bounded
terminal snapshot and sends the provider's native exit command. It reports
`confirmed` only when Herdr reports no agent in the recorded pane, otherwise
`exit_requested` or `exit_uncertain`; manager inspection is then required.
The snapshot is not a guaranteed complete transcript.

Confirmed exit removes the owned single-pane worker tab. Extra panes or a changed
occupant stop cleanup. The manager workspace and private evidence remain.
Claude has restricted read tools; Grok has restricted tools in a private working
directory; Codex uses its read-only sandbox. Agy uses its sandbox and accept-edits
mode with scoped submission approvals. These are not equivalent filesystem
security boundaries: Agy's mode can edit its workspace. Project read-only
instructions alone are not an OS sandbox.

Future work: unattended manager wakeups,
automatic discovery of panes after lost split responses, complete transcript
capture, and Editor/Engineering integrations with isolated worktrees.

See [Firstmate integration boundary](docs/firstmate.md) before extending fleet
supervision. Firstmate already addresses much of that broader workflow.
