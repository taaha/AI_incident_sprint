"""Generate Phase 6's label and checkpoint artifacts without model calls."""

from pathlib import Path

from laddering_checkpoints import generate_phase_6


ROOT = Path(__file__).resolve().parents[3]

if __name__ == "__main__":
    result = generate_phase_6(
        ROOT / "game-of-agents/paper/data/chip_games.csv",
        ROOT / "game-of-agents/paper/data/run_summary.csv",
        ROOT / "data/whistleblower",
        ROOT / "data/whistleblower",
    )
    print(f"Generated {result['labels']} labels, {result['pairs']} matched pairs, and {result['checkpoints']} checkpoints.")
