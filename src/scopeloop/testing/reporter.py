"""Test report generation."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from scopeloop.testing.runner import TestResult, TestStatus


class TestReporter:
    """Generate test reports in various formats."""

    def __init__(self, results: list[TestResult]):
        self.results = results

    def to_dict(self) -> dict[str, Any]:
        """Convert results to a dictionary."""
        passed = sum(1 for r in self.results if r.status == TestStatus.PASSED)
        failed = sum(1 for r in self.results if r.status == TestStatus.FAILED)

        return {
            "generated_at": datetime.now().isoformat(),
            "summary": {
                "total": len(self.results),
                "passed": passed,
                "failed": failed,
                "pass_rate": (passed / len(self.results) * 100) if self.results else 0,
            },
            "tests": [
                {
                    "name": r.test_name,
                    "status": r.status.value,
                    "duration_seconds": r.duration_seconds,
                    "error_message": r.error_message,
                    "criteria_results": [
                        {
                            "name": cr.criterion.name,
                            "status": cr.status.value,
                            "actual_value": cr.actual_value,
                            "expected": cr.criterion.expected,
                            "tolerance_percent": cr.criterion.tolerance_percent,
                            "message": cr.message,
                        }
                        for cr in r.criterion_results
                    ],
                }
                for r in self.results
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        """Generate JSON report."""
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def to_markdown(self) -> str:
        """Generate Markdown report."""
        data = self.to_dict()

        lines = [
            "# Test Report",
            "",
            f"Generated: {data['generated_at']}",
            "",
            "## Summary",
            "",
            f"- Total: {data['summary']['total']}",
            f"- Passed: {data['summary']['passed']}",
            f"- Failed: {data['summary']['failed']}",
            f"- Pass Rate: {data['summary']['pass_rate']:.1f}%",
            "",
            "## Test Results",
            "",
        ]

        for test in data["tests"]:
            status_emoji = {
                "passed": "✅",
                "failed": "❌",
                "skipped": "⏭️",
                "error": "⚠️",
            }.get(test["status"], "❓")

            lines.append(f"### {status_emoji} {test['name']}")
            lines.append("")
            lines.append(f"Status: **{test['status']}**")

            if test["error_message"]:
                lines.append(f"\nError: {test['error_message']}")

            if test["criteria_results"]:
                lines.append("\n#### Criteria")
                lines.append("")
                for cr in test["criteria_results"]:
                    cr_status = "✓" if cr["status"] == "passed" else "✗"
                    lines.append(f"- {cr_status} {cr['name']}: {cr['message']}")

            lines.append("")

        return "\n".join(lines)

    def save(self, path: Path, format: str = "json") -> None:
        """Save report to file.

        Args:
            path: Output file path.
            format: Report format ("json" or "markdown").
        """
        if format == "json":
            content = self.to_json()
        elif format == "markdown":
            content = self.to_markdown()
        else:
            raise ValueError(f"Unknown format: {format}")

        path.write_text(content)
