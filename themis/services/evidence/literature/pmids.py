"""PMID normalisation and the batch ceiling.

A caller holds a PMID as whatever spelling its source wrote — bare digits, a ``PMID:`` prefix,
zero-padded — and those spell one identifier. Everything in the evidence service that keys a lookup
or validates a request against a PMID goes through ``pmid_key``, so one reader's spelling cannot
miss another's record.
"""

from __future__ import annotations

import re

# One identifier in the spellings a caller holds it in: bare digits, a `PMID:` prefix, zero-padded.
_PMID = re.compile(r'\A(?:pmid\s*:?\s*)?0*([1-9][0-9]*)\Z', re.IGNORECASE)

# The most distinct PMIDs one FetchAbstracts answers. A batch is answered whole or refused, so this
# bounds the upstream bibliographic lookups behind a single request.
MAX_PMIDS_PER_BATCH = 50


def pmid_key(pmid: str) -> str:
    """A PubMed identifier in the one form a lookup uses: its digits, unprefixed and unpadded.

    A caller holds a PMID as whatever source it read the identifier from wrote it — bare digits, a
    ``PMID:`` prefix, zero-padded — and those spell one identifier. Anything else is refused rather
    than looked up: a malformed key reaches no paper and would come back as a fact about the corpus.

    Args:
        pmid: The identifier as supplied; surrounding whitespace is not part of it.

    Returns:
        The identifier's digits, without any prefix or leading zeros.

    Raises:
        ValueError: ``pmid`` is not a PubMed identifier in any of those spellings.
    """
    match = _PMID.match(pmid.strip())
    if match is None:
        raise ValueError(f'pmid {pmid!r} is not a PubMed identifier (digits, optionally "PMID:"-prefixed)')
    return match.group(1)
