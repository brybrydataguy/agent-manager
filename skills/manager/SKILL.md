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

## Assignment and review

1. Use `list` to resume existing assignments before creating new ones. Inspect
   pending candidates and events with `status` and `wait --after <cursor>`.
2. `create --config <file> --task <brief>` records a new assignment. Include the
   reader, decision, scope, and expected deliverable. Use returned run IDs.
3. Inside Herdr, `start <run> --direction right|down` creates an interactive Claude
   worker, checks subscription authentication and project prerequisites, and
   sends its assignment. Choose geometry appropriate to the current pane.
4. Call `wait --after <last-event-id> --timeout 45` in the foreground. Submission
   commits an event alongside the artifact; the waiting call returns when new
   events arrive. Keep waiting while work is active and report meaningful progress.
   No background daemon wakes a manager that has ended its turn. Maintain this
   foreground wait loop while supervising, or explicitly hand off monitoring.
5. On submission, `validate <run>`, then `artifact <run> --version <n>`. Review
   evidence and project policy independently. Validation alone is not approval.
6. For content corrections, `revise <run> --version <reviewed-version> --reason <specific feedback>`, then
   `deliver <run>`. The original worker stays alive for this exchange. Revisions
   are bounded by configuration. Escalate unresolved disagreements to the user.
7. Accept only a reviewed version: `accept <run> --version <n> --reason <review>`.
   The runtime pins its digest. Call `close <run>` to capture a terminal snapshot
   and request Claude exit. Confirm session termination with Herdr; an exit request
   does not prove that the process has stopped. Retain the pane for inspection.

## Recovery

The SQLite run store owns raw candidate bytes, versions, events, and decisions.
Never repeat research just because validation or export failed. Repair authorized
prerequisites and retry `validate` on the existing submission. Malformed JSON is
also retained so the worker can repair it. Readiness of a terminal does not count
as artifact submission.

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

The worker's MCP tool can submit only to its assignment and cannot approve it.
These are tool capabilities, not an OS sandbox: all same-user processes can access
the private store. Claude runs in plan permission mode, without bypass flags.
If its permission UI blocks the submission tool, inspect it and surface the exact
permission request. Do not bypass the provider's permissions to force completion.

## User experience

Keep the user informed about outcomes, review findings, and recovery. Explain
provider limitations candidly. v1 supports visible Claude workers; other provider
adapters and unattended wakeups are future work. Manager sessions can use another
agent CLI capable of running these local commands and maintaining the wait loop.
