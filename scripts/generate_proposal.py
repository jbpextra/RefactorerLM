#!/usr/bin/env python3
import argparse
import csv
import json
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

from .detector_report import load_report
from .llm_clients import generate_refactor_plan
from .smell_identity import generate_smell_uuid, normalize_path
from .supported_smells import get_smell_type

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("debug.log"),
        logging.StreamHandler(sys.stdout)
    ]
)


def read_module(path: str) -> Optional[str]:
    """Read file contents, or None if missing/unreadable (logs warning)."""
    if not path:
        return None
    if not os.path.exists(path):
        logging.warning(f"File not found: {path}")
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        logging.warning(f"File unreadable {path}: {e}")
        return None


def extract_smell_details(smell_uuid: str, report_file: str) -> Optional[Dict]:
    """Load validated detector records and find one by its canonical UUID."""
    try:
        smells, _ = load_report(report_file)
        for smell in smells:
            row = smell["row"]
            row_uuid = generate_smell_uuid(row, participants=smell["participants"])
            if row_uuid == smell_uuid:
                row["UUID"] = row_uuid
                logging.info(f"Found smell {smell_uuid} in report")
                return smell
        
        logging.error(f"Smell {smell_uuid} not found in report {report_file}")
        return None
    except (OSError, ValueError, csv.Error) as e:
        logging.error(f"Error reading report file: {e}")
        return None


def extract_modules_for_refactoring(
    smell: Dict,
) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """
    Extracts all modules that might be involved in the refactoring.
    For cyclic dependencies, we need all files in the cycle.
    For unneeded abstractions, we need all redundant abstraction files.
    Also includes importer files (files that import participants) for deletion safety.

    Args:
        smell (Dict): Validated detector record with parsed metadata

    Returns:
        Tuple of (modules dict, selected importers map):
            - modules: Dictionary mapping filenames to their content
            - importers_selected: Map of participant -> list of importers (only those with content loaded)
    """
    modules = {}
    smell_details = smell["row"]

    # Always include the primary file
    primary_file = smell_details.get("File")
    if primary_file and primary_file != "Unknown":
        norm_primary = normalize_path(primary_file)
        if norm_primary:
            content = read_module(norm_primary)
            if content is not None:
                modules[norm_primary] = content

    for participant in smell["participants"]:
        file_path = participant["file_path"]
        if file_path != "Unknown":
            norm_path = normalize_path(file_path)
            if norm_path not in modules:
                content = read_module(norm_path)
                if content is not None:
                    modules[norm_path] = content
                    logging.debug(f"Added related file from participants: {norm_path}")

    # Add importer files for deletion safety
    importers_full = smell["importers"]
    importers_selected: Dict[str, List[str]] = {}

    if importers_full:
        importer_paths = sorted({
            importer
            for importers_list in importers_full.values()
            for importer in importers_list
            if importer and importer not in modules
        })

        loaded_count = 0
        for path in importer_paths:
            content = read_module(path)
            if content is not None:
                modules[path] = content
                loaded_count += 1
                logging.debug(f"Added importer file: {path}")

        if loaded_count:
            logging.info(f"Added {loaded_count} importer files to module context")

        # Build selected map: all importers whose content is in modules
        # (includes both already-loaded participants and newly-loaded importers)
        for participant, full_importers in importers_full.items():
            selected = [i for i in full_importers if i in modules]
            if selected:
                importers_selected[participant] = sorted(selected)

    logging.info(f"Extracted {len(modules)} modules for refactoring")
    return modules, importers_selected


def map_smell_to_type(smell_name: str) -> str:
    """Resolve a supported detector name, raising for unsupported planner input."""
    smell_type = get_smell_type(smell_name)
    if smell_type is not None:
        return smell_type

    raise NotImplementedError(f"Smell type '{smell_name}' is not supported. Please extend the mapping function.")


