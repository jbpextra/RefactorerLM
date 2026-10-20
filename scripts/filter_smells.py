#!/usr/bin/env python3
import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from typing import Dict, List, Set, Tuple

from .detector_report import load_report
from .selection_config import load_config
from .smell_identity import generate_smell_uuid, normalize_path, normalize_smell_type
from .supported_smells import get_smell_type

# Write logs to both debug.log for artifacts and stdout for workflow visibility.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("debug.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

DEBUG = logging.DEBUG
INFO = logging.INFO
WARNING = logging.WARNING
ERROR = logging.ERROR

MAX_IMPORTERS = 20


class DSU:
    """
    Group detector-emitted cyclic-dependency rows by participant overlap.

    Use this instead of pairwise filtering so cycles that share any module collapse
    into one representative candidate set.
    https://en.wikipedia.org/wiki/Disjoint-set_data_structure
    """

    def __init__(self):
        self.parent = {}

    def add(self, x):
        if x not in self.parent:
            self.parent[x] = x

    def find(self, x):
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x, y):
        px, py = self.find(x), self.find(y)
        if px != py:
            if px < py:
                self.parent[py] = px
            else:
                self.parent[px] = py


def filter_smells_by_importer_count(
    smells: List[Dict],
    max_importers: int = MAX_IMPORTERS,
) -> List[Dict]:
    """
    Exclude smells whose unique importer count exceeds the allowed threshold.

    The report loader supplies validated, normalized importer paths.
    """
    filtered_smells = []
    skipped = 0

    for smell in smells:
        unique_importer_count = len({
            importer
            for importers in smell["importers"].values()
            for importer in importers
        })

        if unique_importer_count > max_importers:
            row = smell["row"]
            smell_id = row.get("UUID", row.get("File", "unknown"))
            logging.info(
                "Skipping smell %s: unique importer count (%d) exceeds MAX_IMPORTERS (%d)",
                smell_id,
                unique_importer_count,
                max_importers,
            )
            skipped += 1
            continue

        filtered_smells.append(smell)

    logging.info(
        "Importer-count filtering: kept %d of %d smells (skipped %d over limit %d)",
        len(filtered_smells),
        len(smells),
        skipped,
        max_importers,
    )
    return filtered_smells


def severity_rank(severity: str) -> int:
    """Convert severity string to numeric rank (higher = more severe)."""
    return {"high": 2, "medium": 1, "low": 0}.get(severity.lower().strip(), 0)


def _cycle_representative_score(candidate: Dict) -> Tuple[int, int, int, str, str, str, int]:
    """Return the deterministic sort key for selecting a cycle representative."""
    module_count = len(candidate["mods"])
    smell = candidate["smell"]
    row = smell["row"]
    return (
        -severity_rank(row.get("Severity", "")),
        abs(module_count - 3),
        module_count,
        candidate["sig"],
        normalize_path(row.get("File", "")),
        generate_smell_uuid(row, participants=smell["participants"]),
        candidate["row_index"],
    )


def deduplicate_cyclic_dependencies(smells: List[Dict]) -> List[Dict]:
    """
    Deduplicate cyclic dependency smells by participant overlap.

    Rows are grouped when their Related Participants share at least one module.
    One representative per group is selected using deterministic scoring.
    """
    cycles = []
    other_smells = []

    for i, smell in enumerate(smells):
        if get_smell_type(smell["row"].get("Name")) == "cyclic_dependency":
            mods = sorted({normalize_path(p["file_path"]) for p in smell["participants"]})
            if len(mods) >= 2:
                sig = "||".join(mods)
                cycles.append({
                    "smell": smell,
                    "row_index": i,
                    "mods": mods,
                    "sig": sig
                })
            else:
                other_smells.append((i, smell))
        else:
            other_smells.append((i, smell))

    if not cycles:
        return smells

    dsu = DSU()
    for c in cycles:
        for m in c["mods"]:
            dsu.add(m)
        anchor = c["mods"][0]
        for m in c["mods"][1:]:
            dsu.union(anchor, m)

    groups = defaultdict(list)
    for c in cycles:
        root = dsu.find(c["mods"][0])
        groups[root].append(c)

    selected_indices = set()
    for items in groups.values():
        rep = min(items, key=_cycle_representative_score)
        selected_indices.add(rep["row_index"])

    all_items = other_smells + [(c["row_index"], c["smell"]) for c in cycles if c["row_index"] in selected_indices]
    all_items.sort(key=lambda x: x[0])

    logging.info(
        "Cycle deduplication: %d cycle rows -> %d participant-overlap groups -> %d selected",
        len(cycles),
        len(groups),
        len(selected_indices),
    )

    return [item[1] for item in all_items]


