"""Validate branches of a locally copied-back ROOT tree."""
from pathlib import Path

import uproot
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import TestResult


class RootTreeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    file: str = Field(min_length=1)
    tree: str = Field(min_length=1)
    must_have: list[str] = Field(default_factory=list)
    can_have: list[str] = Field(default_factory=list)
    cannot_have: list[str] = Field(default_factory=list)
    no_other_leaves: bool = False

    @field_validator("must_have", "can_have", "cannot_have")
    @classmethod
    def nonempty_names(cls, values: list[str]) -> list[str]:
        if any(not name.strip() for name in values):
            raise ValueError("leaf names must not be empty")
        return values

    @model_validator(mode="after")
    def consistent_leaves(self):
        if (set(self.must_have) | set(self.can_have)) & set(self.cannot_have):
            raise ValueError("a leaf cannot be both allowed and forbidden")
        return self


def root_tree(
    result: TestResult, *, file: str, tree: str, must_have: list[str], can_have: list[str],
    cannot_have: list[str], no_other_leaves: bool,
) -> tuple[bool, str]:
    matches = [item for item in result.copied_back if file in (item.source, item.destination)]
    if len(matches) != 1:
        return False, f"ROOT file {file!r} must identify exactly one copied-back file."
    item = matches[0]
    if item.error or not Path(item.destination).is_file():
        return False, f"Copied-back ROOT file unavailable: {item.error or item.destination}"
    try:
        with uproot.open(item.destination) as root_file:
            if tree not in root_file:
                return False, f"ROOT tree {tree!r} does not exist in {file!r}."
            obj = root_file[tree]
            if not getattr(obj, "classname", "").startswith(("TTree", "ROOT::RNTuple")):
                return False, f"ROOT object {tree!r} is not a tree."
            leaves = set(obj.keys(recursive=True))
    except Exception as error:
        return False, f"Cannot read ROOT tree {tree!r}: {error}"
    missing = set(must_have) - leaves
    forbidden = set(cannot_have) & leaves
    extra = leaves - set(must_have) - set(can_have) if no_other_leaves else set()
    problems = []
    for label, names in [("missing", missing), ("forbidden", forbidden), ("unexpected", extra)]:
        if names:
            problems.append(f"{label}: {', '.join(sorted(names))}")
    return not problems, f"ROOT tree {tree!r}: " + ("; ".join(problems) or "branches match.")
