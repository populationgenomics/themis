"""Seeding an evidence interface's offline backend from its `THEMIS_<INTERFACE>_FIXTURE` env var.

One shape for all of them: a JSON object of sections, one per port method, each a map from that
method's lookup key to the response's JSON form (`json_format.ParseDict`; a `raw` Struct is a nested
JSON object). An interface with one rpc still names its section, so adding a second changes no
caller's seed.

Required, never defaulted: an unset var is an operator error, and `{}` is how a deliberately empty
store is spelled. A lookup against an unseeded key raises rather than returning an empty response —
"nothing was seeded for this variant" and "this variant has no record" are different answers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from google.protobuf import json_format, message

from themis.services.evidence import errors


def sections_from_json(raw: str | None, *, var_name: str, sections: frozenset[str]) -> Mapping[str, object]:
    """Parse a fixture var into its per-method sections, or `SystemExit`.

    Args:
        raw: The env var's value.
        var_name: Its name, for the messages.
        sections: The section names this interface's ports accept.

    Returns:
        The parsed object, with every key in `sections`. A section the seed omits is absent.

    Raises:
        SystemExit: Unset, not valid JSON, not an object, or naming a section that does not exist.
    """
    if raw is None:
        raise SystemExit(
            f'{var_name} is required for the fixture backend: a JSON object of per-rpc '
            '{key: response} sections, or "{}" for an explicit empty store'
        )
    try:
        seeds = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f'{var_name} is not valid JSON: {e}') from e
    if not isinstance(seeds, dict):
        raise SystemExit(f'{var_name} must be a JSON object of per-rpc sections, got {type(seeds).__name__}')
    unknown = seeds.keys() - sections
    if unknown:
        raise SystemExit(f'{var_name} has unknown section(s) {sorted(unknown)}; expected {sorted(sections)}')
    return seeds


def table[M: message.Message](
    seeds: Mapping[str, object], section: str, message_type: type[M], *, var_name: str
) -> dict[str, M]:
    """One section's `{key: response}` table, parsed onto `message_type`.

    Raises:
        SystemExit: The section is not an object, or an entry is not a valid `message_type`.
    """
    raw_section = seeds.get(section)
    if raw_section is None:
        return {}
    if not isinstance(raw_section, dict):
        raise SystemExit(f'{var_name} section {section!r} must be a JSON object of key -> response')
    parsed: dict[str, M] = {}
    for key, value in raw_section.items():
        if not isinstance(value, dict):
            raise SystemExit(f'{var_name} section {section!r} entry {key!r} must be a JSON object of response fields')
        try:
            parsed[key] = json_format.ParseDict(value, message_type())
        except json_format.ParseError as e:
            raise SystemExit(f'{var_name} section {section!r} entry {key!r} is malformed: {e}') from e
    return parsed


def lookup[M: message.Message](table: Mapping[str, M], key: str, *, kind: str) -> M:
    """The seeded response for `key`.

    Raises:
        errors.UnknownVariantError: Nothing was seeded under it.
    """
    try:
        return table[key]
    except KeyError:
        raise errors.UnknownVariantError(f'no {kind} response seeded for key {key!r}') from None