def get_existing_smell_uuids(existing_branches: str) -> Set[str]:
    """Extract smell UUIDs from existing refactor branch names."""
    branches = [b.strip() for b in existing_branches.split('\n') if b.strip()]
    existing_smell_uuids = set()
    
    for branch in branches:
        # Keep this aligned with workflow branch names in the form origin/refactor/<uuid>.
        parts = branch.split('/')
        if len(parts) >= 3 and parts[-2] == "refactor":
            existing_smell_uuids.add(parts[-1])
    
    logging.info(f"Found {len(existing_smell_uuids)} existing smell UUIDs with branches")
    return existing_smell_uuids


def deduplicate_smell_identities(smells: List[Dict]) -> List[Dict]:
    """Emit one deterministic row per identity, preventing duplicate matrix jobs."""
    representatives = {}
    for smell in sorted(
        smells,
        key=lambda smell: (
            -severity_rank(smell["row"].get("Severity", "")),
            json.dumps(smell["row"], sort_keys=True),
        ),
    ):
        representatives.setdefault(smell["row"]["UUID"], smell)
    return [representatives[smell_uuid] for smell_uuid in sorted(representatives)]


def filter_supported_smells(smells: List[Dict]) -> List[Dict]:
    """Keep only findings that have an implemented refactoring planner."""
    supported = [smell for smell in smells if get_smell_type(smell["row"].get("Name"))]
    logging.info(
        "Supported-type filtering: %d of %d detected smells eligible for refactoring "
        "(%d unsupported excluded)",
        len(supported),
        len(smells),
        len(smells) - len(supported),
    )
    return supported


def filter_smells_by_config(smells: List[Dict], config: Dict) -> List[Dict]:
    """Filter parsed smells using settings already validated by load_config()."""
    smell_filter_config = config["smell_filter"]
    filter_enabled = smell_filter_config["filter_enabled"]
    
    if not filter_enabled:
        logging.info("Repository filtering is disabled. Keeping all eligible smells.")
        return smells
    
    include_names = {
        normalize_smell_type(name)
        for name in smell_filter_config["include_names"]
    }
    include_severities = {s.lower() for s in smell_filter_config["include_severities"]}
    
    logging.info(f"Filtering smells by name: {include_names if include_names else 'All'}")
    logging.info(f"Filtering smells by severity: {include_severities if include_severities else 'All'}")
    
    filtered_smells = []
    for smell in smells:
        row = smell["row"]
        smell_name = normalize_smell_type(row.get("Name", ""))
        smell_severity = row.get("Severity", "").lower()
        
        name_match = not include_names or smell_name in include_names
        severity_match = not include_severities or smell_severity in include_severities
        
        if name_match and severity_match:
            filtered_smells.append(smell)
    
    logging.info(f"Kept {len(filtered_smells)} out of {len(smells)} smells after configuration filtering")
    return filtered_smells


