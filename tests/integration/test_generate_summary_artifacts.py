"""Render summaries from detector and selection artifacts."""

import json
from pathlib import Path

from scripts.generate_summary import generate_summary


class TestGenerateSummary:
    """Tests for generate_summary function."""

    def test_generates_full_summary(self, tmp_path: Path):
        """Generates complete summary with all sections."""
        # Create report.csv
        report_content = '''Name,File,Line Number,Description,Severity,Snippet,Related Participants
"Cyclic Dependency","src/a.py",10,"Cycle detected","Critical","def foo(): pass","[]"
"Cyclic Dependency","src/b.py",20,"Another cycle","Major","def bar(): pass","[]"
'''
        report_path = tmp_path / "report.csv"
        report_path.write_text(report_content)

        summary = generate_summary(report_path)

        assert "## Smell Detection Summary" in summary
        assert "**Total smells detected:** 2" in summary
        assert "### Smells by Type" in summary
        assert "### Smells by Severity" in summary
        assert "| Cyclic Dependency | 2 |" in summary
        assert "| Critical | 1 |" in summary
        assert "| Major | 1 |" in summary

    def test_handles_missing_report(self, tmp_path: Path):
        """Shows message when report doesn't exist."""
        report_path = tmp_path / "nonexistent.csv"
        summary = generate_summary(report_path)

        assert "_No smell report found._" in summary

    def test_handles_empty_report(self, tmp_path: Path):
        """Shows message when no smells detected."""
        report_content = "Name,File,Line Number,Description,Severity,Snippet,Related Participants\n"
        report_path = tmp_path / "report.csv"
        report_path.write_text(report_content)

        summary = generate_summary(report_path)

        assert "**No architectural smells detected.**" in summary

    def test_includes_selected_smells_section(self, tmp_path: Path):
        """Includes selected smells when json provided."""
        # Create report.csv
        report_content = '''Name,File,Line Number,Description,Severity,Snippet,Related Participants
"Cyclic Dependency","src/a.py",10,"Cycle detected","Critical","def foo(): pass","[]"
'''
        report_path = tmp_path / "report.csv"
        report_path.write_text(report_content)

        # Create filtered_report.csv (same content for simplicity)
        filtered_path = tmp_path / "filtered_report.csv"
        filtered_path.write_text(report_content)

        # Create smells_to_process.json with a UUID
        # The UUID is generated from the smell properties
        from scripts.filter_smells import generate_smell_uuid
        import csv

        with open(report_path, 'r') as f:
            reader = csv.DictReader(f)
            row = next(reader)
            smell_uuid = generate_smell_uuid(row)

        smells_path = tmp_path / "smells_to_process.json"
        smells_path.write_text(json.dumps([smell_uuid]))

        summary = generate_summary(report_path, filtered_path, smells_path)

        assert "### Smells Selected for Refactoring" in summary
        assert "**1** of 1 eligible smell(s) selected" in summary
        assert "| Smell Type | File | Severity | UUID |" in summary

    def test_distinguishes_detected_and_supported_findings(self, tmp_path: Path):
        """Eligibility counts use supported names regardless of letter case."""
        from scripts.filter_smells import generate_smell_uuid

        report_path = tmp_path / "report.csv"
        report_path.write_text(
            "Name,Severity\n"
            "CYCLIC DEPENDENCY,High\n"
            "potential redundant abstractions,Medium\n"
            "Hub-like Dependency,High\n"
            "Redundant Abstractions,Low\n"
        )
        smells_path = tmp_path / "smells_to_process.json"
        smells_path.write_text(json.dumps([
            generate_smell_uuid({"Name": "CYCLIC DEPENDENCY", "Severity": "High"})
        ]))

        summary = generate_summary(report_path, smells_json_path=smells_path)

        assert "**Total smells detected:** 4" in summary
        assert "**Eligible for supported refactoring (before repository filters):** 2" in summary
        assert "**Excluded unsupported smells:** 2" in summary
        assert "| Hub-like Dependency | 1 |" in summary
        assert (
            "**1** of 2 eligible smell(s) selected after repository filters "
            "and selection limits (50%)"
        ) in summary

    def test_reports_unsupported_only_findings_without_claiming_no_detection(
        self, tmp_path: Path
    ):
        report_path = tmp_path / "report.csv"
        report_path.write_text("Name,Severity\nHub-like Dependency,High\n")
        filtered_path = tmp_path / "filtered_report.csv"
        filtered_path.write_text("Name,Severity,UUID\n")
        smells_path = tmp_path / "smells_to_process.json"
        smells_path.write_text("[]")

        summary = generate_summary(report_path, filtered_path, smells_path)

        assert "**Total smells detected:** 1" in summary
        assert "**Eligible for supported refactoring (before repository filters):** 0" in summary
        assert "**Excluded unsupported smells:** 1" in summary
        assert (
            "**No smells selected** - no detected smell types support automated refactoring"
        ) in summary
        assert "No architectural smells detected" not in summary
        assert "| Smell Type | File | Severity | UUID |" not in summary

    def test_handles_commas_in_description(self, tmp_path: Path):
        """Correctly parses CSV with commas in description field."""
        # This was the root cause of the "Smells by Severity" bug
        report_content = '''Name,File,Line Number,Description,Severity,Snippet,Related Participants
"Cyclic Dependency","src/a.py",10,"Found cycle between modules a, b, and c","Critical","def foo(): pass","[]"
"Redundant Abstractions","src/d.py",20,"Redundant wrapper for x, y","Major","class Bar: pass","[]"
'''
        report_path = tmp_path / "report.csv"
        report_path.write_text(report_content)

        summary = generate_summary(report_path)

        # Should correctly identify severities, not garbage from shifted columns
        assert "| Critical | 1 |" in summary
        assert "| Major | 1 |" in summary
        # Should NOT contain snippet content in severity section
        assert "def foo" not in summary.split("### Smells by Severity")[1].split("###")[0]

    def test_handles_large_csv_fields(self, tmp_path: Path):
        """Handles detector rows whose fields exceed the default csv module limit."""
        report_path = tmp_path / "report.csv"
        report_content = (
            "Name,File,Line Number,Description,Severity,Snippet,Related Participants\n"
            f"\"Cyclic Dependency\",\"src/a.py\",10,\"{'x' * 200000}\",\"Critical\",\"def foo(): pass\",\"[]\"\n"
        )
        report_path.write_text(report_content)

        summary = generate_summary(report_path)

        assert "**Total smells detected:** 1" in summary
        assert "| Critical | 1 |" in summary
