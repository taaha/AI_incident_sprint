"""Generate root-repository whistleblower trajectory data from the GoA submodule."""
from pathlib import Path

from goa_trajectory_export import write_release

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "game-of-agents"

if __name__ == "__main__":
    result = write_release(SOURCE / ".goa_data/runs", ROOT / "data/whistleblower", ROOT / "docs/reports/game_of_agents_release_stats.md", SOURCE / "paper/data/run_summary.csv")
    print(f"Generated {result['runs']} runs, {result['trajectories']} trajectories, and {result['records']} decision steps.")
