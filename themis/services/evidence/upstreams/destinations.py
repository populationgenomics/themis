"""Every third-party host the evidence adapters may dial, and what admits each one.

The exfiltration criterion ([`docs/design/security.md`](../../../../docs/design/security.md) §What
counts as an exfiltration channel) turns on facts about the upstream that no call site can show:
whether our call selects a response or deposits the text, and whether the host takes a parameter it
resolves as a location. Each host's entry below is where that determination is recorded, and the
client built here is what holds the adapters to it — a request to a host with no entry raises rather
than leaving the perimeter.

An entry is not a claim that the operator is careful with our queries. It is the two mechanical
properties: the host answers what we ask and keeps no copy it serves on, and it has no parameter that
sends the request somewhere else. An operator owes us nothing beyond that, and a public archive is
under no duty to screen what it is sent.

Out of scope, deliberately: the gene-disease refresh job's downloads. Its URLs are constants with no
caller input in them, so no untrusted text reaches those hosts, and it follows redirects — a
destination this register could not fix without breaking the download.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx2

# Each value states why the host is admitted: what our calls ask of it, and that it holds nothing it
# would serve on. A host absent from this mapping cannot be reached (`admitting_client`).
_ADMITTED: Mapping[str, str] = {
    'api.mavedb.org': (
        'MaveDB. Looks up published multiplexed-assay scores by variant URN or ClinGen allele id; '
        'submission is a separate authenticated surface we hold no credential for.'
    ),
    'cspec.genome.network': (
        'ClinGen Criteria Specification Registry. Reads rule sets and specification documents by '
        'identifier; the registry is authored through its own UI, not through this API.'
    ),
    'eutils.ncbi.nlm.nih.gov': (
        'NCBI E-utilities. esearch/esummary/efetch over public archives — every endpoint answers, '
        'none accepts a submission. ClinVar submission is a separate credentialed service.'
    ),
    'gnomad.broadinstitute.org': ('gnomAD. A GraphQL endpoint whose schema is queries only; no mutation is exposed.'),
    'grch37.rest.ensembl.org': 'Ensembl REST (GRCh37). Annotation lookups; the REST API is read-only.',
    'rest.ensembl.org': 'Ensembl REST (GRCh38). Annotation lookups; the REST API is read-only.',
    'gtexportal.org': 'GTEx portal API. Median expression and reference-gene reads; no write surface.',
    'pangolin-37-xwkwwwxdwq-uc.a.run.app': (
        'Broad Pangolin (GRCh37). Scores one variant per call and stores nothing. The host is an '
        'auto-generated Cloud Run address, so it names no operator on its face the way the others do.'
    ),
    'pangolin-38-xwkwwwxdwq-uc.a.run.app': (
        'Broad Pangolin (GRCh38). Scores one variant per call and stores nothing. The host is an '
        'auto-generated Cloud Run address, so it names no operator on its face the way the others do.'
    ),
    'spliceai-37-xwkwwwxdwq-uc.a.run.app': (
        'Broad SpliceAI (GRCh37). Scores one variant per call and stores nothing. The host is an '
        'auto-generated Cloud Run address, so it names no operator on its face the way the others do.'
    ),
    'spliceai-38-xwkwwwxdwq-uc.a.run.app': (
        'Broad SpliceAI (GRCh38). Scores one variant per call and stores nothing. The host is an '
        'auto-generated Cloud Run address, so it names no operator on its face the way the others do.'
    ),
    'reg.clinicalgenome.org': (
        'ClinGen Allele Registry. Resolves an HGVS descriptor to a canonical allele; registering an '
        'allele is a PUT we never issue and would need a credential we do not hold.'
    ),
    'rest.variantvalidator.org': (
        'VariantValidator. Validates a descriptor and reports transcript structure; the service '
        'keeps no caller-visible record of a query.'
    ),
    'www.ebi.ac.uk': (
        'EBI — Europe PMC search and the OLS4 ontology API. Both answer queries; deposition to '
        'Europe PMC is a separate authenticated route.'
    ),
    'www.ncbi.nlm.nih.gov': (
        'NCBI LitVar2. Resolves variant entities and finds the literature citing them; read-only.'
    ),
}


class UnadmittedDestinationError(RuntimeError):
    """A request was made to a host with no entry in this module."""


# URL-shaped constants that are identifiers rather than destinations. Naming them keeps the
# completeness check honest without admitting a host nothing dials.
_NOT_DIALLED: Mapping[str, str] = {
    'purl.obolibrary.org': 'The OBO IRI prefix. Percent-encoded into an OLS4 path as a term id, never dialled.',
}


async def _admit(request: httpx2.Request) -> None:
    host = request.url.host
    if host not in _ADMITTED:
        raise UnadmittedDestinationError(
            f'{host!r} is not an admitted evidence upstream; add it to '
            f'{__name__} with the determination that admits it, or route the call elsewhere'
        )


def admitting_client(
    *, timeout: httpx2.Timeout, transport: httpx2.AsyncBaseTransport | None = None
) -> httpx2.AsyncClient:
    """An HTTP client that refuses any host this module does not admit.

    Redirects are not followed: a redirect is the upstream choosing our next destination, which is
    the property the admitted set exists to keep.

    Args:
        timeout: The per-request timeout the caller's upstreams need.
        transport: Replaces the network transport; the admitted set and the log still apply.
    """
    return httpx2.AsyncClient(
        timeout=timeout,
        transport=transport,
        follow_redirects=False,
        event_hooks={'request': [_admit]},
    )


def is_named(host: str) -> bool:
    """Whether the host is accounted for — admitted as a destination, or named as never dialled."""
    return host in _ADMITTED or host in _NOT_DIALLED
