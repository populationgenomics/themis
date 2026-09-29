"""Clone an Analysis's repository from a deployed environment into a local directory, for inspection.

One command for what `tools/session_token.py`, `tools/sheaf_remote.py` and `git clone` do in turn:
the session token is derived into a token file in a private temporary directory, the loopback
remote is served in-process over the `Sheaf` service, and `git clone` runs against it. The server
stops and the temporary directory is deleted on the way out, token included, however the clone
ended.

The result is a working clone with the default branch checked out and every branch under
`origin/`. The namespaces a push may write besides branches and tags, and sheaf's own, are fetched
under their own names. Among them is `refs/sheaf/reflog`, which gains one commit per publish. A bare
mirror would hold the same refs but no working tree to read files in. Its mirror remote would also
turn a later `git push` into a `push --mirror`, which writes sheaf's own refs, and the service
refuses that push.

`origin` is left in place, naming the loopback URL of a server that has stopped. Removing it would
delete `refs/remotes/origin/*`, which is where the branches live. A fetch or push fails until
`tools/sheaf_remote.py` serves again and `origin` is pointed at its URL.

Run: ``uv run --group tools_sheaf python -m tools.clone_analysis --analysis-id <id> <dir>``. The
caller needs what the two tools need: membership of the `themis-clu` group, and the grants that
account holds (`docs/runbooks/hand-driving-a-service.md`).
"""

from __future__ import annotations

import os

# Set before gRPC is first imported: this process forks `git` while gRPC threads run, and with fork
# support on, gRPC logs every fork on stderr.
os.environ.setdefault('GRPC_ENABLE_FORK_SUPPORT', '0')

import argparse
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

from google.auth import exceptions

from themis.sheaf import refdoc
from themis.sheaf.wire import protect
from tools import clu, session_token, sheaf_remote

# `git clone` takes these by default: branches as remote-tracking refs, and tags.
_CLONED_BY_DEFAULT = ('refs/heads/', 'refs/tags/')
# Fetched on purpose, not everything a store can hold: a writer without the hook can store any ref,
# and a fetched `refs/replace/` would make the local git show one object's content as another's.
_FETCHED_NAMESPACES = tuple(
    namespace
    for namespace in (*protect.WRITABLE_NAMESPACES, refdoc.SHEAF_NAMESPACE)
    if namespace not in _CLONED_BY_DEFAULT
)
_CREDENTIALS_FILE = 'token.json'


def clone(git: str, url: str, destination: pathlib.Path) -> None:
    """Clone the repository at `url` into `destination` with the `git` binary, sheaf's namespaces included.

    Git's own output goes to the terminal. An interrupt is passed on to git and waited out, so git
    removes a half-made clone before the interrupt propagates.

    Raises:
        SystemExit: If `git clone` fails.
    """
    refspecs = [f'--config=remote.origin.fetch=+{namespace}*:{namespace}*' for namespace in _FETCHED_NAMESPACES]
    argv = [git, 'clone', *refspecs, '--', url, str(destination)]
    with subprocess.Popen(argv) as process:  # noqa: S603 — resolved path, fixed flags, our own URL
        try:
            returncode = process.wait()
        except KeyboardInterrupt:
            # Ctrl-C at a terminal reached git as well; SIGTERM and SIGHUP reached only this process.
            process.send_signal(signal.SIGINT)
            process.wait()
            raise
    if returncode != 0:
        raise SystemExit(f'git clone of {url} failed with exit code {returncode}; its output is above')


