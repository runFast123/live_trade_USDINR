"""Adding, removing and re-legging a section.

The operator picks the contracts; nothing here decides which months to roll.
What it does decide is whether a choice may be accepted, and it refuses for
reasons that are about the money rather than about tidiness:

  * a section rolling the pair another section already rolls would leave two
    campaigns each believing they owned the whole thing
  * a second section with no ladder has no cap, so nothing bounds what it would
    sell out of a position the others are also claiming
  * a near leg that expires after its far leg is not a roll forward

Every change is validated as a whole configuration before it is applied, so a
section can never be added in a state the app would refuse to start with.

Nothing here touches Tk.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional

from .config import SECTION_OVERRIDES, ConfigError, RollConfig, section_key


class EditError(ValueError):
    """The change was refused, with a sentence worth showing."""


def as_sections(cfg: RollConfig) -> List[Dict[str, Any]]:
    """The sections as a plain list, inventing one if the file predates them.

    A config with no `sections` is the single-pair arrangement. Adding a second
    roll to it means writing the first one down explicitly first, or it would
    lose its ladder to the one being added.
    """
    if cfg.sections:
        return [dict(entry) for entry in cfg.sections]
    if not (cfg.near_token and cfg.far_token):
        return []
    first = {"name": cfg.section_name(cfg.near_expiry, cfg.far_expiry)
                     or "section 1",
             "near_token": cfg.near_token, "far_token": cfg.far_token,
             "near_expiry": cfg.near_expiry, "far_expiry": cfg.far_expiry}
    if cfg.limit_ladder:
        first["limit_ladder"] = [dict(r) for r in cfg.limit_ladder]
    if cfg.watch_limits:
        first["watch_limits"] = list(cfg.watch_limits)
    return [first]


def describe(near_row: Dict[str, Any], far_row: Dict[str, Any]) -> Dict[str, Any]:
    """A section entry from two rows of the scrip master."""
    near_expiry = str(near_row.get("Expiry") or "")
    far_expiry = str(far_row.get("Expiry") or "")
    return {
        "name": RollConfig.section_name(near_expiry, far_expiry)
                or f"{near_row.get('SecDesc')} into {far_row.get('SecDesc')}",
        "near_token": str(near_row.get("Token") or ""),
        "far_token": str(far_row.get("Token") or ""),
        "near_expiry": near_expiry,
        "far_expiry": far_expiry,
        "limit_ladder": [],
        "watch_limits": [],
    }


def _validated(cfg: RollConfig, sections: List[Dict[str, Any]]) -> RollConfig:
    """A candidate config carrying these sections, or a refusal."""
    candidate = replace(cfg, sections=sections)
    try:
        candidate.validate()
    except ConfigError as exc:
        raise EditError(str(exc).splitlines()[-1].strip(" -")) from exc
    return candidate


def add(cfg: RollConfig, near_row: Dict[str, Any],
        far_row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The section list with this pair added. Raises EditError on a refusal."""
    entry = describe(near_row, far_row)
    if not entry["near_token"] or not entry["far_token"]:
        raise EditError("both legs need a contract")

    key = section_key(entry["near_token"], entry["far_token"])
    existing = as_sections(cfg)
    for other in existing:
        merged = {**{k: getattr(cfg, k) for k in SECTION_OVERRIDES}, **other}
        if section_key(merged["near_token"], merged["far_token"]) == key:
            raise EditError(
                f"{other.get('name') or 'a section'} already rolls that pair. "
                "Two sections on the same contracts would each believe they "
                "owned the whole campaign.")

    if existing:
        # A second section arrives DISABLED, and that is not timidity. It has
        # no ladder yet, and a section without one has no cap on what it would
        # sell out of a position the others are counting on -- so it cannot be
        # allowed to trade before its limits are set. Adding it disabled is
        # what makes the order of operations possible at all: pick the
        # contracts, set the limits, then turn it on deliberately.
        entry["enabled"] = False
        wanted = existing + [entry]
    else:
        wanted = [entry]

    _validated(cfg, wanted)
    return wanted


def remove(cfg: RollConfig, key: str) -> List[Dict[str, Any]]:
    """The section list with this one taken out."""
    existing = as_sections(cfg)
    kept = []
    for other in existing:
        merged = {**{k: getattr(cfg, k) for k in SECTION_OVERRIDES}, **other}
        if section_key(merged["near_token"], merged["far_token"]) != key:
            kept.append(other)
    if len(kept) == len(existing):
        raise EditError("that section is not in the configuration")
    if not kept:
        raise EditError(
            "at least one section has to remain. Change its contracts instead "
            "of removing it.")
    _validated(cfg, kept)
    return kept


def relegs(cfg: RollConfig, key: str, near_row: Dict[str, Any],
           far_row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The section list with one section pointed at a different pair.

    Its ladder settings come with it, but its PROGRESS does not: the state file
    is keyed by the pair, so a section aimed at a new far month starts from
    nothing. That is right, because it is a different roll.
    """
    entry = describe(near_row, far_row)
    existing = as_sections(cfg)
    out, found = [], False
    for other in existing:
        merged = {**{k: getattr(cfg, k) for k in SECTION_OVERRIDES}, **other}
        if section_key(merged["near_token"], merged["far_token"]) == key:
            found = True
            moved = dict(other)
            moved.update({k: entry[k] for k in
                          ("near_token", "far_token", "near_expiry",
                           "far_expiry")})
            moved["name"] = other.get("name") or entry["name"]
            out.append(moved)
        else:
            out.append(other)
    if not found:
        raise EditError("that section is not in the configuration")

    keys = set()
    for other in out:
        merged = {**{k: getattr(cfg, k) for k in SECTION_OVERRIDES}, **other}
        this = section_key(merged["near_token"], merged["far_token"])
        if this in keys:
            raise EditError(
                "that would leave two sections rolling the same pair of "
                "contracts.")
        keys.add(this)

    _validated(cfg, out)
    return out


def enable(cfg: RollConfig, key: str, on: bool = True) -> List[Dict[str, Any]]:
    """Turn a section on or off.

    Turning one ON is where its ladder finally has to exist, because that is
    the moment it could start selling. Turning one OFF is always allowed: it
    stops trading and stops claiming any of the position, which can only make
    the arithmetic easier.
    """
    out, found = [], False
    for other in as_sections(cfg):
        merged = {**{k: getattr(cfg, k) for k in SECTION_OVERRIDES}, **other}
        if section_key(merged["near_token"], merged["far_token"]) == key:
            found = True
            out.append({**other, "enabled": bool(on)})
        else:
            out.append(other)
    if not found:
        raise EditError("that section is not in the configuration")
    _validated(cfg, out)
    return out


def apply(cfg: RollConfig, sections: List[Dict[str, Any]],
          path: Optional[str] = None) -> None:
    """Put a section list into force, and write it down if there is a file."""
    cfg.sections = sections
    if path:
        cfg.save(path)
