# Use Agent Manager from another project

Agent Manager is a separate runtime checkout, not code copied into each site.
There is no required global skill installation. Native provider CLIs still use
their own installed binaries and signed-in accounts. Pin the runtime checkout to
a reviewed commit if reproducibility matters.

| Owner | Files and responsibilities |
| --- | --- |
| Agent Manager checkout | `manager.py`, `auth.py`, provider adapters, `skills/manager/SKILL.md` |
| Consuming project | Task skill, editorial or engineering policy, artifact validator, manager config |
| Private manager root | SQLite artifacts/events, per-worker MCP configuration, snapshots; never commit |

## Minimal project configuration

For a config at `your-project/agent-manager/research.json`, this example resolves
`project` to `your-project`. The referenced skill and validator are files **you
supply**; they are not installed automatically:

```json
{
  "project": "..",
  "provider": "codex",
  "model": "gpt-5.6-sol",
  "skill": "research/SKILL.md",
  "preflight": ["python3", "research/validate.py", "--check"],
  "validator": ["python3", "research/validate.py", "{artifact}"],
  "max_revisions": 3,
  "approvals": {"workspace_trust": true, "artifact_submission": true},
  "read_hosts": [],
  "review_criteria": ["Sources support the claims", "Limitations are explicit"]
}
```

Paths for skill, validator execution, and preflight use the project root.
Validator/preflight are trusted command arrays, not shell snippets. A validator
must inspect `{artifact}` without changing it and exit nonzero on failure.
Add approved source hosts to `read_hosts` only when you want matching native
permission prompts handled automatically. Unknown requests remain blocked.

## Start the manager you talk to

Open your preferred interactive CLI inside Herdr. Give it this prompt, replacing
the example absolute paths:

> Read /path/to/agent-manager/skills/manager/SKILL.md completely. Use runtime
> /path/to/agent-manager/manager.py, config
> /path/to/your-project/agent-manager/research.json, and private root
> /path/to/private-runs/your-project-research. Research [question]. Start visible
> workers, service them regularly, inspect and validate their artifacts, and
> request revisions or accept based on the configured criteria. Keep supervising
> through confirmed worker exit and tab removal. Do not publish or commit results.

The manager skill teaches the workflow; project instructions teach the task.
Use the same explicit `--root` on **every** runtime command, regardless of cwd:

```sh
python3 /path/to/agent-manager/manager.py \
  --root /path/to/private-runs/your-project-research \
  create --config /path/to/your-project/agent-manager/research.json \
  --provider grok --model grok-4.6 --task 'Research question'
```

One root identifies one manager workspace. Reuse it to recover a manager after
restart; use another root for an independent editor or engineering manager.
The runtime creates worker tabs but does not move your existing manager CLI into
that workspace or start an autonomous manager daemon. You can move/open your
manager there in Herdr if desired.

## Myth & Mint

Keep its editorial policy in Myth & Mint and link to it from the research skill.
Point the config at that checkout's actual research skill and contract checker.
If its validator needs Node packages, preflight should check those dependencies
before launching a paid-in-quota research turn. Agent Manager itself needs no
Node dependencies and does not own the editorial policy.

Include article ID, intended reader, decision, and reader benefit in the task.
The run ID, not the article ID alone, separates independent attempts. Agent
Manager saves the submitted bytes even if validation fails. A writer can consume
an accepted packet exported by `artifact RUN --version N`; article generation
and publication are separate authorized workflows, not automatic acceptance
side effects. No production Myth & Mint files are changed by this integration guide.
