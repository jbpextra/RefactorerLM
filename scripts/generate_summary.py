#!/usr/bin/env python3
"""Generate GitHub Actions workflow summary for smell detection."""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from .detector_report import configure_csv_field_limit
from .smell_identity import generate_smell_uuid
from .supported_smells import get_smell_type


def count_by_field(csv_path: Path, field_name: str) -> Counter:
    """
    Count occurrences of each unique value in a CSV field.

    Args:
        csv_path: Path to CSV file
        field_name: Name of the column to count

    Returns:
        Counter with {value: count}
    """
    configure_csv_field_limit()
    counts = Counter()
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            value = row.get(field_name, '').strip()
            if value:
                counts[value] += 1
    return counts


def format_table_with_percentages(counts: Counter, header: str, value_col: str) -> list[str]:
    """
    Format counts as a markdown table with percentages.

    Args:
        counts: Counter with values and their counts
        header: Section header
        value_col: Name for the value column

    Returns:
        List of markdown lines
    """
    lines = []
    total = sum(counts.values())

    if total == 0:
        lines.append(f"### {header}")
        lines.append("")
        lines.append("_No data available_")
        lines.append("")
        return lines

    lines.append(f"### {header}")
    lines.append("")
    lines.append(f"| {value_col} | Count | % |")
    lines.append("|" + "-" * (len(value_col) + 2) + "|-------|-----|")

    # Sort by count descending
    for value, count in counts.most_common():
        pct = round(100 * count / total)
        lines.append(f"| {value} | {count} | {pct}% |")

    lines.append("")
    return lines


def generate_summary(
    report_path: Path,
    filtered_path: Optional[Path] = None,
    smells_json_path: Optional[Path] = None,
    config_path: Optional[Path] = None
) -> str:
    """
    Generate markdown summary for smell detection.

    Args:
        report_path: Path to report.csv (raw detector output)
        filtered_path: Path to filtered_report.csv (optional)
        smells_json_path: Path to smells_to_process.json (optional)
        config_path: Path to refactor_configuration.yml (optional)

    Returns:
        Markdown string
    """
    lines = []

    # Header
    lines.append("## Smell Detection Summary")
    lines.append("")

    # Check if report exists
    if not report_path.exists():
        lines.append("_No smell report found._")
        return "\n".join(lines)

    # Count total smells
    configure_csv_field_limit()
    with open(report_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    total_smells = len(rows)

    if total_smells == 0:
        lines.append("**No architectural smells detected.**")
        return "\n".join(lines)

    lines.append(f"**Total smells detected:** {total_smells}")
    lines.append("")
    supported_count = sum(get_smell_type(row.get("Name")) is not None for row in rows)
    lines.append(
        f"**Eligible for supported refactoring (before repository filters):** {supported_count}"
    )
    lines.append("")
    lines.append(f"**Excluded unsupported smells:** {total_smells - supported_count}")
    lines.append("")

    # Smells by Type
    type_counts = count_by_field(report_path, 'Name')
    lines.extend(format_table_with_percentages(type_counts, "Smells by Type", "Type"))

    # Smells by Severity
    severity_counts = count_by_field(report_path, 'Severity')
    lines.extend(format_table_with_percentages(severity_counts, "Smells by Severity", "Severity"))

    # Selected smells section
    if smells_json_path and smells_json_path.exists():
        with open(smells_json_path, 'r', encoding='utf-8') as f:
            selected_uuids = json.load(f)

        selected_count = len(selected_uuids)
        lines.append("### Smells Selected for Refactoring")
        lines.append("")

        if selected_count > 0:
            pct = round(100 * selected_count / supported_count) if supported_count else 0
            lines.append(
                f"**{selected_count}** of {supported_count} eligible smell(s) selected "
                f"after repository filters and selection limits ({pct}%)"
            )
            lines.append("")

            # Show table of selected smells
            if filtered_path and filtered_path.exists():
                # Reuse the shared UUID derivation so the
                # summary stays consistent with the matrix-selection contract.
                lines.append("| Smell Type | File | Severity | UUID |")
                lines.append("|------------|------|----------|------|")

                configure_csv_field_limit()
                with open(filtered_path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        row_uuid = generate_smell_uuid(row)
                        if row_uuid in selected_uuids:
                            smell_name = row['Name']
                            file_path = row['File']
                            line_num = row['Line Number']
                            severity = row['Severity']
                            short_uuid = row_uuid[:8]

                            # Truncate long file paths
                            if len(file_path) > 40:
                                file_path = '...' + file_path[-37:]

                            lines.append(f"| {smell_name} | `{file_path}:{line_num}` | {severity} | `{short_uuid}...` |")

                lines.append("")
        else:
            if supported_count == 0:
                lines.append(
                    "**No smells selected** - no detected smell types support automated refactoring"
                )
            else:
                lines.append(
                    "**No smells selected** - all eligible smells filtered out "
                    "or already have refactoring branches"
                )
            lines.append("")

        # Show filter configuration if available
        if config_path and config_path.exists():
            try:
                import yaml
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)

                smell_filter = config.get('smell_filter', {})
                if smell_filter.get('filter_enabled', False):
                    filter_parts = []
                    if 'include_names' in smell_filter:
                        names = smell_filter['include_names']
                        filter_parts.append(f"include_names: {names}")
                    if 'include_severities' in smell_filter:
                        severities = smell_filter['include_severities']
                        filter_parts.append(f"include_severities: {severities}")

                    if filter_parts:
                        lines.append(f"> Filtered by: `{', '.join(filter_parts)}`")
                        lines.append("")
            except Exception:
                # Summary generation is best-effort: hide config-display errors
                # instead of failing the entire detect-smells job.
                pass  # Skip filter display if config can't be read

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='Generate GitHub Actions workflow summary for smell detection'
    )
    parser.add_argument(
        '--report-file',
        required=True,
        help='Path to report.csv (raw detector output)'
    )
    parser.add_argument(
        '--filtered-report',
        help='Path to filtered_report.csv'
    )
    parser.add_argument(
        '--smells-file',
        help='Path to smells_to_process.json'
    )
    parser.add_argument(
        '--config-file',
        help='Path to refactor_configuration.yml'
    )

    args = parser.parse_args()

    report_path = Path(args.report_file)
    filtered_path = Path(args.filtered_report) if args.filtered_report else None
    smells_path = Path(args.smells_file) if args.smells_file else None
    config_path = Path(args.config_file) if args.config_file else None

    if not report_path.exists():
        print("## Smell Detection Summary\n", file=sys.stdout)
        print("_No smell report found._", file=sys.stdout)
        return

    summary = generate_summary(report_path, filtered_path, smells_path, config_path)
    print(summary)


if __name__ == '__main__':
    main()
