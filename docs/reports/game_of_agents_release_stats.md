# Game of Agents release statistics

Generated from the local 47-export release by `src/scripts/phase_5/build_goa_trajectories.py`. The analyzed corpus is identified by the authoritative `game-of-agents/paper/data/run_summary.csv`; exports outside it are still represented in the JSONL release.

## Corpus overview

| Measure | Value |
| --- | ---: |
| Exported runs | 47 |
| Analyzed runs | 39 |
| Exported agent trajectories | 356 |
| Analyzed agent trajectories | 292 |
| Outer decision steps | 5358 |
| Model families | claude-opus-4-7, claude-opus-4-8, claude-sonnet-4-6, o4-mini |
| Conditions | 15 |
| Analyzed run-duration range (h) | 1–6 |
| Chats (authoritative analyzed summary) | 6269 |
| Offers (all exports) | 1756 |
| Purchases (all exports) | 1587 |
| Reviews (all exports) | 165 |
| Bots (all exports) | 3019 |
| Games (all exports) | 11808 |

## Data quality

| Measure | Count | Unit |
| --- | ---: | --- |
| Truncated conversation histories (500-block ceiling) | 57 | trajectories |
| Truncated raw output | 1464 | records |
| Unparseable raw output | 4060 | records |
| Complete prompt/response/timestamp records | 5349 | records |
| Partial records | 9 | records |
| Missing timestamps | 0 | records |
| Unassigned environment-event flags | 0 | records |

## Reconstruction limits

Each JSONL line is one outer saved prompt cycle (`step_id`), not a provider turn, transcript block, or tool event. Context is an **observable approximation**: persisted prompt/transcript material and run configuration. The released public collections are not detailed enough to attach events safely, so environment-event fields remain empty; stored tool output is the evidence of what a model actually observed. This does not claim to replay hidden CLI/session context, system instructions, filesystem state, or omitted tool output. Raw stdout is capped in the release (normally 24,000 characters); malformed and truncated captures are retained and flagged.

Offer listings are subject to the known pagination limitation: the public offer collection may not contain every historical listing. The existing run summary is authoritative where its analyzed-corpus fields are available, notably condition, duration, and chat/offer/purchase counts.

## Glossary

| Term | Definition |
| --- | --- |
| Run | One complete multi-agent Game of Agents execution. |
| Agent | An LLM-controlled participant in a run. |
| Agent trajectory | One agent's persisted activity within one run. |
| Outer decision step | One stored `kind: prompt` with a nonempty `step_id`; the JSONL unit. |
| Provider turn | An internal provider-stream turn; several can occur in one outer step. |
| Event | A logged public/environment action or outcome. |
| Checkpoint | A point immediately before a selected decision step. |
| Audit branch | A disposable counterfactual continuation from a checkpoint. |
| Whistleblower call | The extra model call in an audit branch. |
| Observable reconstruction | Persisted, visible context only; not exact model-state replay. |
| Positive trajectory | A trajectory satisfying a predeclared problematic-behavior criterion. |
| Negative trajectory | An eligible trajectory not satisfying that criterion. |
| Extreme ladderer | An eligible trajectory meeting the three-part laddering criterion in the study plan. |
| False marketplace claim | A listing whose objective numeric claim is unsupported by available state. |
