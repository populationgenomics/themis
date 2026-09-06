"""A sheaf repository reached through the `Sheaf` service: the client side of its storage protocol.

`RemoteStore` is `themis.sheaf.Repository` over the generated `SheafStub`, so the wire layer —
the bare mirror, the loopback server, the pre-receive hook — runs over it unchanged and a caller
holding no bucket credential still hydrates, serves and publishes a repository. Every call carries
the session token that scopes it to one Analysis's repository; the service decides which. The
client's own work is the successor document: it derives it through `themis.sheaf.plan`, as the
service does, so the snapshot it hands back after a publish it landed is the one the service wrote.
Contract: `schema/proto/themis/rpc/sheaf.proto`; design: `docs/design/sheaf-service.md`.

Credentials come from a JSON file, never from the process environment or the descriptor the hook
is handed: the hook parses guest bytes, and its environment and state file are readable in places
a token must not be. The file is `{"session_token": "..."}`, with an optional `"bearer"` — an ID
token for the service's Cloud Run URL, for a caller that minted one itself rather than running as a
service account that can. It is re-read on every call, so a refreshed token is picked up.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import stat
from collections.abc import Iterator, Mapping, Sequence
from typing import Self, override

import grpc
from google.protobuf import empty_pb2

from themis import sheaf
from themis.clients import id_token
from themis.clients.auth import session as session_mod
from themis.rpc import sheaf_pb2, sheaf_pb2_grpc
from themis.sheaf import errors, refdoc, stores
from themis.sheaf import store as store_mod

# Under gRPC's 4 MiB default per-message limit, with margin, so a large pack streams.
CHUNK_SIZE = 1 << 20
# Per-call deadlines: not forever, so a stalled service fails the push rather than holding the
# mirror's lock indefinitely. A read returns a few kilobytes and is retried; a transfer moves packs.
READ_TIMEOUT_SECONDS = 30.0
TRANSFER_TIMEOUT_SECONDS = 600.0
_NO_DOCUMENT = 0
_HTTPS = 'https://'
_LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '[::1]'})
# Retry a transport-level UNAVAILABLE on the two reads. Publish is left out: its replay is the
# caller's, and a landed publish succeeds again when replayed.
_SERVICE_CONFIG = json.dumps(
    {
        'methodConfig': [
            {
                'name': [
                    {'service': 'themis.rpc.sheaf.Sheaf', 'method': 'ReadRefDoc'},
                    {'service': 'themis.rpc.sheaf.Sheaf', 'method': 'FetchPack'},
                ],
                'retryPolicy': {
                    'maxAttempts': 4,
                    'initialBackoff': '0.2s',
                    'maxBackoff': '2s',
                    'backoffMultiplier': 2,
                    'retryableStatusCodes': ['UNAVAILABLE'],
                },
            }
        ]
    }
)
_CHANNEL_OPTIONS = (('grpc.service_config', _SERVICE_CONFIG), ('grpc.enable_retries', 1))


@dataclasses.dataclass(frozen=True)
class Credentials:
    """What one call presents: the session token, and the ID token when the caller minted its own."""

    # No repr: a pytest diff or a stray log line must not print a credential.
    session_token: str = dataclasses.field(repr=False)
    bearer: str | None = dataclasses.field(repr=False)

    def metadata(self) -> tuple[tuple[str, str], ...]:
        """The call metadata carrying these credentials."""
        pairs = [(session_mod.SESSION_TOKEN_METADATA, self.session_token)]
        if self.bearer is not None:
            pairs.append(('authorization', f'Bearer {self.bearer}'))
        return tuple(pairs)


def read_credentials(path: pathlib.Path) -> Credentials:
    """Read the token file at `path`.

    Raises:
        CredentialsUnusable: If the file cannot be read, is readable by anyone but its owner, or is
            not a JSON object of `session_token` and an optional `bearer`, each a non-empty string,
            and nothing else — a misspelt key would otherwise send a call without the credential it
            meant to carry.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise errors.CredentialsUnusable(f'{path}: mode {mode:o} lets others read a credential; chmod 600 it')
        payload = json.loads(path.read_text('utf-8'))
    except OSError as exc:
        raise errors.CredentialsUnusable(f'{path}: {exc.strerror or exc}') from exc
    except json.JSONDecodeError as exc:
        raise errors.CredentialsUnusable(f'{path}: not JSON: {exc}') from exc
    if not isinstance(payload, dict):
        raise errors.CredentialsUnusable(f'{path}: a token file is a JSON object')
    unknown = set(payload) - {'session_token', 'bearer'}
    if unknown:
        raise errors.CredentialsUnusable(f'{path}: unexpected keys {sorted(unknown)}')
    session_token = payload.get('session_token')
    if not isinstance(session_token, str) or not session_token:
        raise errors.CredentialsUnusable(f'{path}: session_token must be a non-empty string')
    bearer = payload.get('bearer')
    if bearer is not None and (not isinstance(bearer, str) or not bearer):
        raise errors.CredentialsUnusable(f'{path}: bearer, when present, must be a non-empty string')
    return Credentials(session_token=session_token, bearer=bearer)


