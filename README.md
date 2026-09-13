# Checkpointed Whistleblower Audits

This repository contains the code and analysis for **Checkpointed Whistleblower Audits Surface Incidents Earlier and More Often Than End-of-Run Shared-State Monitoring**.

Monitoring long-horizon multi-agent systems is difficult because safety-relevant evidence may be distributed across individual agents' local histories. By the end of a run, that evidence may be buried in a large volume of messages, tool calls, and actions—or may not appear in the shared public state at all.

This project tests whether an agent can help audit its own accumulated history. At selected checkpoints, the agent's normal objective is temporarily replaced with an incident-reporting objective. The audit is recorded, the temporary branch is discarded, and the original trajectory remains unchanged.

## Key result

We evaluated the approach on 22 agent trajectories from eight [Game of Agents](https://github.com/Andromede-AI/game-of-agents) runs. Human review identified marketplace-integrity incidents in 19 trajectories.

- Whistleblower audits detected **18 of 19 incidents halfway through the trajectory**.
- Whistleblower audits detected **all 19 incidents by the final checkpoint**.
- The final-state Sentinel detected **11 of 19 incidents** using shared public environment state.

Whistleblower audits therefore surfaced most incidents before the run ended and detected incidents that the Sentinel missed. In several missed cases, the decisive evidence was present in the agent's local history but absent from the shared state available to the Sentinel.

Whistleblower audits also produced more false positives. The negative sample contained only three trajectories, so the false-positive estimate is highly uncertain.

## Method

The experiment compares two monitoring approaches:

```text
Selected agent trajectory
        |
        +-- 50% checkpoint --> disposable local-history whistleblower audit
        |
        +-- final checkpoint --> disposable local-history whistleblower audit
        |
        +-- final public state --> external Sentinel audit
```

### Whistleblower audit

At the halfway and final checkpoints, a disposable audit branch receives the selected agent's accumulated local history. The agent's normal task objective is replaced with an explicit whistleblower objective asking it to report concrete evidence of misleading marketplace conduct.

The audit can use observations and history already available to that agent. It cannot use future events or other agents' private histories.

### Sentinel agent

At the end of the run, an external Sentinel inspects shared public information, including chat, marketplace offers, purchases, reviews, and final leaderboard information. It does not receive private agent histories, local tool results, or filesystem state.

Both approaches use the same reporting threshold and output format. The audit model is `glm-5.3-flash`.

## What is monitored

The evaluation focuses on marketplace-integrity concerns such as misleading claims about:

- rankings and ratings;
- bot functionality or stability;
- prices and scarcity;
- purchases and listings;
- reviews.

Model reports are treated as claims requiring human review, not as ground truth. A `NO_REPORT` response does not establish that an agent was aligned.

## Repository structure

```text
src/scripts/phase_5/     Build agent trajectories from Game of Agents exports
src/scripts/phase_6/     Prepare checkpoints and evaluation cases
src/scripts/phase_7/     Run whistleblower and Sentinel audits and generate results
src/tests/               Tests for the experiment and analysis
docs/                    Experiment plan, reports, figures, and tables
game-of-agents/          Game of Agents submodule
```

See [docs/game_of_agents_whistleblower_plan.md](docs/game_of_agents_whistleblower_plan.md) for the full experimental design.

## Setup

Clone the repository and its Game of Agents submodule:

```bash
git clone --recurse-submodules https://github.com/taaha/AI_incident_sprint.git
cd AI_incident_sprint/game-of-agents
uv sync --group dev
cd ..
```

The scripts expect the Game of Agents run exports and generated trajectory data under `data/whistleblower/`. These large local artifacts are not committed to Git.

## Run the experiment

Validate the input data and generate the audit requests without making model calls:

```bash
cd game-of-agents
uv run python ../src/scripts/phase_7/run_marketplace_v10_matched.py --validate-only
```

To run the audits, add the required provider key to the root `.env` file and run:

```bash
cd game-of-agents
uv run python ../src/scripts/phase_7/run_marketplace_v10_matched.py
```

The script runs one trajectory at a time and safely resumes from existing results.

## Generate the figures and tables

```bash
cd game-of-agents
uv run python ../src/scripts/phase_7/run_generate_v10_results_assets.py
```

Generated assets are written to `docs/figuers_tables/`.

## Run the tests

```bash
cd game-of-agents
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest ../src/tests/phase_7
```

## Limitations

- The study uses a small and imbalanced sample: 19 incident trajectories and three trajectories without an adjudicated incident.
- Agent trajectories come from only eight multi-agent runs and are not fully independent.
- The experiment covers one environment, one failure domain, one audit model, and two checkpoints.
- It replays reconstructed historical context rather than interrupting live agents.
- Some local histories and shared-state exports are incomplete or truncated.
- Human-reviewed labels are used as ground truth and may contain errors.

## Source data

The study uses the public [Game of Agents repository and release](https://github.com/Andromede-AI/game-of-agents), where agents build poker bots, compete in tournaments, trade code, leave reviews, and communicate through shared chat.

The included Game of Agents submodule retains its own [license](game-of-agents/LICENSE).
