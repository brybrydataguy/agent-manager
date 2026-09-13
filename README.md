# Agent Manager

A local manager skill and runtime for delegated work you can watch, review, and
recover. Research, editing, and engineering supply their own skills and contracts.

Version 0.1 is an experimental vertical slice: an interactive manager supervises
one or more visible Claude workers in Herdr. A worker submits an artifact through
a scoped MCP tool. SQLite stores the raw result and completion event together.
The manager validates and reviews that exact version, asks the same worker for
changes, or accepts it and requests session exit.

## Start a manager

Requirements: Python 3.10+, Herdr, subscription-authenticated Claude CLI, and the
external-model-orchestrator authentication wrapper. No Python packages required.
The default wrapper path is
`~/.codex/skills/external-model-orchestrator/scripts/run-external-model`.
Set `AGENT_MANAGER_AUTH_WRAPPER` to an alternative compatible wrapper.
It must support `--check claude` and reject API billing credentials.

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
python3 manager.py start RUN_ID --direction right
python3 manager.py wait --after 0 --timeout 45
python3 manager.py validate RUN_ID
python3 manager.py artifact RUN_ID --version 1
python3 manager.py revise RUN_ID --reason 'Explain the missing evidence'
python3 manager.py deliver RUN_ID
python3 manager.py accept RUN_ID --version 2 --reason 'Verified sources and scope'
python3 manager.py close RUN_ID
```

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
Claude subscription supports, or omit it for the CLI default. `review_criteria`
guides the manager's judgment. `max_revisions` defaults to three.

For Myth & Mint, point `skill` at its article research skill and `validator` at
its research contract checker. Use a preflight to verify its Node dependencies
before delegating research. Supply article identity and expected evidence path
in the task. The runtime itself does not depend on Myth & Mint or its editorial
policy. Add `.manager/` to the consuming project's ignore file before use, or
choose a private root outside that repository.

## Durability and permissions

Raw submissions, hashes, versions, review decisions, and events are authoritative
in `.manager/state.sqlite`. Candidate JSON files are reproducible validator inputs.
The accepted version is an immutable database reference. `artifact` exports a
version to stdout for downstream consumption. Missing validators or malformed
JSON leave the original bytes intact. Fix prerequisites and retry validation;
research does not need to run again.

Workers receive a per-run MCP capability that can submit but cannot approve.
Claude starts in plan mode, with the submission server explicitly configured and
its tool allowlisted. Permission prompts remain under the provider's control.
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
duplicate submission rejection, and simulated Herdr startup ordering/failure.
They do not establish compatibility with a live provider UI. A live Herdr/Claude
smoke test must be performed inside Herdr; v0.1 has not yet passed that test.

External session operations cannot be atomic with SQLite. A startup failure
preserves the known pane; uncertain prompt delivery requires inspection before
retrying. The manager skill explains recovery. A crash between pane creation and
recording its ID may require manually locating the pane. `close` saves a bounded
terminal snapshot and sends Claude's native `/exit` command. It reports
`confirmed` only when Herdr reports no agent in the recorded pane, otherwise
`exit_requested`; manager inspection is then required.
The snapshot is not a guaranteed complete transcript.

Future work: verified adapters for other providers, unattended manager wakeups,
automatic reconciliation of interrupted session operations, complete transcript
capture, and Editor/Engineering integrations with isolated worktrees.

See [Firstmate integration boundary](docs/firstmate.md) before extending fleet
supervision. Firstmate already addresses much of that broader workflow.
