"""Unit tests for summary counting and formatting helpers."""

from collections import Counter
from pathlib import Path

from scripts.generate_summary import count_by_field, format_table_with_percentages


class TestCountByField:
    """Tests for count_by_field function."""

    def test_counts_simple_csv(self, tmp_path: Path):
        """Basic counting works."""
        csv_content = """Name,Severity
Cyclic Dependency,Critical
Cyclic Dependency,Major
Redundant Abstractions,Critical
"""
        csv_path = tmp_path / "report.csv"
        csv_path.write_text(csv_content)

        counts = count_by_field(csv_path, "Severity")
        assert counts["Critical"] == 2
        assert counts["Major"] == 1

    def test_handles_commas_in_quoted_fields(self, tmp_path: Path):
        """Properly parses CSV with commas inside quoted fields."""
        # This is the bug that broke shell-based parsing
        csv_content = '''Name,Description,Severity
"Cyclic Dependency","Cycle between a, b, and c modules","Critical"
"Redundant Abstractions","Found in foo, bar","Major"
'''
        csv_path = tmp_path / "report.csv"
        csv_path.write_text(csv_content)

        severity_counts = count_by_field(csv_path, "Severity")
        assert severity_counts["Critical"] == 1
        assert severity_counts["Major"] == 1
        assert len(severity_counts) == 2

    def test_handles_empty_csv(self, tmp_path: Path):
        """Returns empty counter for CSV with only headers."""
        csv_content = "Name,Severity\n"
        csv_path = tmp_path / "report.csv"
        csv_path.write_text(csv_content)

        counts = count_by_field(csv_path, "Severity")
        assert len(counts) == 0

    def test_handles_missing_field(self, tmp_path: Path):
        """Gracefully handles missing field name."""
        csv_content = """Name,Severity
Cyclic,Critical
"""
        csv_path = tmp_path / "report.csv"
        csv_path.write_text(csv_content)

        counts = count_by_field(csv_path, "NonExistentField")
        assert len(counts) == 0


class TestFormatTableWithPercentages:
    """Tests for format_table_with_percentages function."""

    def test_formats_basic_table(self):
        """Generates markdown table with counts and percentages."""
        counts = Counter({"Critical": 3, "Major": 7})
        lines = format_table_with_percentages(counts, "By Severity", "Severity")

        output = "\n".join(lines)
        assert "### By Severity" in output
        assert "| Severity | Count | % |" in output
        assert "| Major | 7 | 70% |" in output
        assert "| Critical | 3 | 30% |" in output

    def test_handles_empty_counter(self):
        """Shows 'no data' for empty counter."""
        counts = Counter()
        lines = format_table_with_percentages(counts, "Empty", "Value")

        output = "\n".join(lines)
        assert "_No data available_" in output

    def test_sorts_by_count_descending(self):
        """Items sorted by count, highest first."""
        counts = Counter({"A": 1, "B": 5, "C": 3})
        lines = format_table_with_percentages(counts, "Test", "Item")

        output = "\n".join(lines)
        # B (5) should come before C (3) should come before A (1)
        b_pos = output.find("| B |")
        c_pos = output.find("| C |")
        a_pos = output.find("| A |")
        assert b_pos < c_pos < a_pos