def write_credentials(path: pathlib.Path, credentials: Credentials) -> None:
    """Write `credentials` to the token file at `path`, readable by its owner alone.

    Written beside `path` and renamed over it, so a reader — a hook mid-push — sees the old file
    or the new one and never a partial one.
    """
    payload: dict[str, str] = {'session_token': credentials.session_token}
    if credentials.bearer is not None:
        payload['bearer'] = credentials.bearer
    staging = path.with_name(f'.{path.name}.{os.getpid()}')
    staging.unlink(missing_ok=True)
    # O_EXCL: the staging file is created here with this mode, never reopened with an older one.
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle)
        os.replace(staging, path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


def _status(exc: grpc.RpcError) -> tuple[grpc.StatusCode, str]:
    """The code and details of a failed call, or `exc` re-raised if it carries no status."""
    if not isinstance(exc, grpc.Call):
        raise exc
    return exc.code(), exc.details() or ''


def _fault(exc: grpc.RpcError) -> errors.ServiceFault:
    code, details = _status(exc)
    return errors.ServiceFault(code.name, details)


def _intent_message(base: store_mod.Snapshot, intent: store_mod.Intent) -> sheaf_pb2.PublishIntent:
    message = sheaf_pb2.PublishIntent(base_generation=_NO_DOCUMENT if base.generation is None else base.generation)
    for ref, update in intent.ref_updates.items():
        wire = message.ref_updates[ref]
        if update.old is not None:
            wire.old = update.old
        if update.new is not None:
            wire.new = update.new
    if intent.head is not None:
        refdoc.write_target(message.head, intent.head)
    for data in intent.packs:
        message.packs.add(size=len(data), pack_id=sheaf.pack_id(data))
    return message


def _requests(message: sheaf_pb2.PublishIntent, packs: Sequence[bytes]) -> Iterator[sheaf_pb2.PublishRequest]:
    """The Publish stream: the intent, then each pack's bytes in declared order."""
    yield sheaf_pb2.PublishRequest(intent=message)
    for index, data in enumerate(packs):
        for start in range(0, len(data), CHUNK_SIZE):
            chunk = sheaf_pb2.PublishChunk(pack=index, content=data[start : start + CHUNK_SIZE])
            yield sheaf_pb2.PublishRequest(chunk=chunk)


def _channel(target: str, *, self_minted: bool) -> grpc.Channel:
    """A channel to `target`: TLS to a Cloud Run URL, plain to a loopback `host:port` a test serves on.

    Over TLS the ID token is either in the token file — the caller minted it, so the channel carries
    only TLS and each call adds it — or minted from the metadata server by the channel's own
    credentials, for a caller running as a service account.

    Raises:
        ValueError: If `target` is a URL with any scheme but `https`, or a plain `host:port` that is
            not loopback: the session token is the credential that scopes the repository, and a
            plain channel to another host would send it in the clear. A bearer is refused over a
            plain channel even to loopback.
    """
    if target.startswith(_HTTPS):
        credentials = grpc.ssl_channel_credentials() if self_minted else id_token.channel_credentials(target)
        return grpc.secure_channel(session_mod.grpc_target(target), credentials, options=_CHANNEL_OPTIONS)
    if '://' in target:
        raise ValueError(f'{target!r}: a sheaf service target is an https:// URL or a loopback host:port')
    host = target.rpartition(':')[0]
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(f'{target!r}: a plain host:port target is loopback only; the service is reached over https://')
    if self_minted:
        raise ValueError(f'{target!r}: a bearer token is sent over TLS only, not to a host:port')
    return grpc.insecure_channel(target, options=_CHANNEL_OPTIONS)


class RemoteStore(store_mod.Repository):
    """One repository, reached through the `Sheaf` service the session token names.

    `repo` is the name the mirror and the server know it by — a path and a label — and nothing on
    the wire: which repository a call reaches is the session's. The channel is built once and
    closed by `close` (or on leaving a `with` block); the token file is read on every call.

    Every status that is not one of the protocol's refusals is `ServiceFault`: the wire layer's
    hook runs where `grpc` is not imported, and reports one as a deployment fault, not the pusher's.
    """

    def __init__(self, target: str, token_file: pathlib.Path, *, repo: str) -> None:
        """Open a store over the service at `target`, presenting the credentials in `token_file`.

        Args:
            target: The service's `https://` URL, or a loopback `host:port` for an in-process server.
            token_file: The JSON token file; see the module docstring for its shape. Made absolute,
                since the hook that rebuilds this store from the descriptor runs in another directory.
            repo: The repository's name in paths and messages.

        Raises:
            ValueError: If `target` is neither form.
            CredentialsUnusable: If the token file cannot be read or is malformed.
        """
        self.repo = repo.strip('/')
        self.target = target
        self.token_file = token_file.absolute()
        self._self_minted = read_credentials(self.token_file).bearer is not None
        self._channel = _channel(target, self_minted=self._self_minted)
        self._stub = sheaf_pb2_grpc.SheafStub(self._channel)

    def close(self) -> None:
        """Release the channel. Every call after this fails."""
        self._channel.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @classmethod
    def from_descriptor(cls, descriptor: Mapping[str, str]) -> Self:
        """Rebuild the store `descriptor` describes.

        Raises:
            ValueError: If the kind is not this store's.
            KeyError: If a key is absent.
        """
        if descriptor.get('kind') != stores.REMOTE_KIND:
            raise ValueError(f'not a {stores.REMOTE_KIND} descriptor: {descriptor.get("kind")!r}')
        return cls(descriptor['target'], pathlib.Path(descriptor['token_file']), repo=descriptor['repo'])

    @override
    def descriptor(self) -> dict[str, str]:
        """Where the service is and where the credentials are — the path, never the token."""
        return {
            'kind': stores.REMOTE_KIND,
            'target': self.target,
            'token_file': str(self.token_file),
            'repo': self.repo,
        }

    def _metadata(self) -> tuple[tuple[str, str], ...]:
        """This call's credentials, read afresh.

        Raises:
            CredentialsUnusable: If the file cannot be read, or has gained or lost its bearer since the
                channel was built — the channel's credentials were chosen on that, and a call would go
                out with the wrong ones.
        """
        credentials = read_credentials(self.token_file)
        if (credentials.bearer is not None) != self._self_minted:
            raise errors.CredentialsUnusable(
                f'{self.token_file}: the bearer appeared or vanished since the store was opened'
            )
        return credentials.metadata()

    @override
    def read(self) -> store_mod.Snapshot:
        try:
            response = self._stub.ReadRefDoc(empty_pb2.Empty(), metadata=self._metadata(), timeout=READ_TIMEOUT_SECONDS)
        except grpc.RpcError as exc:
            code, details = _status(exc)
            if code is grpc.StatusCode.DATA_LOSS:
                raise errors.CorruptRepository(f'{self.repo}: {details}') from exc
            raise _fault(exc) from exc
        if response.generation == _NO_DOCUMENT:
            return store_mod.Snapshot(doc=refdoc.RefDoc(), generation=None)
        try:
            doc = refdoc.RefDoc.from_bytes(response.document.SerializeToString())
        except ValueError as exc:
            raise errors.CorruptRepository(f'{self.repo}: the service returned {exc}') from exc
        return store_mod.Snapshot(doc=doc, generation=response.generation)

    @override
    def fetch_pack(self, ident: str) -> bytes:
        request = sheaf_pb2.FetchPackRequest(pack_id=refdoc.validate_pack_id(ident))
        try:
            chunks = self._stub.FetchPack(request, metadata=self._metadata(), timeout=TRANSFER_TIMEOUT_SECONDS)
            return b''.join(chunk.content for chunk in chunks)
        except grpc.RpcError as exc:
            code, details = _status(exc)
            if code is grpc.StatusCode.NOT_FOUND:
                raise errors.NotFound(f'{self.repo}: {details}') from exc
            if code is grpc.StatusCode.INVALID_ARGUMENT:
                raise errors.InvalidPackId(details) from exc
            raise _fault(exc) from exc

    @override
    def publish(self, base: store_mod.Snapshot, intent: store_mod.Intent) -> store_mod.Snapshot:
        """Publish `intent` against `base` through the service.

        The store-level refusals are raised here before anything is sent, as `themis.sheaf.plan`
        raises them. What the service decides arrives as its status: a lost race is `RaceLost`; a ref
        that moved under the publish is re-read and reported as the `RefConflict` the in-process
        store would raise; anything the service refuses on the intent alone is `PublishRefused`.

        Returns:
            The document this publish leaves, at the generation the service reports. The document
            is derived here, not read back: it is what the service wrote when this call is the one
            that landed the publish. A replay of a publish that had already landed succeeds with the
            current generation, and the document it carries may then be behind publishes that landed
            since — a caller that replays reads again before building on the result.

        Raises:
            ValueError: If `intent` names `stored_packs`: nothing but the service stores a pack, so a
                caller here can have pre-stored none.
            PublishRefused: If the service refused the intent, or the publish is over a ceiling.
            RaceLost: If the document moved and no ref the intent moves moved with it.
            RefConflict: If a ref the intent moves has moved.
            CorruptRepository: If the stored document is not one this code wrote.
            ServiceFault: If the service could not be reached or did not admit the caller.
        """
        if intent.stored_packs:
            raise ValueError('a remote publish carries its packs; it cannot name stored ones')
        doc = store_mod.plan(base, intent)
        message = _intent_message(base, intent)
        try:
            response = self._stub.Publish(
                _requests(message, intent.packs), metadata=self._metadata(), timeout=TRANSFER_TIMEOUT_SECONDS
            )
        except grpc.RpcError as exc:
            raise self._refusal(exc, intent) from exc
        return store_mod.Snapshot(doc=doc, generation=response.generation)

    def _refusal(self, exc: grpc.RpcError, intent: store_mod.Intent) -> errors.SheafError:
        """The typed error for a refused publish; a status that is not a refusal is a `ServiceFault`."""
        code, details = _status(exc)
        if code is grpc.StatusCode.ABORTED:
            return errors.RaceLost(details)
        if code is grpc.StatusCode.FAILED_PRECONDITION:
            return self._conflict(intent)
        if code in (grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.RESOURCE_EXHAUSTED):
            return errors.PublishRefused(details)
        if code is grpc.StatusCode.DATA_LOSS:
            return errors.CorruptRepository(f'{self.repo}: {details}')
        return _fault(exc)

    def _conflict(self, intent: store_mod.Intent) -> errors.SheafError:
        """The `RefConflict` for a publish the service classified as a moved ref, from a fresh read.

        The status names the refs in prose only, so the live document is read once more and the
        first moved ref reported with what it holds — the same refusal the in-process store makes.
        Should another publish have landed in between and the classification no longer be a moved
        ref, the caller has a race to replay, and is told so.
        """
        live = self.read()
        classification = store_mod.classify(live.refs, intent.ref_updates)
        if classification.verdict is not store_mod.Verdict.REF_MOVED:
            return errors.RaceLost(f'{self.repo}: the document moved again while the refusal was being read')
        ref = classification.refs[0]
        return errors.RefConflict(ref, intent.ref_updates[ref].old, live.refs.get(ref))
