"""A round's directory on the operator's machine, and the record of the settings it ran under.

Rounds live under `rounds/` beside this file, which is gitignored: what they hold quotes curators'
rationales and the registrations' clinical contexts, and every tracked file is mirrored to a public
repository. A round's directory holds `round.json` and one directory per variant; inside that, the
scenario half writes its reference and inputs, and the run half writes `run/`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import json
import os
import pathlib
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping

ROUNDS_DIR = pathlib.Path(__file__).resolve().parent / 'rounds'
ROUND_RECORD = 'round.json'
RUN_DIR = 'run'

# A JSON object as `json` reads it; `Fields` checks each value's type where a record is read from one.
Json = dict[str, object]

# Sortable by when the round started, since two rounds in a day are normal.
_ROUND_NAME = re.compile(r'\A\d{4}-\d{2}-\d{2}-\d{4}(?:-[a-z0-9][a-z0-9-]*)?\Z')


class RoundError(Exception):
    """A round's name, directory or records are not what the loop wrote."""


class Fields:
    """One JSON object read into a record, each field checked for its type as it is taken.

    Every getter raises `RoundError` naming the field and `where` the object came from, for a missing
    field or a value of another type; `finish` raises for a field nothing took, so a record written by
    a newer harness is refused rather than read in part.
    """

    def __init__(self, raw: object, where: str) -> None:
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise RoundError(f'{where} is not a JSON object')
        self._raw: Mapping[str, object] = raw
        self._where = where
        self._taken: set[str] = set()

    def _take(self, key: str) -> object:
        if key not in self._raw:
            raise RoundError(f'{self._where} has no `{key}`')
        self._taken.add(key)
        return self._raw[key]

    def _wrong(self, key: str, value: object, expected: str) -> RoundError:
        return RoundError(f'{self._where}: `{key}` is {expected}, not {type(value).__name__} {value!r}')

    def text(self, key: str) -> str:
        value = self._take(key)
        if not isinstance(value, str):
            raise self._wrong(key, value, 'a string')
        return value

    def optional_text(self, key: str) -> str | None:
        return None if self._take(key) is None else self.text(key)

    def integer(self, key: str) -> int:
        value = self._take(key)
        # A JSON `true` reads as a Python bool, which is an int.
        if not isinstance(value, int) or isinstance(value, bool):
            raise self._wrong(key, value, 'an integer')
        return value

    def flag(self, key: str) -> bool:
        value = self._take(key)
        if not isinstance(value, bool):
            raise self._wrong(key, value, 'true or false')
        return value

    def mapping(self, key: str) -> Json:
        value = self._take(key)
        if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
            raise self._wrong(key, value, 'an object')
        return value

    def optional_mapping(self, key: str) -> Json | None:
        return None if self._take(key) is None else self.mapping(key)

    def items(self, key: str) -> list[object]:
        value = self._take(key)
        if not isinstance(value, list):
            raise self._wrong(key, value, 'a list')
        return value

    def texts(self, key: str) -> list[str]:
        values = self.items(key)
        for value in values:
            if not isinstance(value, str):
                raise self._wrong(key, value, 'a list of strings')
        return [str(value) for value in values]

    def mappings(self, key: str) -> list[Json]:
        values = self.items(key)
        for value in values:
            if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
                raise self._wrong(key, value, 'a list of objects')
        return [Fields(value, f'{self._where}: `{key}`').as_json() for value in values]

    def as_json(self) -> Json:
        """The object whole, read as it is: for a field the record keeps as raw JSON."""
        return dict(self._raw)

    def finish(self) -> None:
        unread = sorted(set(self._raw) - self._taken)
        if unread:
            raise RoundError(f'{self._where} holds fields this harness does not read: {unread}')


