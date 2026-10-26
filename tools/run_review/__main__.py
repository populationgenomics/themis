"""`python -m tools.run_review`: prepare, run, render and list a round of the run-review loop.

    uv run --group run_review python -m tools.run_review prepare --round 2026-10-05-0930 --workbench-project <id>
    uv run --group run_review python -m tools.run_review run --round 2026-10-05-0930 --anthropic-profile cpg-themis-dev
    uv run --group run_review python -m tools.run_review render --round 2026-10-05-0930
    uv run --group run_review python -m tools.run_review status --round 2026-10-05-0930

`prepare` reads the reference from the curation schema and records the round's settings; every later
command reads them from the round. Everything in GCP is reached as `themis-clu`, impersonated from the
operator's application-default login; the Anthropic event log is read with the operator's profile for
the dev deployment's workspace.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime
import fcntl
import pathlib
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Sequence

# isort: off
# First: it sets the fork setting gRPC reads once, at gRPC's own first import.
from tools import clone_analysis

# isort: on
import anthropic
from google.auth import exceptions as google_auth_exceptions

from tools import clu, clu_database, operator_path, session_token, stack, web_app
from tools.run_review import rounds
from tools.run_review.run import provenance, runner, sessions
from tools.run_review.variant_classification import reference, render, scenario

_POLL_EVERY = datetime.timedelta(seconds=60)
_WORKSPACE_HEADER = 'anthropic-workspace-id'
_LOCK = '.run.lock'
_REVIEW = 'review.md'
_RENDER_ERROR = 'render-error.txt'
# What a variant's directory holds that was made from its run, and moves aside with it on a rerun.
_DERIVED = (scenario.RENDERED_DIR, _REVIEW, _RENDER_ERROR)
# The stack option only dev sets: it lets `themis-clu` derive session tokens, which collection needs.
_DEV_OPT_IN = 'themis:cluDerivesSessionTokens'


class CommandError(Exception):
    """A command cannot proceed; the message says why."""


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # A hangup or a kill unwinds like Ctrl-C. The signal reaches the main thread only, which stops starting
    # runs and waits for those in flight; a clone on a worker thread runs on to its own end, which deletes
    # its derived token, and the interpreter joins the workers at exit even if a second signal cuts the wait.
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, signal.default_int_handler)
    try:
        return args.handler(args)
    except (
        CommandError,
        rounds.RoundError,
        runner.RunError,
        provenance.ProvenanceError,
        web_app.AccessRefusedError,
        web_app.RpcError,
        clu.GoogleCallError,
        google_auth_exceptions.DefaultCredentialsError,
    ) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m tools.run_review', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)

    prepare = commands.add_parser('prepare', help="start a round: read the reference, write each variant's kickoff")
    prepare.add_argument('--round', required=True, help='the round name, starting YYYY-MM-DD-HHMM')
    prepare.add_argument(
        '--workbench-project', required=True, help="the Themis Project the round's Analyses are created in"
    )
    prepare.add_argument('--project', default=clu.DEFAULT_PROJECT, help='GCP project (default: %(default)s)')
    prepare.add_argument(
        '--deadline-minutes', type=int, default=120, help='when a run still going is interrupted (default: %(default)s)'
    )
    prepare.add_argument(
        '--settle-seconds',
        type=int,
        default=180,
        help=(
            "how long after a run's end to wait for the worker's teardown push, which lands about a minute after "
            f'the end; at least {int(runner.SETTLE_FLOOR.total_seconds())} (default: %(default)s)'
        ),
    )
    prepare.set_defaults(handler=_prepare)

    run = commands.add_parser('run', help='create, watch and collect every runnable variant of a round')
    run.add_argument('--round', required=True)
    run.add_argument(
        '--anthropic-profile', required=True, help="the operator's Anthropic profile for the dev workspace"
    )
    run.add_argument('--variant', action='append', default=[], help='run only this variant; repeatable')
    run.add_argument(
        '--rerun',
        action='append',
        default=[],
        metavar='VARIANT',
        help="move this variant's run aside and create a new one, as for a straggler on another agent version; "
        'repeatable, and run with the --variant ones (without --variant, only these run)',
    )
    run.add_argument('--concurrency', type=int, default=4, help='runs in flight at once (default: %(default)s)')
    run.set_defaults(handler=_run)

    render_command = commands.add_parser(
        'render', help="render every collected run's record and its curators' submissions"
    )
    render_command.add_argument('--round', required=True)
    render_command.set_defaults(handler=_render)

    status = commands.add_parser('status', help='list where every variant of a round stands')
    status.add_argument('--round', required=True)
    status.set_defaults(handler=_status)
    return parser


def _require_dev(project: str) -> None:
    """Refuse a GCP project whose stack is not dev's, before anything is read from or created in it.

    Raises:
        CommandError: If the stack deployed in `project` does not opt in to `themis-clu` deriving tokens.
    """
    try:
        opted_in = stack.value(project, _DEV_OPT_IN)
    except ValueError as e:
        raise CommandError(f'the loop runs against dev only, and {e}') from e
    if opted_in != 'true':
        raise CommandError(f'the loop runs against dev only, and the stack in {project} sets {_DEV_OPT_IN}={opted_in}')


def _prepare(args: argparse.Namespace) -> int:
    if args.deadline_minutes < 1:
        raise CommandError('--deadline-minutes is at least 1')
    if datetime.timedelta(seconds=args.settle_seconds) < runner.SETTLE_FLOOR:
        raise CommandError(f'--settle-seconds is at least {int(runner.SETTLE_FLOOR.total_seconds())}')
    _require_dev(args.project)
    directory = rounds.round_dir(args.round)
    if directory.exists():
        raise CommandError(f'{directory} exists; name a new round')
    service_account = clu.service_account(args.project)
    web_origin = web_app.origin(args.project)
    web = web_app.Workbench(web_origin, service_account)
    listed = {project.id for project in web.list_projects().projects}
    if args.workbench_project not in listed:
        raise CommandError(
            f'{service_account} is not a member of the Project {args.workbench_project!r}; '
            f'its Projects: {sorted(listed)}'
        )
    with clu_database.connect(args.project, service_account) as conn:
        reading = reference.read(conn)
    settings = rounds.Settings(
        gcp_project=args.project,
        service_account=service_account,
        web_origin=web_origin,
        workbench_project=args.workbench_project,
        deadline_seconds=args.deadline_minutes * 60,
        settle_seconds=args.settle_seconds,
        prepared_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )
    # Staged and renamed into place, so a prepare that fails partway leaves no round, half-written or not.
    with rounds.staged(directory) as stage:
        prepared = scenario.prepare(stage, reading, args.workbench_project)
        rounds.create(stage, settings)
    print(
        f'{directory}: {len(prepared.runnable)} variant(s) to run; {reading.unsubmitted} registered, none submitted; '
        f'{len(prepared.incomplete)} submitted, none complete'
    )
    for variant_id, reason in prepared.incomplete:
        print(f'INCOMPLETE {variant_id}: {reason}')
    for variant_id, reason in prepared.unusable:
        print(f'UNUSABLE {variant_id}: {reason}', file=sys.stderr)
    return 1 if prepared.unusable else 0


class _CancelledError(Exception):
    """The operator stopped the round; a run raises it at its next wait."""


def _run(args: argparse.Namespace) -> int:
    if args.concurrency < 1:
        raise CommandError('--concurrency is at least 1')
    directory = rounds.round_dir(args.round)
    settings = rounds.settings(directory)
    _require_dev(settings.gcp_project)
    selected = _requests(directory, settings, only=[*args.variant, *args.rerun])
    client = anthropic.Anthropic(profile=args.anthropic_profile)
    # Read-only checks before any run exists, since a created run cannot be taken back.
    _check_workspace(client, settings.gcp_project, args.anthropic_profile)
    web = web_app.Workbench(settings.web_origin, settings.service_account)
    web.list_analyses(settings.workbench_project)
    our_skills = provenance.declared_skills()
    try:
        timing = runner.Timing(deadline=settings.deadline, settle=settings.settle, poll_every=_POLL_EVERY)
    except ValueError as e:
        raise CommandError(f'the round was prepared with timing this harness refuses: {e}') from e
    print_lock = threading.Lock()
    stop = threading.Event()

    def log_for(variant_id: str) -> Callable[[str], None]:
        def log(line: str) -> None:
            with print_lock:
                print(f'{datetime.datetime.now(datetime.UTC):%H:%M:%S} {variant_id}: {line}', file=sys.stderr)

        return log

    def sleep(seconds: float) -> None:
        if stop.wait(seconds):
            raise _CancelledError('the operator stopped the round')

    def one(variant_id: str, request: runner.RunRequest) -> runner.RunRecord:
        deps = runner.Deps(
            workbench=web,
            sessions=sessions.AnthropicSessions(client),
            clone=_clone(settings.gcp_project, settings.service_account),
            now=lambda: datetime.datetime.now(datetime.UTC),
            sleep=sleep,
            log=log_for(variant_id),
            our_skills=our_skills,
            transient=(*sessions.TRANSIENT_ANTHROPIC_ERRORS, *web_app.TRANSIENT_ERRORS),
            rejected=web_app.NOT_ACTED_ON,
        )
        return runner.execute(request, directory / variant_id / rounds.RUN_DIR, timing=timing, deps=deps)

    uncollected: list[str] = []
    with _round_lock(directory):
        for variant_id in sorted(set(args.rerun)):
            variant_dir = directory / variant_id
            moved = runner.supersede(
                variant_dir / rounds.RUN_DIR,
                datetime.datetime.now(datetime.UTC),
                derived=[variant_dir / name for name in _DERIVED],
            )
            log_for(variant_id)(f'moved the old run aside to {moved.name}; its session is left as it is')
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency)
        try:
            futures = {pool.submit(one, variant_id, request): variant_id for variant_id, request in selected}
            for future in concurrent.futures.as_completed(futures):
                variant_id = futures[future]
                try:
                    record = future.result()
                except Exception as e:  # noqa: BLE001 — one run's failure is reported, and the others go on
                    uncollected.append(variant_id)
                    log_for(variant_id)(f'FAILED {type(e).__name__}: {e}; recorded in its run.json, see `status`')
                    continue
                if record.collected_at is None:
                    uncollected.append(variant_id)
        except KeyboardInterrupt:
            stop.set()
            print(
                'stopping: no new run starts, and each run in flight stops being watched at its next wait. Their '
                'sessions are left running: nothing is interrupted.',
                file=sys.stderr,
            )
            pool.shutdown(wait=True, cancel_futures=True)
            print('stopped; `run` again picks up every run it created where its record leaves it', file=sys.stderr)
            return 130
        finally:
            pool.shutdown(wait=True)
    if uncollected:
        print(f'not collected: {", ".join(sorted(uncollected))}; `status` says why', file=sys.stderr)
        return 1
    return 0


def _check_workspace(client: anthropic.Anthropic, project: str, profile: str) -> None:
    """Refuse a profile whose workspace is not the one the stack in `project` runs its sessions in.

    Raises:
        CommandError: If the workspace the API answers from is another.
    """
    expected = stack.value(project, 'themis:anthropicWorkspaceId')
    answered = client.beta.sessions.with_raw_response.list(limit=1).headers.get(_WORKSPACE_HEADER)
    if answered != expected:
        raise CommandError(
            f'the Anthropic profile {profile!r} reaches workspace {answered!r}; the stack in {project} runs '
            f'its sessions in {expected!r}'
        )


@contextlib.contextmanager
def _round_lock(directory: pathlib.Path) -> Iterator[None]:
    """Hold the round for this process, so two `run`s cannot create the same variant's run twice.

    Raises:
        CommandError: If another process holds it.
    """
    with (directory / _LOCK).open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise CommandError(f'another `run` is working on {directory}') from e
        yield


def _requests(
    directory: pathlib.Path, settings: rounds.Settings, *, only: Sequence[str]
) -> list[tuple[str, runner.RunRequest]]:
    """The requests of the variants named in `only`, or of every runnable variant where it names none.

    Raises:
        CommandError: If a named variant is not in the round or cannot run.
    """
    runnable: dict[str, runner.RunRequest] = {}
    for variant_dir in rounds.variant_dirs(directory):
        request = scenario.run_request(variant_dir, settings.workbench_project)
        if request is not None:
            runnable[variant_dir.name] = request
    unknown = sorted(set(only) - set(runnable))
    if unknown:
        raise CommandError(f'not a runnable variant of this round: {", ".join(unknown)}')
    selected = sorted(set(only)) if only else sorted(runnable)
    return [(variant_id, runnable[variant_id]) for variant_id in selected]


def _clone(project: str, service_account: str) -> Callable[[str, pathlib.Path], None]:
    def clone(analysis_id: str, destination: pathlib.Path) -> None:
        try:
            clone_analysis.clone_analysis(
                analysis_id,
                destination,
                project=project,
                service_url=None,
                origin=None,
                service_account=service_account,
                key_version=None,
            )
        except (
            clone_analysis.CloneError,
            session_token.TokenError,
            clu.GoogleCallError,
            operator_path.TargetError,
        ) as e:
            raise runner.CloneError(str(e)) from e

    return clone


def _render(args: argparse.Namespace) -> int:
    """Render every collected run; a variant that fails is recorded beside it, and the others go on."""
    directory = rounds.round_dir(args.round)
    rounds.settings(directory)
    failed = 0
    for variant_dir in rounds.variant_dirs(directory):
        error_file = variant_dir / _RENDER_ERROR
        error_file.unlink(missing_ok=True)
        if (variant_dir / scenario.UNUSABLE).is_file():
            print(f'{variant_dir.name}: not rendered, its reference is unusable', file=sys.stderr)
            continue
        record = runner.load(variant_dir / rounds.RUN_DIR)
        if record is None or record.collected_at is None:
            # A rendering left from a run that was collected once must not pass for this one's.
            shutil.rmtree(variant_dir / scenario.RENDERED_DIR, ignore_errors=True)
            print(f'{variant_dir.name}: NOT RENDERED, its run was not collected; `status` says why', file=sys.stderr)
            failed += 1
            continue
        try:
            written = scenario.render_variant(variant_dir)
        except (
            render.RenderError,
            reference.UnusableReferenceError,
            rounds.RoundError,
            subprocess.CalledProcessError,
            ValueError,
        ) as e:
            rounds.publish(error_file, f'{type(e).__name__}: {e}\n'.encode())
            print(f'{variant_dir.name}: RENDER FAILED, recorded in {_RENDER_ERROR}: {e}', file=sys.stderr)
            failed += 1
            continue
        for path in written:
            print(path)
    return 1 if failed else 0


def _status(args: argparse.Namespace) -> int:
    directory = rounds.round_dir(args.round)
    settings = rounds.settings(directory)
    print(
        f'round {directory.name}: Project {settings.workbench_project}, deadline {settings.deadline}, '
        f'settle {settings.settle}'
    )
    on_version: dict[tuple[str, int], list[str]] = {}
    for variant_dir in rounds.variant_dirs(directory):
        line, version = _variant_status(variant_dir)
        print(f'{variant_dir.name}: {line}')
        if version is not None:
            on_version.setdefault(version, []).append(variant_dir.name)
    if len(on_version) > 1:
        newest = max(on_version, key=lambda version: version[1])
        stragglers = sorted(name for version, names in on_version.items() if version != newest for name in names)
        reruns = ' '.join(f'--rerun {name}' for name in stragglers)
        print(
            f'MIXED AGENT VERSIONS {sorted(on_version)}: not one round. Rerun the stragglers on another version '
            f'than {newest[0]} v{newest[1]}: `run {reruns}`'
        )
        return 1
    return 0


def _variant_status(variant_dir: pathlib.Path) -> tuple[str, tuple[str, int] | None]:
    if (variant_dir / scenario.UNUSABLE).is_file():
        return f'unusable reference: {(variant_dir / scenario.UNUSABLE).read_text("utf-8").strip()}', None
    record = runner.load(variant_dir / rounds.RUN_DIR)
    if record is None:
        return 'not run', None
    parts = ['creating' if record.created is None else f'analysis {record.created.analysis_id}']
    parts.append('ending pending' if record.ending is None else f'ended {record.ending.kind}')
    if record.interrupts:
        parts.append(f'interrupted by the round ({len(record.interrupts)} sent)')
    if record.failure is not None:
        parts.append(f'FAILED: {record.failure}')
    if record.collection_error is not None:
        parts.append(f'NOT COLLECTED: {record.collection_error}')
    elif record.collected_at is not None:
        parts.append('collected')
    version = None
    if record.provenance is not None:
        fields = rounds.Fields(record.provenance, f'{variant_dir.name}: provenance')
        version = (fields.text('agent_id'), fields.integer('agent_version'))
        parts.append(f'agent {version[0]} v{version[1]}')
    if (variant_dir / _RENDER_ERROR).is_file():
        parts.append(f'RENDER FAILED: {(variant_dir / _RENDER_ERROR).read_text("utf-8").strip()}')
    review = variant_dir / _REVIEW
    if review.is_file():
        first = next(iter(review.read_text('utf-8').splitlines()), '')
        parts.append(f'review: {first}')
    return '; '.join(parts), version


if __name__ == '__main__':
    sys.exit(main())
