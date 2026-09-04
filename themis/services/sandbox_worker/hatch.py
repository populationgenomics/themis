"""The gRPC hatch's forwarders: the guest's allowlisted door to the internal services.

Each forwarding servicer runs in the trusted worker process (sandbox-worker.md §"The hatch is the capability
boundary"): it injects the per-session token and forwards an allowlisted call to the real service, so the guest
never holds a credential and never names an upstream. The hatch server is synchronous (postern's ``grpc.server``),
so the forward stubs dial over synchronous channels — distinct from the worker's own async checkpoint channel.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import override

import grpc
from google.protobuf import message
from postern import grpc as postern_grpc

from themis.rpc import (
    clinvar_pb2,
    clinvar_pb2_grpc,
    cspec_pb2,
    cspec_pb2_grpc,
    gene_disease_pb2,
    gene_disease_pb2_grpc,
    gnomad_pb2,
    gnomad_pb2_grpc,
    hello_pb2,
    hello_pb2_grpc,
    literature_pb2,
    literature_pb2_grpc,
    mavedb_pb2,
    mavedb_pb2_grpc,
    splice_pb2,
    splice_pb2_grpc,
    transcript_pb2,
    transcript_pb2_grpc,
    variant_pb2,
    variant_pb2_grpc,
    vep_pb2,
    vep_pb2_grpc,
)
from themis.services.sandbox_worker import _generated

_SESSION_TOKEN_METADATA = 'x-themis-session-token'  # noqa: S105 — a metadata key name, not a secret

# `time_remaining()` reports an absent caller deadline as the remainder of an int64-nanosecond one rather than as
# an absence, so the bound has to be a cap and not a fallback. Held under `worker._TOOL_TIMEOUT_S`
# (sandbox-rpc-exposure.md, "Forwarder model").
_FORWARD_CEILING_S = 90.0

# Bounds the hatch's serving threads, and with them how many forwarded calls one guest can hold open at once.
_HATCH_WORKERS = 8

# The codes whose `details` only an upstream servicer writes. grpc synthesises the text under any other code, and a
# channel-level failure's synthesised text names the resolved upstream. UNAUTHENTICATED and PERMISSION_DENIED stay
# out although a servicer sets them too: Cloud Run and the ID-token plugin set them as well, naming the audience.
_SERVICER_AUTHORED_CODES = frozenset(
    {
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.FAILED_PRECONDITION,
    }
)

# The methods the guest may reach, generated from the `agent_exposed` proto option (sandbox-rpc-exposure.md):
# whichever rpcs carry it. store and auth carry no option, so the worker-only surface stays off the hatch.
GUEST_METHODS = _generated.GUEST_METHODS


def _forward[Request: message.Message, Response: message.Message](
    stub_method: grpc.UnaryUnaryMultiCallable[Request, Response],
    request: Request,
    metadata: tuple[tuple[str, str], ...],
    context: grpc.ServicerContext,
) -> Response:
    """Forward one call under the caller's own budget, capped, restating a settled failure under its own code.

    An ``RpcError`` left to escape a servicer reaches the guest as UNKNOWN, so a settled NOT_FOUND would read as
    a fault worth retrying rather than as the answer it is. The status crosses; the diagnostic does not — only a
    code the upstream contract sets carries its ``details`` through, so the guest never learns an upstream from
    text grpc synthesised.

    Raises:
        grpc.RpcError: Always, for a failed upstream call — either the original, when it carries no status, or
            whatever ``context.abort`` raises to end the rpc under the upstream's own code.
    """
    try:
        return stub_method(request, metadata=metadata, timeout=min(context.time_remaining(), _FORWARD_CEILING_S))
    except grpc.RpcError as error:
        if not isinstance(error, grpc.Call):
            raise
        code = error.code()
        details = error.details() if code in _SERVICER_AUTHORED_CODES else None
        context.abort(code, details or code.name)


class _Forwarder[Stub]:
    """The half of a forwarder that is not its rpcs: one upstream stub, and the identity every call carries.

    Subclasses pass their generated stub class through, so the metadata line that decides what identity a
    forwarded call presents is authored once rather than once per service.
    """

    def __init__(
        self, stub_class: Callable[[grpc.Channel], Stub], channel: grpc.Channel, *, session_token: str
    ) -> None:
        self._stub = stub_class(channel)
        self._metadata = ((_SESSION_TOKEN_METADATA, session_token),)


class HelloForwarder(_Forwarder[hello_pb2_grpc.HelloStub], hello_pb2_grpc.HelloServicer):
    """Forward the guest's allowlisted hello call to the real hello service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        super().__init__(hello_pb2_grpc.HelloStub, channel, session_token=session_token)

    @override
    def SayHello(self, request: hello_pb2.SayHelloRequest, context: grpc.ServicerContext) -> hello_pb2.SayHelloResponse:
        return _forward(self._stub.SayHello, request, self._metadata, context)