@dataclasses.dataclass(frozen=True)
class Settings:
    """What a round runs against and under, fixed when it is prepared and recorded with it.

    `deadline` and `settle` are per-round values (docs/design/run-review-loop.md), so they are stated
    here once and every run of the round reads them from the record.
    """

    gcp_project: str
    service_account: str
    web_origin: str
    workbench_project: str
    deadline_seconds: int
    settle_seconds: int
    prepared_at: str

    @property
    def deadline(self) -> datetime.timedelta:
        return datetime.timedelta(seconds=self.deadline_seconds)

    @property
    def settle(self) -> datetime.timedelta:
        return datetime.timedelta(seconds=self.settle_seconds)

    @classmethod
    def from_json(cls, raw: object, where: str) -> Settings:
        """Read back what `create` recorded.

        Raises:
            RoundError: If a field is missing, of another type, or not one a round records.
        """
        fields = Fields(raw, where)
        settings = cls(
            gcp_project=fields.text('gcp_project'),
            service_account=fields.text('service_account'),
            web_origin=fields.text('web_origin'),
            workbench_project=fields.text('workbench_project'),
            deadline_seconds=fields.integer('deadline_seconds'),
            settle_seconds=fields.integer('settle_seconds'),
            prepared_at=fields.text('prepared_at'),
        )
        fields.finish()
        return settings


def round_dir(name: str) -> pathlib.Path:
    """The directory of the round `name`, under `ROUNDS_DIR`.

    Raises:
        RoundError: If the name does not start with the date and time the round started.
    """
    if not _ROUND_NAME.match(name):
        raise RoundError(f'round name {name!r} must start YYYY-MM-DD-HHMM, optionally followed by -words')
    return ROUNDS_DIR / name


@contextlib.contextmanager
def staged(directory: pathlib.Path) -> Iterator[pathlib.Path]:
    """A new directory beside `directory` to prepare a round in, renamed onto it when the block completes.

    A block that raises leaves nothing behind, so a round directory is either whole or absent.

    Raises:
        RoundError: If `directory` exists, or appeared while the block ran.
    """
    if directory.exists():
        raise RoundError(f'{directory} exists; name a new round')
    directory.parent.mkdir(parents=True, exist_ok=True)
    stage = pathlib.Path(tempfile.mkdtemp(prefix=f'.{directory.name}.', dir=directory.parent))
    try:
        yield stage
        try:
            stage.rename(directory)
        except OSError as e:
            raise RoundError(f'{directory} appeared while the round was prepared: {e}') from e
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def create(directory: pathlib.Path, settings: Settings) -> None:
    """Start a round in `directory`, recording its settings.

    Raises:
        RoundError: If the directory already holds a round.
    """
    if (directory / ROUND_RECORD).exists():
        raise RoundError(f'{directory} already holds a round; name a new one')
    write_json(directory / ROUND_RECORD, dataclasses.asdict(settings))


def settings(directory: pathlib.Path) -> Settings:
    """The settings `directory`'s round was prepared with.

    Raises:
        RoundError: If the directory holds no round, or its record is not one.
    """
    raw = read_json(directory / ROUND_RECORD)
    if raw is None:
        raise RoundError(f'{directory} holds no round; `prepare` starts one')
    return Settings.from_json(raw, str(directory / ROUND_RECORD))


def variant_dirs(directory: pathlib.Path) -> list[pathlib.Path]:
    """The round's variant directories, in name order."""
    return sorted(path for path in directory.iterdir() if path.is_dir())


def publish(path: pathlib.Path, data: bytes) -> None:
    """Write beside the target and rename onto it, so a truncated file never passes for a whole one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f'.{path.name}.{os.getpid()}')
    try:
        staged.write_bytes(data)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def write_json(path: pathlib.Path, document: object) -> None:
    publish(path, (json.dumps(document, indent=1, sort_keys=True) + '\n').encode('utf-8'))


def read_json(path: pathlib.Path) -> Json | None:
    """The JSON object at `path`, or None where there is no file.

    Raises:
        RoundError: If the file is not a JSON object.
    """
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text('utf-8'))
    except json.JSONDecodeError as e:
        raise RoundError(f'{path} is not JSON: {e}') from e
    return Fields(document, str(path)).as_json()
