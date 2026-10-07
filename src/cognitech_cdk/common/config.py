"""Configuration shared by every stack and construct.

`Common` mirrors the `common` variable in the Terraform modules, so resources
built here follow the same naming convention as the Terraform estate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# Suffix per availability zone, matching the Terraform modules.
ORDINALS = ("primary", "secondary", "tertiary", "quaternary")
MAX_AZS = len(ORDINALS)

# Tags every resource must carry. `Name` is excluded: it is built per resource.
REQUIRED_TAGS = ("Build-method", "Environment", "ManagedBy", "Owner", "Compliance")


def ordinal(index: int) -> str:
    """`primary`, `secondary`, `tertiary`, `quaternary` for an AZ position."""
    return ORDINALS[index]


@dataclass(frozen=True)
class Common:
    """Values every resource needs. Mirrors the Terraform `common` object.

    ```hcl
    type = object({
      global           = bool
      tags             = map(string)
      account_name     = string
      region_prefix    = string
      account_name_abr = optional(string, "")
      environment_abr  = optional(string, "")
    })
    ```
    """

    account_name: str
    region_prefix: str
    tags: Mapping[str, str] = field(default_factory=dict)
    global_: bool = False
    account_name_abr: str = ""
    environment_abr: str = ""

    def name(self, *parts: str) -> str:
        """`{account_name}-{region_prefix}-{parts...}`, skipping empty parts.

        Matches the Terraform convention, e.g.
        `int-production-use1-uat-vpc`.
        """
        return "-".join([self.account_name, self.region_prefix, *(p for p in parts if p)])

    def abbreviated_name(self, *parts: str) -> str:
        """Shorter form for resources with a length cap (ALBs, target groups).

        Falls back to the full `account_name` when no abbreviation is set.
        """
        account = self.account_name_abr or self.account_name
        return "-".join([account, self.region_prefix, *(p for p in parts if p)])

    @classmethod
    def from_dict(cls, raw: Mapping[str, object], env_name: str) -> "Common":
        tags = {str(k): str(v) for k, v in (raw.get("tags") or {}).items()}

        if "Name" in tags:
            raise ValueError(
                f"{env_name}: 'Name' must not be a common tag; it is built per resource."
            )
        missing = [key for key in REQUIRED_TAGS if not tags.get(key)]
        if missing:
            raise ValueError(f"{env_name}: common.tags is missing {', '.join(missing)}.")

        return cls(
            account_name=str(raw["account_name"]),
            region_prefix=str(raw["region_prefix"]),
            tags=tags,
            global_=bool(raw.get("global", False)),
            account_name_abr=str(raw.get("account_name_abr", "") or ""),
            environment_abr=str(raw.get("environment_abr", "") or ""),
        )