class VariantForwarder(_Forwarder[variant_pb2_grpc.VariantStub], variant_pb2_grpc.VariantServicer):
    """Forward the guest's allowlisted variant call to the evidence service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        super().__init__(variant_pb2_grpc.VariantStub, channel, session_token=session_token)

    @override
    def Normalize(
        self, request: variant_pb2.NormalizeRequest, context: grpc.ServicerContext
    ) -> variant_pb2.NormalizeResponse:
        return _forward(self._stub.Normalize, request, self._metadata, context)


class VepForwarder(_Forwarder[vep_pb2_grpc.VepStub], vep_pb2_grpc.VepServicer):
    """Forward the guest's allowlisted vep call to the evidence service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        super().__init__(vep_pb2_grpc.VepStub, channel, session_token=session_token)

    @override
    def Annotate(self, request: vep_pb2.AnnotateRequest, context: grpc.ServicerContext) -> vep_pb2.AnnotateResponse:
        return _forward(self._stub.Annotate, request, self._metadata, context)


class GnomadForwarder(_Forwarder[gnomad_pb2_grpc.GnomadStub], gnomad_pb2_grpc.GnomadServicer):
    """Forward the guest's allowlisted gnomad call to the evidence service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        super().__init__(gnomad_pb2_grpc.GnomadStub, channel, session_token=session_token)

    @override
    def DescribeVariant(
        self, request: gnomad_pb2.DescribeVariantRequest, context: grpc.ServicerContext
    ) -> gnomad_pb2.DescribeVariantResponse:
        return _forward(self._stub.DescribeVariant, request, self._metadata, context)


class ClinVarForwarder(_Forwarder[clinvar_pb2_grpc.ClinVarStub], clinvar_pb2_grpc.ClinVarServicer):
    """Forward the guest's allowlisted clinvar calls to the evidence service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        super().__init__(clinvar_pb2_grpc.ClinVarStub, channel, session_token=session_token)

    @override
    def DescribeVariant(
        self, request: clinvar_pb2.DescribeVariantRequest, context: grpc.ServicerContext
    ) -> clinvar_pb2.DescribeVariantResponse:
        return _forward(self._stub.DescribeVariant, request, self._metadata, context)

    @override
    def SearchCodingSpan(
        self, request: clinvar_pb2.SearchCodingSpanRequest, context: grpc.ServicerContext
    ) -> clinvar_pb2.SearchCodingSpanResponse:
        return _forward(self._stub.SearchCodingSpan, request, self._metadata, context)


class GeneDiseaseForwarder(
    _Forwarder[gene_disease_pb2_grpc.GeneDiseaseStub], gene_disease_pb2_grpc.GeneDiseaseServicer
):
    """Forward the guest's allowlisted gene-disease call to the evidence service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        super().__init__(gene_disease_pb2_grpc.GeneDiseaseStub, channel, session_token=session_token)

    @override
    def DescribeGene(
        self, request: gene_disease_pb2.DescribeGeneRequest, context: grpc.ServicerContext
    ) -> gene_disease_pb2.DescribeGeneResponse:
        return _forward(self._stub.DescribeGene, request, self._metadata, context)


class TranscriptForwarder(_Forwarder[transcript_pb2_grpc.TranscriptStub], transcript_pb2_grpc.TranscriptServicer):
    """Forward the guest's allowlisted transcript calls to the evidence service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        super().__init__(transcript_pb2_grpc.TranscriptStub, channel, session_token=session_token)

    @override
    def GetStructure(
        self, request: transcript_pb2.GetStructureRequest, context: grpc.ServicerContext
    ) -> transcript_pb2.GetStructureResponse:
        return _forward(self._stub.GetStructure, request, self._metadata, context)

    @override
    def AssessExonRelevance(
        self, request: transcript_pb2.AssessExonRelevanceRequest, context: grpc.ServicerContext
    ) -> transcript_pb2.AssessExonRelevanceResponse:
        return _forward(self._stub.AssessExonRelevance, request, self._metadata, context)


