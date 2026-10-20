"""Detector participant parsing accepts valid empty and external inputs."""

import json

from scripts import detector_report


def test_empty_participants_and_external_path_sentinels_are_valid():
    assert detector_report.parse_participants({"Related Participants": "[]"}) == []
    participants = [
        {"element_name": "a", "element_type": "module", "file_path": path}
        for path in ("Unknown", "External Library")
    ]
    assert detector_report.parse_participants({
        "Related Participants": json.dumps(participants),
    }) == participants
