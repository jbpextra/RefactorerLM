"""Validate selection settings before detection or proposal selection starts."""

import argparse
from pathlib import Path
import sys

import yaml


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate keys rather than silently replacing configured limits."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as exc:
                raise ValueError("configuration keys must be scalar values") from exc
            if duplicate:
                raise ValueError(
                    f"duplicate configuration key {key!r} on line {key_node.start_mark.line + 1}"
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _target_directory(value):
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(character in value for character in ("\n", "\r", "\0"))
    ):
        raise ValueError("target_directory must be a nonempty, single-line string")
    return value


def validate_config(data) -> dict:
    """Validate supplied settings and fill only genuinely omitted optional fields."""
    if not isinstance(data, dict):
        raise ValueError("configuration must be a YAML mapping")
    if any(not isinstance(key, str) for key in data):
        raise ValueError("configuration keys must be strings")
    unknown_keys = sorted(
        data.keys() - {"target_directory", "max_proposal_limit", "smell_filter"}
    )
    if unknown_keys:
        raise ValueError(f"unknown configuration keys: {', '.join(unknown_keys)}")

    config = dict(data)
    config["target_directory"] = _target_directory(data.get("target_directory", "."))
    limit = data.get("max_proposal_limit", 0)
    if type(limit) is not int or limit < 0:
        raise ValueError("max_proposal_limit must be a nonnegative integer")
    config["max_proposal_limit"] = limit

    smell_filter = data.get("smell_filter", {})
    if not isinstance(smell_filter, dict):
        raise ValueError("smell_filter must be a mapping")
    if any(not isinstance(key, str) for key in smell_filter):
        raise ValueError("smell_filter keys must be strings")
    unknown_keys = sorted(
        smell_filter.keys() - {"filter_enabled", "include_names", "include_severities"}
    )
    if unknown_keys:
        raise ValueError(f"unknown smell_filter keys: {', '.join(unknown_keys)}")
    smell_filter = dict(smell_filter)
    enabled = smell_filter.get("filter_enabled", False)
    if type(enabled) is not bool:
        raise ValueError("smell_filter.filter_enabled must be a boolean")
    smell_filter["filter_enabled"] = enabled

    for field in ("include_names", "include_severities"):
        values = smell_filter.get(field, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(
                f"smell_filter.{field} must be a list of nonempty strings"
            )
        smell_filter[field] = list(values)
    config["smell_filter"] = smell_filter
    return config


def load_config(path) -> dict:
    """Load a configuration file, failing on unreadable or invalid input."""
    try:
        with Path(path).open(encoding="utf-8") as source:
            data = yaml.load(source, Loader=_UniqueKeyLoader)
        return validate_config(data)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ValueError(
            f"Invalid Refactorer configuration {str(path)!r}: {exc}"
        ) from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--target-directory", default="")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        target_directory = _target_directory(
            args.target_directory or config["target_directory"]
        )
    except ValueError as exc:
        print(f"Configuration validation failed: {exc}", file=sys.stderr)
        return 1
    print(target_directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
