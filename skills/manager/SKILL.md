---
name: manager
description: Manage delegated assignments in visible Herdr sessions, review versioned artifacts, request revisions, and recover interrupted work using the agent-manager runtime.
---

# Manager

You are the user's persistent point of contact. Own each assignment through an
accepted result or a clearly explained blocker. Project configuration supplies
worker skills, validators, and review criteria. Read the project's instructions
and configuration before assigning work. This skill does not authorize merging,
publishing, installing software, or changing project scope by itself.

The runtime is `../../manager.py` relative to this skill directory. Resolve its
absolute path once. Use a consistent absolute `--root` outside tracked content.
Run `--help` for commands. Python 3.10+ is required; no runtime packages are needed.
Authentication checks are repo-local in `auth.py`; do not require or install a
global skill or authentication wrapper. See `docs/integration.md` in the runtime
checkout for consuming-project setup. Resolve task policy in the consuming project.

## Assignment and review

1. Use `list` to resume existing assignments before creating new ones. Inspect
   pending candidates and events with `status` and `wait --after <cursor>`.
2. `create --config <file> --provider <provider> --model <model> --label <short-name> --task <brief>` records a new assignment. Include the
   reader, decision, scope, and expected deliverable. Use returned run IDs.
3. Inside Herdr, `start <run>` checks subscription authentication and project
   prerequisites and creates a named worker tab in this manager store's workspace.
   Claude, Grok, Codex, and Agy are selectable; preserve the user's model choice.
   Do not create right splits. One absolute `--root` identifies one manager and
   reuses its workspace across restarts. `workspace --label <name>` initializes
   a named workspace before the first run when desired.
4. Call `wait --after <last-event-id> --timeout 45` in the foreground. Submission
   commits an event alongside the artifact; the waiting call returns when new
   events arrive. Keep waiting while work is active and report meaningful progress.
   No background daemon wakes a manager that has ended its turn. Maintain this
   foreground wait loop while supervising, or explicitly hand off monitoring.
   While starting or supervising, call `service <run>` frequently (every 1-3
   seconds during startup or a gate). It initializes the bootstrap, handles
   narrowly authorized native menus, dispatches pending revisions, and completes
   accepted-worker cleanup. These are short, restartable ticks, not a daemon.
   Use shorter event waits during startup. When no progress occurs, inspect
   outstanding workers with `inspect <run>`.
   An idle or blocked worker without a submission needs inspection, not another
   indefinite wait. Follow the approval policy below when a routine gate appears.
5. On submission, `validate <run>`, then `artifact <run> --version <n>`. Review
   evidence and project policy independently. Validation alone is not approval.
6. For content corrections, `revise <run> --version <reviewed-version> --reason <specific feedback>`, then
   `deliver <run>`. The original worker stays alive for this exchange. Revisions
   are bounded by configuration. Escalate unresolved disagreements to the user.
7. Accept only a reviewed version: `accept <run> --version <n> --reason <review>`.
   The runtime pins its digest. Call `close <run>` to capture a terminal snapshot
   and request native provider exit. Continue `service` until cleanup is confirmed
   and `tab_removed` is true. Only the owned single-pane tab is removed; artifacts
   and bounded snapshots remain in the store and the manager workspace stays open.

## Recovery

The SQLite run store owns raw candidate bytes, versions, events, and decisions.
Never repeat research just because validation or export failed. Repair authorized
prerequisites and retry `validate` on the existing submission. Malformed JSON is
also retained so the worker can repair it. Readiness of a terminal does not count
as artifact submission.

New workers call `get_assignment` before doing work. That tool durably records
the generation acknowledgment and returns the brief. An initial terminal write
is not receipt: require `initial_delivery: acknowledged`. Agy's bootstrap first
checks its native MCP panel because its tools are lazily loaded. Never replace
missing-tool failures with a shell submission workaround. The worker ends its
turn after submission; it must not call native agent-wait tools. Revisions also
call `get_assignment` and acknowledge their exact generation.

