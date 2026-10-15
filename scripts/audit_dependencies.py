"""Audit complete committed locks using the pinned free PyPI advisory client."""

import json
import subprocess
import sys
from pathlib import Path

LOCKS = ("requirements.lock", "requirements-dev.lock")


def main() -> None:
    directory = Path("artifacts/dependency-audit")
    directory.mkdir(parents=True, exist_ok=True)
    outcomes = []
    for lock in LOCKS:
        # No dependency resolution/install or automatic fixes; audit every exact lock version.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--no-deps",
                "--disable-pip",
                "-r",
                lock,
                "--format",
                "json",
                "--output",
                str(directory / (lock + ".json")),
            ],
            check=False,
        )
        outcomes.append({"lock": lock, "exit_code": result.returncode})
    failed = any(item["exit_code"] != 0 for item in outcomes)
    print(json.dumps({"status": "FAIL" if failed else "PASS", "audits": outcomes}))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
