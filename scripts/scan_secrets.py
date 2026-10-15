"""Free offline secret scan; compare only explicitly reviewed synthetic fingerprints."""

import json
import subprocess
import sys
from pathlib import Path


def main():
    files = subprocess.check_output(["git", "ls-files", "-z"], text=True).split("\0")
    files = [name for name in files if name and Path(name).is_file() and name != ".secrets.baseline"]
    scanned = subprocess.run(
        [str(Path(sys.executable).with_name("detect-secrets")), "scan", "--all-files", *files],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(scanned.stdout)["results"]
    baseline = json.loads(Path(".secrets.baseline").read_text())["results"]
    allowed = {
        (path, item["type"], item["hashed_secret"])
        for path, entries in baseline.items()
        for item in entries
        if item.get("is_secret") is False
    }
    failures = [
        (path, item["line_number"], item["type"])
        for path, entries in report.items()
        for item in entries
        if (path, item["type"], item["hashed_secret"]) not in allowed
    ]
    if failures:
        print(json.dumps({"unreviewed_findings": failures}))
        raise SystemExit(1)
    print("Secret scan PASS (offline; reviewed synthetic fingerprints only)")


if __name__ == "__main__":
    main()
