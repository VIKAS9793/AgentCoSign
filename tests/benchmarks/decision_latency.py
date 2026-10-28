"""How long a decision takes, measured on the policy scenario table.

Run: ``python -m tests.benchmarks.decision_latency`` (offline, no keys).

Two numbers, both excluding the AI checks and the person:

* the rules alone: the pure policy engine on every row of the scenario table;
* the full decision step as the server runs it: read the refunds so far from the
  record, evaluate, and write the sealed entry to SQLite.

For the rules alone every answer is checked against the table, so a fast wrong
answer fails the run. The full step starts each row from an empty record, so its
answers are not compared; it measures the cost of reading and writing the record.
"""

import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agentcosign.services.ledger import Ledger
from agentcosign.services.policy_engine import evaluate
from tests.scenarios.policy_scenarios import ACCOUNT, NOW, SCENARIOS

ROUNDS = 2  # the table twice: more than 200 evaluations
TARGET_P95_MS = 300.0
SECRET = b"b" * 32


def _p(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "count": len(values),
        "p50_ms": round(_p(values, 0.50), 3),
        "p95_ms": round(_p(values, 0.95), 3),
        "max_ms": round(max(values), 3),
        "mean_ms": round(statistics.fmean(values), 3),
    }


def rules_alone() -> list[float]:
    """The pure engine on every row; each answer must match the table."""
    times: list[float] = []
    for _ in range(ROUNDS):
        for row in SCENARIOS:
            proposal, evidence, ledger, signals, rules, screening = row.build()
            started = time.perf_counter()
            result = evaluate(
                proposal, evidence, ledger, signals, rules, screening, NOW
            )
            times.append((time.perf_counter() - started) * 1000)
            if result.decision is not row.decision:
                raise AssertionError(f"{row.id}: {result.decision} != {row.decision}")
    return times


def full_decision_step() -> list[float]:
    """Read, evaluate and write the sealed entry, as the server does."""
    times: list[float] = []
    with tempfile.TemporaryDirectory() as folder:
        for round_number in range(ROUNDS):
            for index, row in enumerate(SCENARIOS):
                proposal, evidence, _, signals, rules, screening = row.build()
                ledger = Ledger(
                    Path(folder) / f"r{round_number}-{index}.db", SECRET, rules, ACCOUNT
                )
                started = time.perf_counter()
                ledger.decide(proposal, evidence, signals, screening, NOW)
                times.append((time.perf_counter() - started) * 1000)
    return times


def main() -> int:
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    report = {
        "commit": commit,
        "machine": (
            f"{platform.system()} {platform.machine()}, "
            f"Python {platform.python_version()}"
        ),
        "rules_alone": _summary(rules_alone()),
        "full_decision_step": _summary(full_decision_step()),
        "target_p95_ms": TARGET_P95_MS,
    }
    worst = max(report["rules_alone"]["p95_ms"], report["full_decision_step"]["p95_ms"])
    report["meets_target"] = worst < TARGET_P95_MS
    sys.stdout.write(json.dumps(report, indent=1) + "\n")
    return 0 if report["meets_target"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
