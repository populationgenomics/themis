"""The names a work request and its worker share (`docs/design/evidence-fulltext.md`).

Kept apart from the enqueuer so the worker can read them without the Cloud Tasks client.
"""

from __future__ import annotations

import datetime

# The target a request names to get a paper's main text by whichever route reaches it.
MAIN_TEXT_TARGET = 'main_text'

# The media type of a task body holding an encoded `WorkRequest`.
WORK_REQUEST_MEDIA_TYPE = 'application/x-protobuf'

# How long a delivery may run before the queue abandons it, and the worker's request timeout with it
# (`infra/themis_infra/convert.py`). A begun attempt older than this has been killed.
DISPATCH_DEADLINE = datetime.timedelta(minutes=30)
