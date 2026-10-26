"""Create a task asking the worker to bring a paper up to date (`docs/design/evidence-fulltext.md`).

The task pushes `POST /convert` with a `WorkRequest` body to the worker, which runs off any request
path. It sits in litcache rather than in the evidence service because litcache is the package its
producers and its readers share; the evidence service reaches it through the literature backend port.

The task's name is built from the request (`task_id`), so a second create of the same request is an
`AlreadyExists` rather than a second run, while requests that would reach different work have names of
their own. A caller has to know two things about a name, both of them Cloud Tasks' documented
behaviour rather than this module's:

- **It outlives the task.** A name is refused for at least an hour after the task executed or was
  deleted, and the same reference elsewhere puts the release of the id at up to 24 hours (9 days for a
  queue created from a `queue.yaml`/`queue.xml`, which this one is not). So `enqueue` returning False
  does not mean a conversion is under way — it may mean one ran and failed within the window, and this
  name cannot re-drive it.
- **It is slower.** A named create pays a duplicate lookup an unnamed one does not, and the penalty
  grows with sequential ids. A `doc_id` is a uuid4 (`crosswalk.py`), which is the shape it is smallest
  for.
"""

from __future__ import annotations

import dataclasses
import logging
import re

import protovalidate
from google.api_core import exceptions as api_exceptions
from google.cloud import tasks_v2
from google.protobuf import duration_pb2

from themis.litcache import work
from themis.litcache.models import work_pb2

_logger = logging.getLogger(__name__)

_CONVERT_PATH = '/convert'

# A sweep pass is named for the minute it started, e.g. `20261021T1500`.
_SWEEP_PASS = re.compile(r'^[0-9]{8}T[0-9]{4}$')


@dataclasses.dataclass(frozen=True)
class ConversionTarget:
    """The queue a conversion task goes on and the identity it carries into the worker.

    Attributes:
        queue_path: The queue's resource name, `projects/{project}/locations/{location}/queues/{name}`.
        worker_url: The convert worker's base URL — both the `/convert` request's prefix and the OIDC
            token's audience, which Cloud Run validates against the service URL rather than the
            request path.
        invoker_service_account_email: The service account Cloud Tasks mints the task's OIDC token as;
            the one holding `run.invoker` on the worker.
    """

    queue_path: str
    worker_url: str
    invoker_service_account_email: str


def task_id(request: work_pb2.WorkRequest, *, generation: int | None = None, sweep_pass: str | None = None) -> str:
    """The task id for `request`: the `doc_id`, then what tells it apart from other requests for the paper.

    The parts, in order, each only when it applies: the sorted targets; `run-` and the funded run's
    id; `gen-` and the manifest generation the asking writer committed; `sweep-` and the sweep's
    pass. A target cannot hold a hyphen and a run id cannot hold `--`, so no part can be read as
    another, and `work.proto` restricts every field to the characters a task id allows.

    Args:
        request: The request the task carries.
        generation: The manifest generation the asking writer committed, for a writer's request.
        sweep_pass: The sweep pass asking, as `YYYYMMDDTHHMM`, for the sweep's request.

    Returns:
        The task id, e.g. `7c1e…--main_text` or `7c1e…--gen-5`.

    Raises:
        protovalidate.ValidationError: The request breaks a rule `work.proto` declares.
        ValueError: The request names no target and gives neither a generation nor a sweep pass, so
            its name would be held for a day against every later request naming no target; or the
            sweep pass is malformed.
    """
    protovalidate.validate(request)
    if not request.targets and generation is None and sweep_pass is None:
        raise ValueError(f'a request for {request.doc_id} naming no target needs a generation or a sweep pass')
    parts = [request.doc_id, *sorted(request.targets)]
    if request.HasField('run_id'):
        parts.append(f'run-{request.run_id}')
    if generation is not None:
        parts.append(f'gen-{generation}')
    if sweep_pass is not None:
        if not _SWEEP_PASS.fullmatch(sweep_pass):
            raise ValueError(f'malformed sweep pass {sweep_pass!r}')
        parts.append(f'sweep-{sweep_pass}')
    return '--'.join(parts)


def work_task(
    target: ConversionTarget,
    request: work_pb2.WorkRequest,
    *,
    generation: int | None = None,
    sweep_pass: str | None = None,
) -> tasks_v2.Task:
    """The task that carries `request` to the worker, named by `task_id`.

    Raises:
        protovalidate.ValidationError: The request breaks a rule `work.proto` declares.
        ValueError: As `task_id` raises.
    """
    base_url = target.worker_url.rstrip('/')  # a trailing slash would make the path `//convert`
    return tasks_v2.Task(
        name=f'{target.queue_path}/tasks/{task_id(request, generation=generation, sweep_pass=sweep_pass)}',
        dispatch_deadline=duration_pb2.Duration(seconds=int(work.DISPATCH_DEADLINE.total_seconds())),
        http_request=tasks_v2.HttpRequest(
            url=f'{base_url}{_CONVERT_PATH}',
            http_method=tasks_v2.HttpMethod.POST,
            headers={'Content-Type': work.WORK_REQUEST_MEDIA_TYPE},
            body=request.SerializeToString(),
            oidc_token=tasks_v2.OidcToken(
                service_account_email=target.invoker_service_account_email,
                audience=base_url,
            ),
        ),
    )


class Enqueuer:
    """A Cloud Tasks client bound to one conversion queue and worker.

    Client and target travel together so the lane is configured as a whole or not at all: a client
    without a target could not name a queue, and a target without a client could not reach one.
    """

    def __init__(self, client: tasks_v2.CloudTasksClient, target: ConversionTarget) -> None:
        self._client = client
        self._target = target

    def enqueue(
        self, request: work_pb2.WorkRequest, *, generation: int | None = None, sweep_pass: str | None = None
    ) -> bool:
        """Create the task carrying `request`, or report that its name is already taken.

        Args:
            request: What to ask the worker for.
            generation: As `task_id` takes it.
            sweep_pass: As `task_id` takes it.

        Returns:
            True when this call created the task. False when the name was already taken — a task in
            flight, or one that ran inside the id-reuse window. Not a failure, and not a promise that
            the work is coming (see the module docstring on the window).

        Raises:
            protovalidate.ValidationError: The request breaks a rule `work.proto` declares.
            ValueError: As `task_id` raises.
            google.api_core.exceptions.GoogleAPIError: every other Cloud Tasks failure, permanent
                (denied, no such queue, a malformed task) and transient (queue quota, an unreachable
                API) alike. Classifying them belongs to the caller, which is the only party that knows
                what a retry costs it.
        """
        task = work_task(self._target, request, generation=generation, sweep_pass=sweep_pass)
        try:
            created = self._client.create_task(parent=self._target.queue_path, task=task)
        except api_exceptions.AlreadyExists:
            _logger.info('task %s already exists; not enqueued again', task.name)
            return False
        _logger.info('enqueued task %s', created.name)
        return True
