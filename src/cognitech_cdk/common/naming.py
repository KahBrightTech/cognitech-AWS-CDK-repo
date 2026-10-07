"""Naming helpers that mirror the Terraform module naming convention."""

from __future__ import annotations

ORDINALS = ("primary", "secondary", "tertiary", "quaternary", "quinary", "senary")


def ordinal(index: int) -> str:
    """Return `primary`, `secondary`, ... falling back to `az<n>` past the table."""
    if index < len(ORDINALS):
        return ORDINALS[index]
    return f"az{index + 1}"
