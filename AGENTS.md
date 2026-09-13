# Development

Python 3.10+ standard library only. Run `python3 -m unittest discover -s tests -v`.
Use `apply_patch` for source edits. Keep runtime state, MCP capabilities, and
transcripts out of Git. Do not add agent co-author trailers or run no-mistakes.

If the user asks you to act as a research/editor/engineering manager, read
`skills/manager/SKILL.md`. Routine development on this repo does not start workers.
Do not control Herdr from outside a Herdr-managed session. Never claim a live
provider test passed based on mocked tests. Keep provider permission and
subscription checks explicit. See `docs/firstmate.md` before expanding session
orchestration.
