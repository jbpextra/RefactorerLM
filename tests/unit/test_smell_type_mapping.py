from scripts import generate_proposal


def test_planner_mapping_accepts_normalized_smell_label():
    assert (
        generate_proposal.map_smell_to_type("  CYCLIC   Dependency\t")
        == "cyclic_dependency"
    )
