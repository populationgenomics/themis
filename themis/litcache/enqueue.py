"""Create a paper's full-text conversion task on the `themis-convert` Cloud Tasks queue.

The producer half of the conversion lane (`docs/design/evidence-fulltext.md`): the task pushes
`POST /convert {"doc_id"}` to the convert worker, which runs `produce.produce_full_text` off any
request path. It sits in litcache rather than in the evidence service because litcache is the package
its producers and its readers share; the evidence service reaches it through the literature backend
port.

The task is named for the `doc_id`, so the second create for a paper is an `AlreadyExists` rather
than a second conversion. A caller has to know two things about that name, both of them Cloud Tasks'
documented behaviour rather than this module's:

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
import json
import logging

from google.api_core import exceptions as api_exceptions
from google.cloud import tasks_v2
from google.protobuf import duration_pb2

_logger = logging.getLogger(__name__)

# The per-task dispatch deadline, matched to the convert worker's Cloud Run request timeout
# (`infra/themis_infra/convert.py`). A shorter deadline abandons a conversion the worker is still
# running and dispatches a second attempt alongside it.
_DISPATCH_DEADLINE_SECONDS = 1800

_CONVERT_PATH = '/convert'


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


def conversion_task(target: ConversionTarget, doc_id: str) -> tasks_v2.Task:
    """The task that converts `doc_id`, named for it so a duplicate create is an `AlreadyExists`."""
    base_url = target.worker_url.rstrip('/')  # a trailing slash would make the path `//convert`
    return tasks_v2.Task(
        name=f'{target.queue_path}/tasks/{doc_id}',
        dispatch_deadline=duration_pb2.Duration(seconds=_DISPATCH_DEADLINE_SECONDS),
        http_request=tasks_v2.HttpRequest(
            url=f'{base_url}{_CONVERT_PATH}',
            http_method=tasks_v2.HttpMethod.POST,
            headers={'Content-Type': 'application/json'},
            body=json.dumps({'doc_id': doc_id}).encode('utf-8'),
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

    def enqueue(self, doc_id: str) -> bool:
        """Create `doc_id`'s conversion task, or report that its name is already taken.

        Args:
            doc_id: The paper to convert; also the task's id.

        Returns:
            True when this call created the task. False when the name was already taken — a task in
            flight, or one that ran inside the id-reuse window. Not a failure, and not a promise that
            a conversion is coming (see the module docstring on the window).

        Raises:
            google.api_core.exceptions.GoogleAPIError: every other Cloud Tasks failure, permanent
                (denied, no such queue, a malformed task) and transient (queue quota, an unreachable
                API) alike. Classifying them belongs to the caller, which is the only party that knows
                what a retry costs it.
        """
        try:
            task = self._client.create_task(parent=self._target.queue_path, task=conversion_task(self._target, doc_id))
        except api_exceptions.AlreadyExists:
            _logger.info('conversion task for %s already exists; not enqueued again', doc_id)
            return False
        _logger.info('enqueued conversion task %s', task.name)
        return True
