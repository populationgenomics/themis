"""The content check the worker hands sheaf's pre-receive hook: an agent's push keeps the user's judgements.

Named in the hook's environment (`git_hatches.PROTECTION`) as a `themis.sheaf.wire.protect.ContentCheck`: the rule over
each pushed commit is `themis.widgets.commits.check`, the reading the guest's asset helper mirrors. git runs against
the mirror through sheaf's own helper, so the check reads the objects the store will keep.
"""

from __future__ import annotations

import functools
import pathlib
from collections.abc import Sequence

from themis.sheaf.wire import bare
from themis.widgets import commits


def check(git_dir: pathlib.Path, commit: str, parents: Sequence[str]) -> list[str]:
    """Why `commit` may not land: each user's judgement an asset it changes breaks, empty when it keeps them all.

    Raises:
        RuntimeError: If git cannot read the commit or its parents.
    """
    return commits.check(functools.partial(bare.git, cwd=git_dir), commit, parents)
