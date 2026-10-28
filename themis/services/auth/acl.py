"""The ACL: its format, its validation, and the evaluator both rpcs answer from (access-control.md).

The site writes one JSON document per deployment, for example::

    {
      "format": 1,
      "projects": {
        "asd": {"managers": ["john.doe@example.org"], "curators": ["jane.roe@example.com"]},
        "epi": {"managers": [], "curators": ["jane.roe@example.com", "john.doe@example.org"]}
      }
    }

:func:`parse` turns a version of it into a :class:`Snapshot`, or rejects the whole version with an
:class:`AclError` naming the first rule it breaks: a half-applied ACL is a wrong one. The rules:
``format`` is 1; the document has exactly the keys ``format`` and ``projects``, each Project exactly
``managers`` and ``curators``, and no object repeats a key; a Project names at least one person in one
of the two; every email is ``local@domain`` with one ``@`` and no character that lowercases to another;
a Project's id is a lowercase slug (ASCII letters, digits and hyphens, not starting with a hyphen); no
string has surrounding whitespace; and no list repeats an entry. One person may be in both lists of a
Project. A Project is one dataset, and its id is the dataset's name, which is also what Themis shows
for it and puts in its URLs.

The ACL has no list of admitted people: a person is admitted to the deployment by holding a role on
at least one Project. The snapshot holds the ACL as the site wrote it. The evaluator adds one caller
the ACL never lists: the deployment's operator account, the ``CALLER_CLU`` principal, which it grants
both roles on every Project the ACL holds. Who may act as that account is IAM's to decide, in Themis's
own infrastructure.

An email matches case-insensitively in ASCII alone. Unicode lowercasing would fold some non-ASCII
characters into ASCII ones (the Kelvin sign into ``k``), letting one address match another. The ACL
itself is held to Unicode lowercase, so an address with a non-ASCII capital, which ASCII matching could
never match, rejects the ACL rather than silently admitting no one.
"""

from __future__ import annotations

import dataclasses
import json
import re
import types
from collections.abc import Iterable, Mapping, Sequence

from themis.rpc import auth_pb2

FORMAT = 1

_ASCII_LOWER = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')

_DOCUMENT_KEYS = frozenset({'format', 'projects'})
_PROJECT_KEYS = frozenset({'managers', 'curators'})
_EVERY_ROLE = (auth_pb2.ROLE_MANAGER, auth_pb2.ROLE_CURATOR)
_PROJECT_ID = re.compile(r'[a-z0-9][a-z0-9-]*')


class AclError(ValueError):
    """A version of the ACL breaks a rule; the message names the rule and where."""


@dataclasses.dataclass(frozen=True)
class Project:
    """One Project as the ACL states it.

    Attributes:
        id: The Project's key in the ACL, the name of the dataset it is.
        managers: The lowercase emails of the people holding the manager role.
        curators: The lowercase emails of the people holding the curator role.
    """

    id: str
    managers: frozenset[str]
    curators: frozenset[str]

    def roles_of(self, email: str) -> list[auth_pb2.Role]:
        """The roles the lowercase ``email`` holds here, manager first; empty for someone on neither list."""
        return [
            role
            for role, holders in ((auth_pb2.ROLE_MANAGER, self.managers), (auth_pb2.ROLE_CURATOR, self.curators))
            if email in holders
        ]

    def grant(self, roles: Iterable[auth_pb2.Role] = ()) -> auth_pb2.ProjectGrant:
        """The grant this Project answers with, carrying ``roles``; a message of its own on each call."""
        return auth_pb2.ProjectGrant(id=self.id, roles=roles)


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """One validated version of the ACL, never changed once built; the evaluator over it.

    Attributes:
        projects: Each Project by its id.
    """

    projects: Mapping[str, Project]

    def access(self, email: str, *, operator: str) -> auth_pb2.GetAccessResponse:
        """What the caller behind ``email`` may reach, matched case-insensitively.

        Args:
            email: The person's or account's email.
            operator: The email of the deployment's operator account, which holds both roles on every
                Project in the ACL whether or not the ACL lists it.

        Returns:
            A grant per Project on which the caller holds a role, by Project id, carrying those roles:
            both roles on every Project for the operator account. No grant at all means the caller is not
            admitted.
        """
        email = ascii_lower(email)
        everything = email == ascii_lower(operator)
        grants = []
        for pid in sorted(self.projects):
            project = self.projects[pid]
            roles = list(_EVERY_ROLE) if everything else project.roles_of(email)
            if roles:
                grants.append(project.grant(roles))
        return auth_pb2.GetAccessResponse(projects=grants)

    def grant(self, project_id: str) -> auth_pb2.ProjectGrant | None:
        """The roleless grant a session carries for ``project_id``, or ``None`` if the ACL has no such Project."""
        project = self.projects.get(project_id)
        return None if project is None else project.grant()


