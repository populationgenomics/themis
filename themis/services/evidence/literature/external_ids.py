"""The external-id vocabulary this interface resolves papers by: the schemes, the key check, the key.

No backend imports, so the servicer's request validation and the fixture store's seed parser hold to
one set of schemes without importing a backend between them. A seed under a scheme no request can
name would be a paper the interface holds and nothing reaches — the same silent unreachability a
request under an unknown scheme is refused for.
"""

from __future__ import annotations

from themis.services.evidence.literature import pmids

# Crosswalk keys are `{scheme}:{value}`. Ingestion mints under further schemes (`pii:`, `binhash:`,
# and `bookid:` for a Bookshelf chapter), but only these three are a spelling a caller holds a paper
# by, so only these resolve one here; whether the door should take `bookid:` too is an open question
# (docs/design/literature-evidence-layer.md).
SCHEMES = frozenset({'doi', 'pmid', 'pmcid'})


def is_qualified(external_id: str) -> bool:
    """Whether ``external_id`` is ``{resolvable scheme}:{non-empty value}``.

    Both halves are checked: ``'doi'`` and ``'doi:'`` would otherwise reach the crosswalk as literal
    keys, miss, and come back as an empty doc_id with UNKNOWN_PAPER — reporting a malformed id as the
    settled fact that the store does not hold the paper. The scheme is compared as written, since
    the crosswalk folds the case of a key's value and never of its scheme.
    """
    scheme, _, value = external_id.partition(':')
    return scheme in SCHEMES and bool(value)


def lookup_key(external_id: str) -> str:
    """A qualified id in the spelling the crosswalk lookup takes: the pmid value normalised.

    A caller holds a PMID in spellings the crosswalk's own canonicalisation does not read — a
    `PMID:` prefix inside the value, say — so they normalise here, where a value that is not a PMID
    can also be refused rather than reported as the settled fact that the store does not hold the
    paper. The rest pass as written; the crosswalk canonicalises its own keys on both mint and
    lookup (`themis.litcache.crosswalk.normalise_key`).

    Raises:
        ValueError: A `pmid:` value that is not a PMID.
    """
    scheme, _, value = external_id.partition(':')
    if scheme != 'pmid':
        return external_id
    return f'pmid:{pmids.pmid_key(value)}'