def clone_analysis(
    analysis_id: str,
    destination: pathlib.Path,
    *,
    project: str,
    service_url: str | None,
    service_account: str,
    key_version: str | None,
) -> None:
    """Clone `analysis_id`'s repository into `destination` over the sheaf service at `service_url`.

    The token file and the bare mirror live in one temporary directory that only the caller can
    read, deleted with both when the clone ends, on success, failure or interrupt alike.

    Args:
        analysis_id: The Analysis whose repository to clone.
        destination: Where to clone to: absent, or an empty directory.
        project: The environment's GCP project.
        service_url: The sheaf service's `https://` URL, or None to have `gcloud` describe it.
        service_account: The account the token derivation and the bearer act as.
        key_version: The signing key version, or None for the one on the environment's key ring.

    Raises:
        SystemExit: If `destination` is occupied, the Analysis does not exist, the key ring cannot be
            listed, or the clone fails.
        clu.GcloudError: If `gcloud` or `git` is not on PATH, or `gcloud` cannot describe the service
            or mint an identity token.
        google.auth.exceptions.DefaultCredentialsError: If there is no application-default login.
    """
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise SystemExit(f'{destination} exists and is not an empty directory; name a new or empty one')
    git = clu.resolve_binary('git')
    service_url = service_url or clu.run_service_url(sheaf_remote.SERVICE, project=project)
    # mkdtemp creates the directory readable, writable and searchable by its owner alone.
    scratch = pathlib.Path(tempfile.mkdtemp(prefix='clone-analysis-'))
    try:
        token_file = scratch / _CREDENTIALS_FILE
        session_token.write_token_file(
            token_file,
            session_token.derive_session_token(
                analysis_id, project=project, service_account=service_account, key_version=key_version
            ),
        )
        with sheaf_remote.serving(
            analysis_id=analysis_id,
            token_file=token_file,
            service_url=service_url,
            service_account=service_account,
            port=0,
            root=scratch / 'mirror',
        ) as instance:
            clone(git, instance.url(analysis_id), destination)
    finally:
        _remove_scratch(scratch)
    _report(git, analysis_id, destination)


def _remove_scratch(scratch: pathlib.Path) -> None:
    """Delete the token first, then the rest; a directory left behind is named rather than raised over the cause.

    A request thread still syncing after a second interrupt can be writing into the mirror, which can
    make the tree removal fail partway.
    """
    for credential in scratch.glob(f'*{_CREDENTIALS_FILE}*'):
        credential.unlink(missing_ok=True)
    shutil.rmtree(scratch, ignore_errors=True)
    if scratch.exists():
        print(f'could not delete {scratch}; delete it yourself, as it may hold the session token', file=sys.stderr)


def _report(git: str, analysis_id: str, destination: pathlib.Path) -> None:
    where = shlex.quote(str(destination))
    namespaces = ', '.join(_FETCHED_NAMESPACES)
    checked_out = subprocess.run(  # noqa: S603 — resolved path, fixed flags
        [git, '-C', str(destination), 'rev-parse', '--verify', '--quiet', 'HEAD'], capture_output=True, check=False
    )
    print(f'cloned {analysis_id} into {destination}. The session token and the server are gone.')
    if checked_out.returncode == 0:
        print('The default branch is checked out, and every branch is under origin/.')
    else:
        print("Nothing is checked out: the repository's default branch has no commits.")
    print(f'Tags, and the refs under {namespaces}, keep their own names.')
    print()
    print('origin names the stopped loopback server. To fetch or push, serve it with tools.sheaf_remote')
    print('(docs/runbooks/hand-driving-a-service.md) and point origin at the URL it prints:')
    print(f'    git -C {where} remote set-url origin <url>')
    print()
    print(f'Every publish adds one commit to {refdoc.REFLOG_REF}. Its first parent is the previous entry,')
    print('its other parents are the tips the publish set, and its message names each ref it moved, from')
    print('and to. Newest first, ending at the root entry `sheaf: init`:')
    print(f'    git -C {where} log --first-parent {refdoc.REFLOG_REF}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--analysis-id', required=True, help='the Analysis whose repository to clone')
    parser.add_argument('destination', type=pathlib.Path, help='where to clone to: absent, or an empty directory')
    parser.add_argument('--project', default=clu.DEFAULT_PROJECT, help='GCP project (default: %(default)s)')
    parser.add_argument(
        '--service-url',
        default=None,
        help=f'the sheaf service URL (default: gcloud describes {sheaf_remote.SERVICE} in --project)',
    )
    parser.add_argument(
        '--key-version',
        default=None,
        help='the signing key version resource name (default: the one MAC key on the themis key ring, version 1)',
    )
    parser.add_argument(
        '--as',
        dest='impersonate',
        metavar='SERVICE_ACCOUNT',
        default=None,
        help='derive the token and mint the identity token as this service account (default: themis-clu in --project)',
    )
    args = parser.parse_args()
    # These unwind like Ctrl-C, so the server stops and the token is deleted whichever ends the run.
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, signal.default_int_handler)
    service_account = args.impersonate or clu.service_account(args.project)
    try:
        clone_analysis(
            args.analysis_id,
            args.destination,
            project=args.project,
            service_url=args.service_url,
            service_account=service_account,
            key_version=args.key_version,
        )
    except (clu.GcloudError, exceptions.DefaultCredentialsError) as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        raise SystemExit(128 + signal.SIGINT) from None


if __name__ == '__main__':
    main()
