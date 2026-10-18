"""Small validators shared by the YAML-backed props classes."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, TypeVar

T = TypeVar("T")

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def mapping(
    value: Any, label: str, keys: Iterable[str], *, required: Iterable[str] = ()
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping.")
    allowed = set(keys)
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise ValueError(f"{label}: unknown fields: {', '.join(unknown)}.")
    missing = [key for key in required if value.get(key) in (None, "")]
    if missing:
        raise ValueError(f"{label}: missing required fields: {', '.join(missing)}.")
    return value


def name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError(
            f"{label} must start with a letter or digit and use letters, "
            "digits, '-' or '_'."
        )
    return value


def string(
    value: Any, label: str, *, default: str | None = None, allow_empty: bool = False
) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{label} must be a nonempty string.")
    return value


def required_string(value: Any, label: str) -> str:
    result = string(value, label)
    if result is None:
        raise ValueError(f"{label} is required.")
    return result


def strings(value: Any, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if value is None:
        return ()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or (not allow_empty and not values) or any(
        not isinstance(item, str) or not item.strip() for item in values
    ):
        raise ValueError(f"{label} must be a string or a list of nonempty strings.")
    return tuple(values)


def boolean(value: Any, label: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false.")
    return value


def integer(
    value: Any,
    label: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be at most {maximum}.")
    return value


def string_map(value: Any, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not key or not isinstance(item, (str, int, float, bool))
        for key, item in value.items()
    ):
        raise ValueError(f"{label} must map strings to scalar values.")
    return {key: str(item).lower() if isinstance(item, bool) else str(item) for key, item in value.items()}


def items(
    value: Any,
    label: str,
    parse: Callable[[Any], T],
    *,
    key: Callable[[T], str] = lambda item: getattr(item, "name"),
) -> tuple[T, ...]:
    """Parse an optional YAML list and reject duplicate keys case-insensitively."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    parsed = tuple(parse(item) for item in value)
    seen: set[str] = set()
    for item in parsed:
        item_key = key(item).casefold()
        if item_key in seen:
            raise ValueError(f"{label}: duplicate name '{key(item)}'.")
        seen.add(item_key)
    return parsed


def lookup(values: Mapping[str, T], key: str, label: str) -> T:
    try:
        return values[key]
    except KeyError:
        known = ", ".join(sorted(values)) or "none"
        raise ValueError(f"{label}: unknown reference '{key}'. Configured: {known}.") from None
