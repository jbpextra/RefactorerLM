#!/usr/bin/env python3
"""Generate enhanced PR descriptions for refactoring PRs."""

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path

from .detector_report import configure_csv_field_limit
from .smell_identity import generate_smell_uuid


def find_smell_in_report(report_file: Path, smell_uuid: str) -> dict:
    """Find smell details in the CSV report by UUID."""
    configure_csv_field_limit()
    with open(report_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Generate UUID for this row (matches filter_smells.py logic)
            row_uuid = generate_smell_uuid(row)
            if row_uuid == smell_uuid:
                row["UUID"] = row_uuid
                return row

    raise ValueError(f"Smell UUID {smell_uuid} not found in report")


def parse_related_participants(participants_json: str) -> list:
    """Parse Related Participants JSON string."""
    try:
        return json.loads(participants_json) if participants_json else []
    except json.JSONDecodeError:
        return []


def _short_action_text(value: object) -> str:
    """Keep prose concise without serializing structured action payloads."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= 240 else text[:237] + "..."


def render_validation_assessment(assessment: dict | None) -> str:
    """Render recorded outcomes without treating skipped checks as successful."""
    lines = ["## Validation Results", ""]
    if assessment is None:
        lines.append("Validation assessment unavailable; no check outcomes are reported.")
        return "\n".join(lines)

    check_labels = {
        "repository_tests": "Repository tests",
        "strategy_policy": "Strategy policy",
        "deletion_safety": "Deletion safety",
        "code_similarity": "Code similarity",
    }
    status_labels = {
        "passed": "Passed",
        "failed": "Failed",
        "skipped": "Skipped",
        "not_applicable": "Not applicable",
        "not_reached": "Not reached",
    }

    if not isinstance(assessment, dict) or set(assessment) != {
        "decision", "reason", "checks"
    }:
        raise ValueError("Invalid validation assessment fields")
    decision, reason, checks = (
        assessment["decision"], assessment["reason"], assessment["checks"]
    )
    if decision not in ("accepted", "rejected"):
        raise ValueError("Invalid validation decision")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("A validation assessment requires a reason")
    if not isinstance(checks, dict) or set(checks) != set(check_labels):
        raise ValueError("A validation assessment must contain all four checks")

    lines.extend(
        [f"**Overall decision:** {decision.capitalize()}", f"**Reason:** {reason}", ""]
    )
    for name, label in check_labels.items():
        result = checks[name]
        if (
            not isinstance(result, dict)
            or not {"status", "reason"} <= result.keys()
            or result.keys() - {"status", "reason", "score", "threshold"}
        ):
            raise ValueError(f"Invalid fields for {name}")
        status, reason = result["status"], result["reason"]
        if not isinstance(status, str) or status not in status_labels:
            raise ValueError(f"Invalid status for {name}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"A result for {name} requires a reason")
        if decision == "accepted" and status in {"failed", "not_reached"}:
            raise ValueError("An accepted assessment cannot contain failed or unreached checks")
        score, threshold = result.get("score"), result.get("threshold")
        for metric in (score, threshold):
            if metric is not None and (
                type(metric) not in (int, float) or not math.isfinite(metric)
            ):
                raise ValueError(f"Metrics for {name} must be finite numbers")
        if score is not None and (
            threshold is None or status not in {"passed", "failed"}
        ):
            raise ValueError("A similarity score requires a threshold and a completed check")
        metrics = []
        if score is not None:
            metrics.append(f"score: {score:.4f}")
        if threshold is not None:
            metrics.append(f"threshold: {threshold:.4f}")
        metrics_text = f" ({'; '.join(metrics)})" if metrics else ""
        lines.append(
            f"- **{label}:** {status_labels[status]}{metrics_text}. {reason}"
        )
    return "\n".join(lines)


def generate_pr_description(
    smell_uuid: str,
    report_file: Path,
    proposal_file: Path,
    refactored_files_file: Path = None,
    validation_assessment_file: Path | None = None,
) -> tuple[str, str]:
    """Generate PR title and description.

    Returns:
        (title, body) tuple of strings
    """
    # Read smell details from CSV
    smell = find_smell_in_report(report_file, smell_uuid)

    # Read refactoring proposal
    with open(proposal_file, 'r', encoding='utf-8') as f:
        proposal = json.load(f)

    # Read refactored files if available
    refactored_files = []
    if refactored_files_file and refactored_files_file.exists():
        with open(refactored_files_file, 'r', encoding='utf-8') as f:
            refactored_files = json.load(f)

    assessment = None
    if validation_assessment_file is not None:
        with validation_assessment_file.open(encoding="utf-8") as f:
            assessment = json.load(f)
        if assessment is None:
            raise ValueError("A validation assessment must be a JSON object")

    # Parse participants
    participants = parse_related_participants(smell.get('Related Participants', '[]'))

    # Generate title
    smell_name = smell['Name']
    strategy = proposal.get('strategy', 'Unknown')
    title = f"Refactor: Fix {smell_name} using {strategy} strategy"

    # Generate body
    body_parts = []

    # Header
    body_parts.append("## Automated Refactoring")
    body_parts.append("")
    body_parts.append("This PR contains automated refactoring changes to address a detected architectural smell.")
    body_parts.append("")

    # Smell details
    body_parts.append("## Smell Details")
    body_parts.append("")
    body_parts.append(f"**Type:** {smell_name}")
    body_parts.append(f"**Severity:** {smell['Severity']}")
    body_parts.append(f"**Description:** {smell['Description']}")
    body_parts.append(f"**Primary Location:** `{smell['File']}:{smell['Line Number']}`")
    body_parts.append(f"**UUID:** `{smell_uuid}`")
    body_parts.append("")

    # Affected files
    if participants:
        body_parts.append("### Affected Files")
        body_parts.append("")
        for p in participants:
            element_type = p.get('element_type', 'unknown')
            element_name = p.get('element_name', 'unknown')
            file_path = p.get('file_path', 'unknown')
            role = p.get('role', '')
            role_str = f" ({role})" if role else ""
            body_parts.append(f"- `{file_path}` - {element_type} `{element_name}`{role_str}")
        body_parts.append("")

    # Refactoring approach
    body_parts.append("## Refactoring Approach")
    body_parts.append("")
    body_parts.append(f"**Strategy:** {strategy}")
    body_parts.append("")
    explanation = proposal.get('explanation', 'No explanation provided')
    body_parts.append(f"**Rationale:** {explanation}")
    body_parts.append("")

    # Render only action prose, never file contents or arbitrary payload fields.
    if proposal.get('actions'):
        body_parts.append("### Actions Planned")
        body_parts.append("")
        for i, action in enumerate(proposal['actions'], 1):
            if isinstance(action, str):
                action_type = action
                description = rationale = ''
            else:
                action_type = action.get(
                    'type', action.get('op', action.get('operation', 'unknown'))
                )
                description = _short_action_text(
                    action.get('description')
                ) or _short_action_text(action.get('details'))
                rationale = _short_action_text(action.get('rationale'))
            body_parts.append(f"{i}. **{action_type}**")
            if description:
                body_parts.append(f"   - {description}")
            if rationale:
                body_parts.append(f"   - **Rationale:** {rationale}")
        body_parts.append("")

    # Files changed summary
    if refactored_files:
        created = [f for f in refactored_files if f['operation'] == 'create']
        modified = [f for f in refactored_files if f['operation'] == 'modify']
        deleted = [f for f in refactored_files if f['operation'] == 'delete']

        body_parts.append("## Changes Made")
        body_parts.append("")

        if created:
            body_parts.append(f"**Created** ({len(created)} file{'s' if len(created) != 1 else ''}):")
            for f in created:
                body_parts.append(f"- `{f['path']}`")
            body_parts.append("")

        if modified:
            body_parts.append(f"**Modified** ({len(modified)} file{'s' if len(modified) != 1 else ''}):")
            for f in modified:
                body_parts.append(f"- `{f['path']}`")
            body_parts.append("")

        if deleted:
            body_parts.append(f"**Deleted** ({len(deleted)} file{'s' if len(deleted) != 1 else ''}):")
            for f in deleted:
                body_parts.append(f"- `{f['path']}`")
            body_parts.append("")

    body_parts.append(render_validation_assessment(assessment))
    body_parts.append("")

    # Review checklist
    body_parts.append("## Review Checklist")
    body_parts.append("")
    body_parts.append("- [ ] Code changes address the architectural smell")
    body_parts.append("- [ ] No unintended side effects introduced")
    body_parts.append("- [ ] Refactoring preserves original functionality")
    body_parts.append("- [ ] Code style is consistent with the project")
    body_parts.append("")

    # Footer
    body_parts.append("---")
    body_parts.append("")
    body_parts.append("_Generated by RefactorerLLM - Automated architectural smell detection and refactoring_")

    body = "\n".join(body_parts)

    return title, body


def main():
    parser = argparse.ArgumentParser(
        description='Generate enhanced PR description for refactoring'
    )
    parser.add_argument('--smell-uuid', required=True, help='Smell UUID')
    parser.add_argument('--report-file', required=True, help='Path to filtered_report.csv')
    parser.add_argument('--proposal-file', required=True, help='Path to proposal.json')
    parser.add_argument('--refactored-files', help='Path to refactored_files.json (optional)')
    parser.add_argument('--validation-assessment', help='Path to validation assessment JSON (optional)')
    parser.add_argument('--output-title', help='Output file for title')
    parser.add_argument('--output-body', help='Output file for body')

    args = parser.parse_args()

    report_file = Path(args.report_file)
    proposal_file = Path(args.proposal_file)
    refactored_files_file = Path(args.refactored_files) if args.refactored_files else None
    validation_assessment_file = (
        Path(args.validation_assessment) if args.validation_assessment else None
    )

    if not report_file.exists():
        print(f"Error: Report file not found: {report_file}", file=sys.stderr)
        sys.exit(1)

    if not proposal_file.exists():
        print(f"Error: Proposal file not found: {proposal_file}", file=sys.stderr)
        sys.exit(1)

    try:
        title, body = generate_pr_description(
            args.smell_uuid,
            report_file,
            proposal_file,
            refactored_files_file,
            validation_assessment_file,
        )

        # Output to files or stdout
        if args.output_title:
            Path(args.output_title).write_text(title, encoding='utf-8')
        else:
            print(f"TITLE: {title}")

        if args.output_body:
            Path(args.output_body).write_text(body, encoding='utf-8')
        else:
            print(f"BODY:\n{body}")

        # Also output in GitHub Actions format if running in CI
        if 'GITHUB_ENV' in os.environ:
            # Multi-line output requires special handling
            # Use heredoc format with unique delimiters
            with open(os.environ['GITHUB_ENV'], 'a') as env_file:
                env_file.write("PR_TITLE<<END_OF_PR_TITLE_5d8f2a\n")
                env_file.write(title)
                env_file.write("\nEND_OF_PR_TITLE_5d8f2a\n")
                env_file.write("PR_BODY<<END_OF_PR_BODY_7c9e3b\n")
                env_file.write(body)
                env_file.write("\nEND_OF_PR_BODY_7c9e3b\n")

    except Exception as e:
        print(f"Error generating PR description: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
