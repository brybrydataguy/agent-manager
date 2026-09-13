# Relationship to Firstmate

[Firstmate](https://github.com/kunchenguid/firstmate) already implements a broad
agent fleet: visible sessions, multiple provider adapters, persistent coordination,
worktrees, wake queues, and lifecycle control. Its README and
[control-plane documentation](https://github.com/kunchenguid/firstmate/blob/main/docs/agent-control.md)
are useful references. This project does not vendor Firstmate code.

Agent Manager concentrates on a narrower contract: retain submitted artifact bytes,
notify the manager, retry failed validation, review an exact version, request
revisions, and accept a specific digest. That contract can sit below a Research
Manager or Editor and alongside Firstmate's broader supervision.

The immediate lessons incorporated here are durable state, explicit distinction
between a worker's terminal status and accepted work, and separating conversational
revision messages from native exit control. Exit is requested with Claude's native
`/exit` and reported as confirmed only after Herdr returns `agent_not_found` for
the recorded pane. An ambiguous failure is not proof of exit.

The first integration candidate is to let Firstmate own spawning, wakeups, and
verified lifecycle control while this package owns artifacts and review decisions.
That integration is not implemented in v0.1. Avoid importing its entire deployment,
merge, or validation policy into research configuration. In particular, using
Firstmate does not require enabling no-mistakes for this project.

Before adding more session providers here, test a Firstmate adapter against these
operations: launch and return an exact worker identity; deliver revision feedback;
wait on the artifact event cursor; confirm worker exit while preserving artifacts.