def select_top_smells(smells: List[Dict], config: Dict) -> List[Dict]:
    """
    Select parsed smells using the validated proposal limit and round-robin ordering.

    This ensures that when limiting proposals, we get a balanced distribution
    across different smell types rather than all smells coming from one type.
    """
    max_proposal_limit = config["max_proposal_limit"]

    if max_proposal_limit <= 0:
        logging.info("Proposal limiting is disabled. Returning all smells.")
        return smells

    if len(smells) <= max_proposal_limit:
        logging.info(f"Smell count ({len(smells)}) is within limit ({max_proposal_limit}). Returning all.")
        return smells

    smells_by_type: Dict[str, List[Dict]] = {}
    for smell in smells:
        smell_name = smell["row"].get("Name", "Unknown")
        smell_type = get_smell_type(smell_name) or smell_name
        if smell_type not in smells_by_type:
            smells_by_type[smell_type] = []
        smells_by_type[smell_type].append(smell)

    severity_order = {"high": 0, "medium": 1, "low": 2}

    for smell_type in smells_by_type:
        smells_by_type[smell_type].sort(
            key=lambda s: (
                severity_order.get(s["row"].get("Severity", "").lower(), 99),
                s["row"].get("UUID", "")
            )
        )

    sorted_types = sorted(smells_by_type.keys())

    selected_smells = []
    type_indices = {t: 0 for t in sorted_types}

    while len(selected_smells) < max_proposal_limit:
        made_progress = False
        for smell_type in sorted_types:
            if len(selected_smells) >= max_proposal_limit:
                break
            idx = type_indices[smell_type]
            if idx < len(smells_by_type[smell_type]):
                selected_smells.append(smells_by_type[smell_type][idx])
                type_indices[smell_type] += 1
                made_progress = True

        if not made_progress:
            break

    logging.info(f"Selected {len(selected_smells)} top smells from {len(smells)} using round-robin selection")

    type_counts = {}
    for smell in selected_smells:
        t = smell["row"].get("Name", "Unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
    logging.info(f"Selected smell distribution: {type_counts}")

    return selected_smells


def main():
    parser = argparse.ArgumentParser(description="Filter detector smells into workflow artifacts")
    parser.add_argument(
        "--report-file",
        required=True,
        help="Detector CSV to filter, usually report.csv",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Refactorer YAML config to apply before proposal selection",
    )
    parser.add_argument(
        "--output-file",
        required=True,
        help="JSON file to write selected smell UUIDs to",
    )
    parser.add_argument(
        "--existing-branches",
        required=True,
        help="Remote branch list used to skip already-processed smells",
    )
    
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        all_smells, fieldnames = load_report(args.report_file)
        logging.info(f"Loaded {len(all_smells)} smells from {args.report_file}")
    except (OSError, ValueError, csv.Error) as e:
        logging.error(f"Selection inputs are invalid: {e}")
        sys.exit(1)
    
    # Eligibility is unconditional and precedes every repository selection rule.
    supported_smells = filter_supported_smells(all_smells)

    # Preserve detector fields but replace any supplied UUID with the stable identity.
    for smell in supported_smells:
        row = smell["row"]
        try:
            row["UUID"] = generate_smell_uuid(row, participants=smell["participants"])
        except ValueError as exc:
            logging.error(
                "Invalid smell identity for %s in %s: %s",
                row.get("Name"), row.get("File"), exc,
            )
            sys.exit(1)

    logging.info(f"Generated UUIDs for {len(supported_smells)} supported smells")

    existing_smell_uuids = get_existing_smell_uuids(args.existing_branches)

    filtered_by_config = filter_smells_by_config(supported_smells, config)
    filtered_by_config = deduplicate_cyclic_dependencies(filtered_by_config)
    filtered_by_config = filter_smells_by_importer_count(filtered_by_config)

    final_smells = []
    
    for smell in filtered_by_config:
        smell_uuid = smell["row"]["UUID"]

        if smell_uuid in existing_smell_uuids:
            logging.info(f"Skipping smell {smell_uuid} as it already has a branch")
            continue
            
        final_smells.append(smell)
    
    logging.info(f"Final list contains {len(final_smells)} smells after filtering existing branches")

    # Apply proposal limits after branch skips so slots are not spent on processed smells.
    final_smells = select_top_smells(deduplicate_smell_identities(final_smells), config)
    final_smell_uuids = [smell["row"]["UUID"] for smell in final_smells]

    # Write the artifact contracts consumed by later workflow jobs.
    if "UUID" not in fieldnames:
        fieldnames.append("UUID")
    with open("filtered_report.csv", 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(smell["row"] for smell in final_smells)
    
    with open(args.output_file, 'w') as f:
        json.dump(final_smell_uuids, f)
    
    with open("smells_detected.txt", 'w') as f:
        f.write("true" if final_smell_uuids else "false")
    
    sys.exit(0)


if __name__ == "__main__":
    main()
