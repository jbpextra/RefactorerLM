"""Smell identity depends only on type, primary path and participants."""

import json

import pytest

from scripts.smell_identity import generate_smell_uuid, normalize_path, normalize_smell_type


def _row(**changes):
    row = {
        "Name": "Cyclic Dependency",
        "File": "pkg/A.py",
        "Description": "A and B import one another",
        "Line Number": "17",
        "Severity": "High",
        "Related Participants": json.dumps(
            [
                {
                    "element_name": "B",
                    "element_type": "module",
                    "file_path": "pkg/B.py",
                    "role": "dependent",
                },
                {
                    "element_name": "A",
                    "element_type": "module",
                    "file_path": "pkg/A.py",
                    "role": "source",
                },
            ]
        ),
    }
    row.update(changes)
    return row


def _with_participants(participants):
    return _row(**{"Related Participants": json.dumps(participants)})


@pytest.mark.parametrize("preparsed", [False, True])
def test_uuid_is_stable_with_raw_or_parsed_participants(preparsed):
    row = _row()
    kwargs = {"participants": json.loads(row["Related Participants"])} if preparsed else {}
    assert generate_smell_uuid(row, **kwargs) == "45379042-8568-59b9-817e-95c9edc84988"


@pytest.mark.parametrize(
    "changes",
    [
        {"Description": "Reworded after an unrelated edit"},
        {"Line Number": "900"},
        {"Severity": "Low"},
        {"UUID": "a-stale-uuid-from-an-older-report"},
        {"Importers By Participant": '{"pkg/A.py":["new_importer.py"]}'},
        {"Participant Dependency Edges": '[["pkg/B.py","pkg/A.py"]]'},
        {"report_index": 100, "unknown detector metadata": "changed"},
    ],
)
def test_incidental_row_metadata_does_not_change_identity(changes):
    assert generate_smell_uuid(_row(**changes)) == generate_smell_uuid(_row())


def test_participant_roles_extra_metadata_and_order_do_not_change_identity():
    participants = json.loads(_row()["Related Participants"])
    for participant in participants:
        participant["role"] = "new-role"
        participant["line_number"] = 900
        participant["description"] = "changed"
        participant["importers"] = ["another.py"]
    changed = _with_participants(list(reversed(participants)))
    assert generate_smell_uuid(changed) == generate_smell_uuid(_row())


@pytest.mark.parametrize(
    "name",
    ["cyclic dependency", " CYCLIC   Dependency ", "\tCyclic\nDependency\r"],
)
def test_smell_type_whitespace_and_case_are_normalized(name):
    assert generate_smell_uuid(_row(Name=name)) == generate_smell_uuid(_row())
    assert normalize_smell_type(name) == "cyclic dependency"


def test_smell_type_uses_unicode_casefold():
    assert normalize_smell_type(" STRAẞE ") == "strasse"


@pytest.mark.parametrize(
    "path",
    ["./pkg/A.py", "pkg//A.py", "pkg/nested/../A.py", "pkg\\A.py"],
)
def test_equivalent_primary_path_spellings_preserve_identity(path):
    assert normalize_path(path) == "pkg/A.py"
    assert generate_smell_uuid(_row(File=path)) == generate_smell_uuid(_row())


def test_equivalent_participant_paths_preserve_identity():
    participants = json.loads(_row()["Related Participants"])
    participants[0]["file_path"] = "./pkg/nested/../B.py"
    participants[1]["file_path"] = "pkg\\A.py"
    assert generate_smell_uuid(_with_participants(participants)) == (
        generate_smell_uuid(_row())
    )


def test_duplicate_participants_are_a_set_even_when_metadata_differs():
    participants = json.loads(_row()["Related Participants"])
    participants.append(
        {**participants[0], "file_path": "./pkg/B.py", "role": "different"}
    )
    assert generate_smell_uuid(_with_participants(participants)) == (
        generate_smell_uuid(_row())
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"Name": "Potential Redundant Abstractions"},
        {"File": "pkg/C.py"},
        {"File": "pkg/a.py"},
        {"File": "/pkg/A.py"},
    ],
)
def test_meaningful_type_and_primary_path_changes_create_new_identity(changes):
    assert generate_smell_uuid(_row(**changes)) != generate_smell_uuid(_row())


@pytest.mark.parametrize(
    "field,value",
    [
        ("element_name", "C"),
        ("element_name", "b"),
        ("element_type", "class"),
        ("element_type", "Module"),
        ("file_path", "pkg/C.py"),
        ("file_path", "pkg/b.py"),
    ],
)
def test_participant_identity_fields_preserve_meaning_and_case(field, value):
    participants = json.loads(_row()["Related Participants"])
    participants[0][field] = value
    assert generate_smell_uuid(_with_participants(participants)) != (
        generate_smell_uuid(_row())
    )


@pytest.mark.parametrize("change", ["add", "remove"])
def test_participant_set_change_creates_new_identity(change):
    participants = json.loads(_row()["Related Participants"])
    if change == "add":
        participants.append(
            {"element_name": "C", "element_type": "module", "file_path": "pkg/C.py"}
        )
    else:
        participants.pop()
    assert generate_smell_uuid(_with_participants(participants)) != (
        generate_smell_uuid(_row())
    )


def test_fingerprint_delimiters_cannot_collide_between_fields():
    first = _with_participants(
        [{"element_name": "A:B", "element_type": "C", "file_path": "D"}]
    )
    second = _with_participants(
        [{"element_name": "A", "element_type": "B:C", "file_path": "D"}]
    )
    assert generate_smell_uuid(first) != generate_smell_uuid(second)


@pytest.mark.parametrize("payload", [None, "", "[]", "null"])
def test_empty_participant_payloads_share_the_missing_payload_identity(payload):
    row_without_participants = _row()
    del row_without_participants["Related Participants"]
    assert generate_smell_uuid(_row(**{"Related Participants": payload})) == (
        generate_smell_uuid(row_without_participants)
    )


def test_missing_identity_strings_default_to_empty_strings():
    assert generate_smell_uuid({}) == generate_smell_uuid(
        {"Name": "", "File": "", "Related Participants": "[]"}
    )
    assert generate_smell_uuid(_with_participants([{}])) == generate_smell_uuid(
        _with_participants([{"element_name": "", "element_type": "", "file_path": ""}])
    )


@pytest.mark.parametrize(
    "payload",
    ["not json", "{}", '"participant"', "42", "true", "[null]", "[[]]", '["A"]'],
)
def test_malformed_participant_structure_is_rejected(payload):
    with pytest.raises(ValueError):
        generate_smell_uuid(_row(**{"Related Participants": payload}))


@pytest.mark.parametrize("field", ["Name", "File"])
@pytest.mark.parametrize("value", [None, 42, [], {}])
def test_present_row_identity_fields_must_be_strings(field, value):
    with pytest.raises(ValueError):
        generate_smell_uuid(_row(**{field: value}))


@pytest.mark.parametrize("field", ["element_name", "element_type", "file_path"])
@pytest.mark.parametrize("value", [None, 42, [], {}])
def test_present_participant_identity_fields_must_be_strings(field, value):
    with pytest.raises(ValueError):
        generate_smell_uuid(_with_participants([{field: value}]))


def test_path_normalization_is_lexical_and_independent_of_working_directory(
    tmp_path, monkeypatch
):
    before = generate_smell_uuid(_row())
    monkeypatch.chdir(tmp_path)
    assert generate_smell_uuid(_row()) == before
    assert normalize_path("../pkg/A.py") == "../pkg/A.py"
    assert normalize_path("/pkg/A.py") == "/pkg/A.py"
    assert normalize_path("") == ""
