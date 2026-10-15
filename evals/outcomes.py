"""Explicit coverage and exit semantics shared by optional evaluation commands."""

from typing import Any


def select_rows(rows: Any, requested: list[str] | None) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("Evaluation input must contain cases")
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] for row in rows):
        raise ValueError("Evaluation cases need nonempty string IDs")
    identities = [row["id"] for row in rows]
    if len(set(identities)) != len(rows):
        raise ValueError("Evaluation case IDs must be unique")
    if requested is None:
        return rows
    if not requested or len(set(requested)) != len(requested) or not set(requested) <= set(identities):
        raise ValueError("Requested cases are missing or duplicated")
    return [row for row in rows if row["id"] in requested]


def aggregate(report: dict[str, Any]) -> dict[str, Any]:
    required = report.get("required_count", 0)
    results = report["results"]
    passed = sum(row.get("passed", False) for row in results)
    evaluated = sum(row.get("evaluated", False) for row in results)
    success = bool(required and len(results) == required and passed == required and not report.get("error"))
    return {
        "status": ("PASS_PARTIAL" if report.get("partial") else "PASS") if success else "FAIL",
        "required_count": required,
        "result_count": len(results),
        "evaluated_count": evaluated,
        "passed_count": passed,
        "failed_count": required - passed,
        "missing_count": max(required - len(results), 0),
    }