class SpliceForwarder(_Forwarder[splice_pb2_grpc.SpliceStub], splice_pb2_grpc.SpliceServicer):
    """Forward the guest's allowlisted splice calls to the evidence service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        super().__init__(splice_pb2_grpc.SpliceStub, channel, session_token=session_token)

    @override
    def PredictDeltas(
        self, request: splice_pb2.PredictDeltasRequest, context: grpc.ServicerContext
    ) -> splice_pb2.PredictDeltasResponse:
        return _forward(self._stub.PredictDeltas, request, self._metadata, context)

    @override
    def PredictSkipOutcome(
        self, request: splice_pb2.PredictSkipOutcomeRequest, context: grpc.ServicerContext
    ) -> splice_pb2.PredictSkipOutcomeResponse:
        return _forward(self._stub.PredictSkipOutcome, request, self._metadata, context)


class MaveDbForwarder(_Forwarder[mavedb_pb2_grpc.MaveDbStub], mavedb_pb2_grpc.MaveDbServicer):
    """Forward the guest's allowlisted mavedb call to the evidence service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        super().__init__(mavedb_pb2_grpc.MaveDbStub, channel, session_token=session_token)

    @override
    def DescribeVariant(
        self, request: mavedb_pb2.DescribeVariantRequest, context: grpc.ServicerContext
    ) -> mavedb_pb2.DescribeVariantResponse:
        return _forward(self._stub.DescribeVariant, request, self._metadata, context)


class CspecForwarder(_Forwarder[cspec_pb2_grpc.CspecStub], cspec_pb2_grpc.CspecServicer):
    """Forward the guest's allowlisted cspec call to the evidence service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        super().__init__(cspec_pb2_grpc.CspecStub, channel, session_token=session_token)

    @override
    def ListSpecifications(
        self, request: cspec_pb2.ListSpecificationsRequest, context: grpc.ServicerContext
    ) -> cspec_pb2.ListSpecificationsResponse:
        return _forward(self._stub.ListSpecifications, request, self._metadata, context)


class LiteratureForwarder(_Forwarder[literature_pb2_grpc.LiteratureStub], literature_pb2_grpc.LiteratureServicer):
    """Forward the guest's allowlisted literature calls to the evidence service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        super().__init__(literature_pb2_grpc.LiteratureStub, channel, session_token=session_token)

    @override
    def DescribePaper(
        self, request: literature_pb2.DescribePaperRequest, context: grpc.ServicerContext
    ) -> literature_pb2.PaperInfo:
        return _forward(self._stub.DescribePaper, request, self._metadata, context)

    @override
    def ResolveContent(
        self, request: literature_pb2.ResolveContentRequest, context: grpc.ServicerContext
    ) -> literature_pb2.ContentLocation:
        return _forward(self._stub.ResolveContent, request, self._metadata, context)

    @override
    def Locate(
        self, request: literature_pb2.LocateRequest, context: grpc.ServicerContext
    ) -> literature_pb2.LocateResponse:
        return _forward(self._stub.Locate, request, self._metadata, context)

    @override
    def Validate(
        self, request: literature_pb2.ValidateRequest, context: grpc.ServicerContext
    ) -> literature_pb2.ValidateResponse:
        return _forward(self._stub.Validate, request, self._metadata, context)

    @override
    def PollFullTexts(
        self, request: literature_pb2.PollFullTextsRequest, context: grpc.ServicerContext
    ) -> literature_pb2.PollFullTextsResponse:
        return _forward(self._stub.PollFullTexts, request, self._metadata, context)

    @override
    def MaybeIngestPapers(
        self, request: literature_pb2.MaybeIngestPapersRequest, context: grpc.ServicerContext
    ) -> literature_pb2.MaybeIngestPapersResponse:
        return _forward(self._stub.MaybeIngestPapers, request, self._metadata, context)

    @override
    def GetMarkdown(
        self, request: literature_pb2.GetMarkdownRequest, context: grpc.ServicerContext
    ) -> literature_pb2.GetMarkdownResponse:
        return _forward(self._stub.GetMarkdown, request, self._metadata, context)

    @override
    def SearchEuropePmc(
        self, request: literature_pb2.SearchEuropePmcRequest, context: grpc.ServicerContext
    ) -> literature_pb2.SearchEuropePmcResponse:
        return _forward(self._stub.SearchEuropePmc, request, self._metadata, context)

    @override
    def FetchPubmedArticles(
        self, request: literature_pb2.FetchPubmedArticlesRequest, context: grpc.ServicerContext
    ) -> literature_pb2.FetchPubmedArticlesResponse:
        return _forward(self._stub.FetchPubmedArticles, request, self._metadata, context)

    @override
    def SearchLitVar(
        self, request: literature_pb2.SearchLitVarRequest, context: grpc.ServicerContext
    ) -> literature_pb2.SearchLitVarResponse:
        return _forward(self._stub.SearchLitVar, request, self._metadata, context)

    @override
    def ListLitVarEntities(
        self, request: literature_pb2.ListLitVarEntitiesRequest, context: grpc.ServicerContext
    ) -> literature_pb2.ListLitVarEntitiesResponse:
        return _forward(self._stub.ListLitVarEntities, request, self._metadata, context)


