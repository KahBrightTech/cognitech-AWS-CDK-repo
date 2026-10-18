"""Repository-relative YAML/JSON documents with Terraform-style placeholders."""

from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

import yaml

PLACEHOLDER = re.compile(r"\[\[([A-Za-z0-9_:-]+)\]\]")


def repository_path(raw: str, label: str, *, base_dir: Path | None) -> Path:
    if (
        not raw.strip() or "\0" in raw or "://" in raw
        or Path(raw).is_absolute() or PureWindowsPath(raw).anchor
    ):
        raise ValueError(f"{label}: file must be a local relative path.")
    if base_dir is None:
        raise ValueError(f"{label}: base_dir is required to resolve '{raw}'.")
    root = base_dir.resolve()
    path = (root / raw).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"{label}: file path must remain under the repository root.")
    return path


def load_document(raw: Any, label: str, *, base_dir: Path | None) -> Any:
    """Return an embedded document unchanged, or safely load a relative file."""
    if not isinstance(raw, str):
        return raw
    path = repository_path(raw, label, base_dir=base_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"{label}, file '{path}': cannot read file: {error}") from error
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"{label}, file '{path}': invalid YAML/JSON: {error}") from error


def load_text(raw: str, label: str, *, base_dir: Path | None) -> str:
    path = repository_path(raw, label, base_dir=base_dir)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"{label}, file '{path}': cannot read file: {error}") from error


def placeholders(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(PLACEHOLDER.findall(value))
    if isinstance(value, Mapping):
        return set().union(*(placeholders(key) | placeholders(item) for key, item in value.items()))
    if isinstance(value, list):
        return set().union(*(placeholders(item) for item in value))
    return set()


def render(value: Any, values: Mapping[str, Any], label: str) -> Any:
    """Replace every `[[name]]`; unresolved placeholders are configuration errors."""
    missing = sorted(placeholders(value) - set(values))
    if missing:
        raise ValueError(f"{label}: unresolved placeholders: {', '.join(missing)}.")

    def text(item: str) -> Any:
        whole = PLACEHOLDER.fullmatch(item)
        if whole:
            return values[whole.group(1)]
        return PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), item)

    if isinstance(value, str):
        return text(value)
    if isinstance(value, Mapping):
        return {render(key, values, label): render(item, values, label) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, values, label) for item in value]
    return value


def json_compatible(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}: must contain JSON-compatible values: {error}") from error
