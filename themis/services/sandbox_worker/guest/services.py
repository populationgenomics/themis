"""Ready stubs for the internal Themis services, reached through the sandbox hatch.

Guest-side (sandbox-worker.md §"The guest's world is assembled at build time"): runs inside the postern sandbox,
shipped into the guest rootfs as ``themis.agent.services`` (the Dockerfile remaps it there so the agent's code-mode
snippets import it under that stable name). Each function returns a gRPC stub over a single, lazily-created channel
to the hatch UDS at ``unix:$POSTERN_HATCH``. The trusted worker's hatch injects the session token and forwards to
the real service, so calling code holds no credentials and no service URL — get a stub, keep it, and call it. Only
the allowlisted methods are reachable; anything else is ``PERMISSION_DENIED``.

TODO: generate this module from the proto so the guest stubs stay in lockstep with the hatch allowlist
(``hatch.GUEST_METHODS``) by construction, rather than hand-authoring both.
"""

from __future__ import annotations

import functools
import os

import grpc

from themis.rpc import (
    clinvar_pb2_grpc,
    cspec_pb2_grpc,
    gene_disease_pb2_grpc,
    gnomad_pb2_grpc,
    hello_pb2_grpc,
    mavedb_pb2_grpc,
    splice_pb2_grpc,
    transcript_pb2_grpc,
    variant_pb2_grpc,
    vep_pb2_grpc,
)


@functools.cache
def _channel() -> grpc.Channel:
    return grpc.insecure_channel('unix:' + os.environ['POSTERN_HATCH'])


def hello() -> hello_pb2_grpc.HelloStub:
    """``hello``: echo a note against the Analysis the session is bound to (a connectivity check)."""
    return hello_pb2_grpc.HelloStub(_channel())


def variant() -> variant_pb2_grpc.VariantStub:
    """``variant``: one transcript HGVS in, the canonical allele and its projections out — every other source's key."""
    return variant_pb2_grpc.VariantStub(_channel())


def vep() -> vep_pb2_grpc.VepStub:
    """``vep``: Ensembl's VEP — the routing consequence, the calibrated predictor scores, a colocated snapshot."""
    return vep_pb2_grpc.VepStub(_channel())


def gnomad() -> gnomad_pb2_grpc.GnomadStub:
    """``gnomad``: population allele frequencies and the observation counts POP_FRQ and POP_HMZ rest on."""
    return gnomad_pb2_grpc.GnomadStub(_channel())


def clinvar() -> clinvar_pb2_grpc.ClinVarStub:
    """``clinvar``: the allele's own record and submissions, the gene's classified pool, every record at one c. span."""
    return clinvar_pb2_grpc.ClinVarStub(_channel())


def gene_disease() -> gene_disease_pb2_grpc.GeneDiseaseStub:
    """``gene_disease``: the curated gene-disease entities and gene-scoped signals — ClinGen, GenCC, PanelApp."""
    return gene_disease_pb2_grpc.GeneDiseaseStub(_channel())


def transcript() -> transcript_pb2_grpc.TranscriptStub:
    """``transcript``: the exon table every positional judgement rests on, and the exon-relevance signals over it."""
    return transcript_pb2_grpc.TranscriptStub(_channel())


def splice() -> splice_pb2_grpc.SpliceStub:
    """``splice``: whether a splice site is lost (SpliceAI + Pangolin deltas), and what the transcript becomes."""
    return splice_pb2_grpc.SpliceStub(_channel())


def mavedb() -> mavedb_pb2_grpc.MaveDbStub:
    """``mavedb``: the depositor's calibrated multiplexed-assay result for one variant, the *_FXN input."""
    return mavedb_pb2_grpc.MaveDbStub(_channel())


def cspec() -> cspec_pb2_grpc.CspecStub:
    """``cspec``: the ClinGen Criteria Specification Registry — what an expert panel specified for a gene."""
    return cspec_pb2_grpc.CspecStub(_channel())
