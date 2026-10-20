"""Supported smell name variants share selection helper behavior."""

from scripts import filter_smells


def test_case_variants_share_round_robin_type():
    smells = [
        {"Name": "CYCLIC DEPENDENCY", "Severity": "High", "UUID": "first"},
        {"Name": "Cyclic Dependency", "Severity": "Low", "UUID": "second"},
        {"Name": "Potential Redundant Abstractions", "Severity": "Low", "UUID": "third"},
    ]
    selected = filter_smells.select_top_smells(
        [{"row": row} for row in smells], {"max_proposal_limit": 2}
    )
    assert [smell["row"]["UUID"] for smell in selected] == ["first", "third"]
