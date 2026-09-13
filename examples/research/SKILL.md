---
name: example-research
description: Produce a small source-backed research packet for the agent-manager example.
---

Research the user's question using primary sources when feasible. Return a JSON
object with `summary`, `sources` (objects with `url` and `claim`), and `limitations`
(an array of strings). Distinguish observations from inference in your prose.
Do not claim to have inspected a source you could not access. Submit the complete
JSON through the supplied submission tool and wait for the manager's review.
