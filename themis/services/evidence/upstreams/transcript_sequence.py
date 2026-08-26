"""NCBI Nucleotide adapter (E-utilities efetch): a RefSeq transcript's mature mRNA sequence.

The exon table alone fixes the frame arithmetic of a skipped exon; where the resulting premature
termination codon falls is a property of the *sequence*, so the splice-outcome prediction reads the
transcript's bases rather than assuming a stop from the frame shift. ``efetch`` serves the versioned
RefSeq accession's FASTA, and the coordinates VariantValidator's exon table uses are positions in
exactly that record.

The returned header accession is checked against the requested one: efetch answers a superseded
version with that version's own sequence, so a silent mismatch would splice against the wrong bases.
"""

from __future__ import annotations

import dataclasses

import httpx

from themis.services.evidence import errors

_EFETCH_URL = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi'
_SOURCE = 'NCBI Nucleotide (E-utilities efetch)'
_DB = 'nuccore'


@dataclasses.dataclass(frozen=True)
class TranscriptSequenceResult:
    """One RefSeq transcript's mature sequence.

    Attributes:
        accession: The versioned accession the record carries (checked against the request).
        description: The FASTA definition line minus the accession.
        sequence: The mature transcript's bases, uppercase, 5'->3'; position n of the n. coordinate
            system is ``sequence[n - 1]``.
        source: Provenance source label.
        dataset_versions: The E-utilities database and the accession version — together, what
            actually pins which bases were returned.
        query: The exact request URL issued, for replay.
    """

    accession: str
    description: str
    sequence: str
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def parse_fasta(text: str, *, accession: str, query: str) -> TranscriptSequenceResult:
    """Parse a single-record FASTA into the transcript's sequence.

    Args:
        text: The efetch FASTA body.
        accession: The versioned accession requested; the record's own must match it.
        query: The exact request URL issued, carried into provenance for replay.

    Returns:
        The parsed `TranscriptSequenceResult`.

    Raises:
        ValueError: If the body is not a FASTA record, carries a different accession, or has no
            bases.
    """
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith('>'):
        raise ValueError(f'efetch returned no FASTA record for {errors.clipped(accession)!r}: {errors.clipped(text)!r}')
    header, _, description = lines[0][1:].partition(' ')
    if header != accession:
        raise ValueError(
            f'efetch returned {errors.clipped(header)!r} for requested accession {errors.clipped(accession)!r}'
        )
    sequence = ''.join(line.strip() for line in lines[1:]).upper()
    if not sequence:
        raise ValueError(f'efetch returned an empty sequence for {errors.clipped(accession)!r}')
    return TranscriptSequenceResult(
        accession=header,
        description=description,
        sequence=sequence,
        source=_SOURCE,
        dataset_versions=(_DB, header),
        query=query,
    )


async def fetch_transcript_sequence(accession: str, *, http_client: httpx.AsyncClient) -> TranscriptSequenceResult:
    """Fetch one versioned RefSeq transcript's mature sequence.

    Args:
        accession: The versioned RefSeq accession (e.g. ``NM_001042492.3``).
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The parsed `TranscriptSequenceResult`.

    Raises:
        errors.UnknownVariantError: If NCBI holds no sequence for the accession (efetch answers a
            400 naming it) — a settled answer, not a fault.
        httpx.HTTPStatusError: For any other non-2xx status. The 4xx that are about the *client* are
            among them: 403 for a blocked one, 429 for a throttled one. Neither is a fact about the
            accession, and a NOT_FOUND is what `SpliceOutcome` would then rest a prediction on.
        ValueError: If the body is not the requested accession's FASTA record.
    """
    params = {'db': _DB, 'id': accession, 'rettype': 'fasta', 'retmode': 'text'}
    response = await http_client.get(_EFETCH_URL, params=params)
    if response.status_code == httpx.codes.BAD_REQUEST:
        raise errors.UnknownVariantError(
            f'NCBI Nucleotide holds no sequence for {errors.clipped(accession)!r} ({response.status_code}): '
            f'{errors.clipped(response.text.strip())}'
        )
    response.raise_for_status()
    return parse_fasta(response.text, accession=accession, query=str(response.request.url))
