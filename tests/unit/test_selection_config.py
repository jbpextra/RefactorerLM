"""Selection configuration validation and YAML parsing."""

import pytest
import yaml

from scripts.selection_config import load_config, validate_config


@pytest.mark.parametrize(
    "data, field",
    [
        (None, "mapping"),
        ([], "mapping"),
        ({"smell_filter": None}, "smell_filter"),
        ({"smell_filter": []}, "smell_filter"),
        ({"smell_filter": {"filter_enabled": "false"}}, "filter_enabled"),
        ({"smell_filter": {"filter_enabled": 0}}, "filter_enabled"),
        ({"smell_filter": {"include_names": "Cyclic Dependency"}}, "include_names"),
        ({"smell_filter": {"include_names": [None]}}, "include_names"),
        ({"smell_filter": {"include_severities": None}}, "include_severities"),
        ({"smell_filter": {"include_severities": [3]}}, "include_severities"),
        ({"max_proposal_limit": None}, "max_proposal_limit"),
        ({"max_proposal_limit": True}, "max_proposal_limit"),
        ({"max_proposal_limit": -1}, "max_proposal_limit"),
        ({"max_proposal_limit": 1.5}, "max_proposal_limit"),
        ({"max_proposal_limit": "5"}, "max_proposal_limit"),
        ({"target_directory": None}, "target_directory"),
        ({"target_directory": []}, "target_directory"),
        ({"target_directory": ""}, "target_directory"),
        ({"target_directory": ".\nother-output=true"}, "target_directory"),
    ],
)
def test_configuration_rejects_invalid_field_types(data, field):
    with pytest.raises(ValueError, match=field):
        validate_config(data)


@pytest.mark.parametrize(
    "data, unknown_key",
    [
        ({"max_proposal_limits": 2}, "max_proposal_limits"),
        ({"smell_filters": {"filter_enabled": True}}, "smell_filters"),
        ({"target_directories": "."}, "target_directories"),
        ({"smell_filter": {"filter_enable": True}}, "filter_enable"),
        ({"smell_filter": {"include_name": []}}, "include_name"),
        ({"smell_filter": {"include_severity": []}}, "include_severity"),
    ],
)
def test_configuration_rejects_unknown_keys(tmp_path, data, unknown_key):
    config_path = tmp_path / "config.yml"
    config_path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match=f"unknown .* keys: {unknown_key}"):
        load_config(config_path)


def test_configuration_defaults_apply_only_to_omitted_fields(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("{}\n")
    assert load_config(config_path) == {
        "target_directory": ".",
        "max_proposal_limit": 0,
        "smell_filter": {
            "filter_enabled": False,
            "include_names": [],
            "include_severities": [],
        },
    }
    supplied = {
        "target_directory": "nested project",
        "max_proposal_limit": 0,
        "smell_filter": {
            "filter_enabled": True,
            "include_names": [],
            "include_severities": [],
        },
    }
    assert validate_config(supplied) == supplied


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "[",
        "max_proposal_limit: !!int nonsense\n",
        "max_proposal_limit: 5\nmax_proposal_limit: 0\n",
        "smell_filter: {filter_enabled: true, filter_enabled: false}",
        "smell_filter: {<<: {filter_enabled: true}, filter_enabled: false}",
    ],
)
def test_load_configuration_rejects_invalid_yaml(tmp_path, contents):
    config_path = tmp_path / "config.yml"
    config_path.write_text(contents)
    with pytest.raises(ValueError, match="Invalid Refactorer configuration") as error:
        load_config(config_path)
    assert str(config_path) in str(error.value)


def test_load_configuration_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError, match="No such file"):
        load_config(tmp_path / "missing.yml")


def test_yaml_aliases_and_nonoverlapping_merges_remain_valid(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "smell_filter:\n"
        "  <<: {filter_enabled: true}\n"
        "  include_names: &empty []\n"
        "  include_severities: *empty\n"
    )
    assert load_config(config_path)["smell_filter"] == {
        "filter_enabled": True,
        "include_names": [],
        "include_severities": [],
    }


def test_config_loader_does_not_change_other_yaml_readers(tmp_path):
    config_path = tmp_path / "config.yml"
    duplicate_keys = "max_proposal_limit: 5\nmax_proposal_limit: 0\n"
    config_path.write_text(duplicate_keys)
    with pytest.raises(ValueError, match="duplicate configuration key"):
        load_config(config_path)
    assert yaml.safe_load(duplicate_keys) == {"max_proposal_limit": 0}
