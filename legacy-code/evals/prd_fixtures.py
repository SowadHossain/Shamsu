"""Medium/long PRD benchmark fixtures with machine-checkable acceptance.

These fixtures are intentionally separate from the stochastic eval runner: they
define durable prompts and deterministic acceptance checks so Phase 2+ runs can
reuse the exact same PRDs, commands, and sample counts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "prds"


@dataclass(frozen=True)
class PRDArtifactExpectation:
    path: str
    kind: str = "file"
    contains: str = ""


@dataclass(frozen=True)
class PRDAcceptanceCommand:
    command: str
    expected_stdout: str = ""
    expected_stdout_contains: tuple[str, ...] = ()
    expected_artifacts: tuple[PRDArtifactExpectation, ...] = ()


@dataclass(frozen=True)
class PRDBenchmarkFixture:
    name: str
    size: str
    prd_path: Path
    target_dir: str
    acceptance: tuple[PRDAcceptanceCommand, ...]
    setup_commands: tuple[str, ...] = ()
    required_artifacts: tuple[PRDArtifactExpectation, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def prompt(self) -> str:
        return (
            f"Read {self.prd_path.name} and build the complete project in a new folder "
            f"named {self.target_dir}. Run the acceptance commands from the PRD and "
            "report the evidence. Do not modify anything outside the target folder "
            "except SHAMSU's own .shamsu logs/state and managed .cbmignore."
        )


PRD_BENCHMARK_FIXTURES: tuple[PRDBenchmarkFixture, ...] = (
    PRDBenchmarkFixture(
        name="ledgerlite_medium_cli",
        size="medium",
        prd_path=FIXTURE_ROOT / "ledgerlite_medium.md",
        target_dir="ledgerlite-medium-build",
        acceptance=(
            PRDAcceptanceCommand(
                "python ledgerlite.py seed --db data.json",
                "seeded 4 expenses",
            ),
            PRDAcceptanceCommand(
                "python ledgerlite.py add --db data.json --category travel --amount 42.50 --note taxi",
                "added expense travel 42.50",
            ),
            PRDAcceptanceCommand(
                "python ledgerlite.py summary --db data.json",
                "total 192.50",
            ),
            PRDAcceptanceCommand(
                "python ledgerlite.py export --db data.json --out report.csv",
                "exported report.csv",
                expected_artifacts=(
                    PRDArtifactExpectation("report.csv", contains="id,category,amount,note"),
                ),
            ),
            PRDAcceptanceCommand(
                "python ledgerlite.py list --db data.json",
                expected_stdout_contains=(
                    "exp-001 supplies 18.25 notebooks",
                    "exp-005 travel 42.50 taxi",
                ),
            ),
        ),
        required_artifacts=(
            PRDArtifactExpectation("ledgerlite.py"),
        ),
        tags=("prd", "medium", "cli", "persistence"),
    ),
    PRDBenchmarkFixture(
        name="atlasdesk_long_fullstack",
        size="long",
        prd_path=FIXTURE_ROOT / "atlasdesk_long.md",
        target_dir="atlasdesk-long-build",
        acceptance=(
            PRDAcceptanceCommand("npm test -- --run", ""),
            PRDAcceptanceCommand("npm run build", ""),
            PRDAcceptanceCommand("node scripts/seed.mjs", "seeded 6 records"),
            PRDAcceptanceCommand("node scripts/status.mjs", "open 3 high 2 overdue 1"),
        ),
        setup_commands=("npm install --silent",),
        required_artifacts=(
            PRDArtifactExpectation("package.json", contains="vite"),
            PRDArtifactExpectation("src", kind="directory"),
            PRDArtifactExpectation("scripts/seed.mjs"),
            PRDArtifactExpectation("scripts/status.mjs"),
        ),
        tags=("prd", "long", "react", "node", "sqlite"),
    ),
)


def load_fixture_text(fixture: PRDBenchmarkFixture) -> str:
    return fixture.prd_path.read_text(encoding="utf-8")


def fixture_by_name(name: str) -> PRDBenchmarkFixture:
    for fixture in PRD_BENCHMARK_FIXTURES:
        if fixture.name == name:
            return fixture
    raise KeyError(name)