def construct_prompt_for_smell(smell, modules=None):
    """
    Constructs a context prompt for the LLM based on details of a single smell.
    This is used for both planning and execution stages.

    Args:
        smell (dict): Validated detector record with parsed metadata.
        modules (dict, optional): Dictionary mapping filenames to their content.
                                 If provided, the content of relevant files will be included in the prompt.

    Returns:
        str: A formatted prompt string for the LLM.
    """
    smell_details = smell["row"]
    line_info = f":{smell_details.get('Line Number')}" if smell_details.get('Line Number') else ""
    prompt_lines = [
        f"Architectural Smell Detected:",
        f"  Name: {smell_details.get('Name', 'N/A')}",
        f"  Description: {smell_details.get('Description', 'N/A')}",
        f"  File: {smell_details.get('File', 'N/A')}{line_info}",
        f"  Severity: {smell_details.get('Severity', 'N/A')}"
    ]

    # Track files we've already included to avoid duplicates
    included_files = set()

    if smell["participants"]:
        prompt_lines.append("\nParticipating Files and Elements:")
        for participant in smell["participants"]:
            element_name = participant["element_name"]
            element_type = participant["element_type"]
            role = participant.get("role", "N/A")
            file_path = participant["file_path"]

            participant_info = f"  • {element_name} ({element_type})"
            if file_path != "Unknown":
                participant_info += f" - File: {file_path}"
            participant_info += f"\n    Role: {role}"
            prompt_lines.append(participant_info)

    # Include dependency graph if available
    edges = smell["dependency_edges"]
    if edges:
        prompt_lines.append("\nDependency Graph (A -> B means A imports B):")
        for src, dst in edges:
            prompt_lines.append(f"  {src} -> {dst}")

    # Include importer information
    importers_map = smell["importers"]
    if importers_map:
        prompt_lines.append("\nFiles That Import Each Participant:")
        for participant, importers in sorted(importers_map.items()):
            if importers:
                prompt_lines.append(f"  {participant}:")
                for importer in importers:
                    prompt_lines.append(f"    - {importer}")

    # Add primary file snippet from the smell details first
    primary_file = smell_details.get("File")
    if primary_file and primary_file != "Unknown":
        snippet = smell_details.get("Snippet")
        if snippet:
            prompt_lines.append(
                f"\nPrimary File Snippet:\n```python\n{snippet}\n```"
            )
        else:
            prompt_lines.append(
                "\nPrimary File Snippet:\n```python\n# No snippet available\n```"
            )

    # If modules are provided, add the complete code of each file
    if modules and len(modules) > 0:
        prompt_lines.append("\nComplete Code of All Relevant Files:")

        # Organize the files - always put the primary file first if it exists
        ordered_files: List[str] = []
        if primary_file and primary_file in modules:
            ordered_files.append(primary_file)

        for file_path in modules.keys():
            if file_path not in ordered_files:
                ordered_files.append(file_path)

        for file_path in ordered_files:
            if file_path in included_files:
                continue

            file_content = modules.get(file_path)
            if file_content:
                prompt_lines.append(
                    f"\n--- File: {file_path} ---\n```python\n{file_content}\n```"
                )
                included_files.add(file_path)
    elif not smell_details.get("Snippet"):
        # If no modules were provided and the smell entry had no snippet, add a placeholder
        prompt_lines.append(
            "\nRelevant Code Snippet:\n```python\n# No snippet available\n```"
        )

    prompt = "\n".join(prompt_lines)
    return prompt


def main():
    parser = argparse.ArgumentParser(description="Generate refactoring proposal for a specific smell")
    parser.add_argument("--smell-uuid", required=True, help="UUID of the smell to generate proposal for")
    parser.add_argument("--report-file", required=True, help="Path to the filtered CSV report file")

    args = parser.parse_args()

    # Extract smell details
    smell = extract_smell_details(args.smell_uuid, args.report_file)
    if smell is None:
        sys.exit(1)

    smell_details = smell["row"]
    smell_name = smell_details.get("Name")
    smell_type = map_smell_to_type(smell_name)

    # Extract modules for refactoring
    modules, importers_selected = extract_modules_for_refactoring(
        smell
    )
    if not modules:
        logging.error(f"No modules found for smell {args.smell_uuid}")
        sys.exit(1)

    with open("modules.json", "w") as f:
        json.dump(modules, f, indent=2, sort_keys=True)

    # Generate the prompt using llm_clients
    prompt = construct_prompt_for_smell(smell, modules)

    # Save the prompt for debugging
    with open("prompt.txt", "w") as f:
        f.write(prompt)

    # Generate the refactoring proposal using llm_clients
    logging.info("Requesting refactoring proposal from LLM")
    proposal = generate_refactor_plan(smell_type, prompt)

    # Save the detector's file list for the similarity check.
    proposal["similarity_context"] = {
        "File": smell_details.get("File"),
        "Related Participants": smell_details.get("Related Participants"),
    }

    # Add importers metadata to proposal for execution stage
    proposal["importers_by_participant"] = {
        k: sorted(v) for k, v in sorted(importers_selected.items())
    }

    # Save the proposal for future reference
    with open("proposal.json", "w") as f:
        json.dump(proposal, f, indent=2, sort_keys=True)
    
    if not proposal.get("actions"):
        logging.error("No actions operations in the proposal")
        sys.exit(1)
    
    logging.info(f"Generated proposal with {len(proposal.get('actions', []))} file operations")
    logging.info("Successfully generated refactoring proposal")
    sys.exit(0)


if __name__ == "__main__":
    main()