def parse(raw: bytes) -> Snapshot:
    """Validate one version of the ACL into a :class:`Snapshot`.

    Raises:
        AclError: The bytes are not UTF-8 JSON, or the document breaks a rule of the format.
    """
    try:
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_keys)
    except UnicodeDecodeError as e:
        raise AclError(f'not UTF-8: {e}') from e
    except AclError:
        raise
    # ValueError covers JSONDecodeError and an integer past the int-string conversion limit.
    except (ValueError, RecursionError) as e:
        raise AclError(f'not JSON this reader can decode: {e}') from e
    top = _object(document, 'the ACL', _DOCUMENT_KEYS)
    if type(top['format']) is not int or top['format'] != FORMAT:
        raise AclError(f'format is {top["format"]!r}; this reader understands format {FORMAT}')
    projects_doc = top['projects']
    if not isinstance(projects_doc, dict):
        raise AclError(f'projects is a {type(projects_doc).__name__}, not an object')
    projects = {pid: _project(pid, entry) for pid, entry in projects_doc.items()}
    return Snapshot(projects=types.MappingProxyType(projects))


def ascii_lower(email: str) -> str:
    """``email`` with its ASCII letters lowercased and every other character as it was."""
    return email.translate(_ASCII_LOWER)


def _unique_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    repeated = _repeated(key for key, _ in pairs)
    if repeated:
        raise AclError(f'an object repeats the key(s) {repeated}')
    return dict(pairs)


def _object(value: object, where: str, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AclError(f'{where} is a {type(value).__name__}, not an object')
    unknown, missing = sorted(value.keys() - keys), sorted(keys - value.keys())
    if unknown:
        raise AclError(f'{where} has unknown key(s) {unknown}')
    if missing:
        raise AclError(f'{where} lacks key(s) {missing}')
    return value


def _strings(value: object, where: str) -> list[str]:
    """A list of non-empty strings with no entry repeated."""
    if not isinstance(value, list):
        raise AclError(f'{where} is a {type(value).__name__}, not a list')
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry:
            raise AclError(f'{where}[{index}] is {entry!r}, not a non-empty string')
        if entry != entry.strip():
            raise AclError(f'{where}[{index}] {entry!r} has surrounding whitespace')
    repeated = _repeated(value)
    if repeated:
        raise AclError(f'{where} repeats {repeated}')
    return value


def _repeated(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        (repeated if value in seen else seen).add(value)
    return sorted(repeated)


def _emails(value: object, where: str) -> frozenset[str]:
    emails = _strings(value, where)
    for index, email in enumerate(emails):
        local, _, domain = email.rpartition('@')
        if email.count('@') != 1 or not local or not domain or any(c.isspace() for c in email):
            raise AclError(f'{where}[{index}] {email!r} is not an email address')
        if any(c.lower() != c for c in email):
            raise AclError(f'{where}[{index}] {email!r} is not lowercase')
    return frozenset(emails)


def _project(pid: str, entry: object) -> Project:
    if not _PROJECT_ID.fullmatch(pid):
        raise AclError(
            f'projects has the Project id {pid!r}, not a lowercase slug of ASCII letters, digits and hyphens'
        )
    where = f'projects[{pid!r}]'
    lists = _object(entry, where, _PROJECT_KEYS)
    managers = _emails(lists['managers'], f'{where}.managers')
    curators = _emails(lists['curators'], f'{where}.curators')
    if not managers and not curators:
        raise AclError(f'{where} has no manager and no curator; a Project names at least one person')
    return Project(id=pid, managers=managers, curators=curators)