Session creation and prompt delivery involve external effects. A crash can leave
`starting`, `initial_delivery: uncertain`, or `delivery: uncertain`. Inspect the
recorded pane and named worker using Herdr before taking action; do not blindly
relaunch or resend. For a split with no recorded pane, inspect Herdr. If no pane
was created, use `recover-start <run> --no-pane-created`, then start again. If a
bare pane or ready worker was created, use `recover-start <run> --pane <id>` to
adopt it. Use `launch <run>` to launch in an adopted bare pane; after an uncertain
launch, `launch <run> --retry-launch` first verifies that pane is still agent-free.
If startup was blocked and the recorded worker is now ready, use `assign <run>`.
For uncertain initial delivery, establish that it was not delivered, then use
`assign <run> --retry-undelivered`. It uses the exact persisted prompt. For an
undelivered revision, use `deliver <run> --retry-undelivered`. For uncertain exit,
inspect the recorded worker and use `close <run> --retry-exit` when needed.
Submission remains enabled while an initial/revision delivery is uncertain.

Workers submit `packet`, `operation_id`, and `generation`. The prompt supplies the
generation. A retry must reuse its original operation ID and bytes, not mint a
new operation or adopt a newer generation. Receipt replay does not constitute a
new revision. Review feedback is pinned to the exact version; a retry of the same
feedback leaves newer submissions untouched.

## Routine approvals

The user wants the manager to resolve routine gates within the assignment. Read
`config.approvals`; its explicit grants authorize these actions without asking
the user again. They do not grant new authority to publish, install, edit, or
execute arbitrary commands. Missing grants mean ask the user.

- `workspace_trust: true`: accept the provider's folder-trust prompt only when
  the displayed canonical path matches the recorded worker directory exactly.
- `artifact_submission: true`: allow the exact per-run submission MCP tool.
  For a legacy worker blocked in plan mode, read its complete plan and ensure
  the only proposed execution is that tool, with the expected run, generation,
  and candidate packet. Approve using the option that retains normal permission
  checks, never automatic file editing or bypass permissions. This permits
  submission for review, not acceptance of the submitted content.

`service` also allows reading the exact assigned skill and reads of explicitly
configured `read_hosts` (including a narrowly matched header-only curl request,
without sandbox bypass). It leaves unknown commands, other files, other hosts,
broader plans, and changed menus for inspection. Do not broaden these grants to
make a test pass. Native approval handlers are provider/version-specific; a new
unrecognized UI requires a deliberate update, not guessed menu keys.

Before any UI response, verify the named worker still occupies its recorded pane,
inspect the actual prompt and current choices, and ensure the user is not editing
in the pane. Do not send menu keys into an editor, infer option numbers from a
past screenshot, or treat an agent's ordinary prose as a permission dialog.
After responding, inspect the result and return to event monitoring. If the menu
does not offer a suitably scoped option, surface the blocker instead of broadening
permissions. Tell the user what routine gate you resolved as a progress update.

Research workers use execution mode, never plan mode that prohibits submission.
Claude has a read/web tool whitelist. Grok uses its read tool configuration and
disables native subagents. Codex uses a read-only sandbox and disables agent
tools. Agy uses its terminal sandbox and a per-run workspace; its accept-edits
mode still permits workspace writes, so it is not a universal read-only sandbox.
Do not claim identical tool or filesystem isolation across providers.

The worker's MCP tool can submit only to its assignment and cannot approve it.
These are tool capabilities, not an OS sandbox: all same-user processes can access
the private store. Claude runs with normal permissions and a restricted tool
list, without bypass flags. Apply the scoped approval grants above to routine
permission gates; escalate requests outside those grants.

## User experience

Keep the user informed about outcomes, review findings, and recovery. Explain
provider limitations candidly. Unattended wakeups remain future work. Manager sessions can use another
agent CLI capable of running these local commands and maintaining the wait loop.