def build_hatch(
    *,
    hello_channel: grpc.Channel,
    evidence_channel: grpc.Channel,
    session_token: str,
) -> postern_grpc.GrpcHatch:
    """A hatch exposing the allowlisted hello and evidence methods, forwarded with the session token injected.

    Every evidence forwarder dials ``evidence_channel``: the evidence interfaces are one deployment serving
    one gRPC service each. Both channels are keyword-only — they are the same type, and transposing them
    leaves every rpc dialling the wrong deployment.
    """
    hatch = postern_grpc.GrpcHatch(allowlist=GUEST_METHODS, max_workers=_HATCH_WORKERS)
    hatch.add_servicer(
        hello_pb2_grpc.add_HelloServicer_to_server,
        HelloForwarder(hello_channel, session_token=session_token),
    )
    hatch.add_servicer(
        variant_pb2_grpc.add_VariantServicer_to_server,
        VariantForwarder(evidence_channel, session_token=session_token),
    )
    hatch.add_servicer(
        vep_pb2_grpc.add_VepServicer_to_server,
        VepForwarder(evidence_channel, session_token=session_token),
    )
    hatch.add_servicer(
        gnomad_pb2_grpc.add_GnomadServicer_to_server,
        GnomadForwarder(evidence_channel, session_token=session_token),
    )
    hatch.add_servicer(
        clinvar_pb2_grpc.add_ClinVarServicer_to_server,
        ClinVarForwarder(evidence_channel, session_token=session_token),
    )
    hatch.add_servicer(
        gene_disease_pb2_grpc.add_GeneDiseaseServicer_to_server,
        GeneDiseaseForwarder(evidence_channel, session_token=session_token),
    )
    hatch.add_servicer(
        transcript_pb2_grpc.add_TranscriptServicer_to_server,
        TranscriptForwarder(evidence_channel, session_token=session_token),
    )
    hatch.add_servicer(
        splice_pb2_grpc.add_SpliceServicer_to_server,
        SpliceForwarder(evidence_channel, session_token=session_token),
    )
    hatch.add_servicer(
        mavedb_pb2_grpc.add_MaveDbServicer_to_server,
        MaveDbForwarder(evidence_channel, session_token=session_token),
    )
    hatch.add_servicer(
        cspec_pb2_grpc.add_CspecServicer_to_server,
        CspecForwarder(evidence_channel, session_token=session_token),
    )
    hatch.add_servicer(
        literature_pb2_grpc.add_LiteratureServicer_to_server,
        LiteratureForwarder(evidence_channel, session_token=session_token),
    )
    return hatch
