"""Upload PR review screenshots and print the markdown to paste into the PR body.

``CLAUDE.md`` requires a rendered-surface change to ship before/after screenshots in its PR
description. GitHub has no API to attach an image, so the capture goes to the public-read
bucket ``infra/themis_infra/screenshots.py`` provisions and the PR body links it; GitHub's
Camo proxy fetches that URL anonymously and caches it (``docs/design/pr-screenshots.md``).

Run: ``uv run --group screenshot python -m tools.screenshot.upload after.png [before.png ...]``.
Each argument prints one ``![<stem>](<url>)`` line on stdout, in the order given; upload notes
go to stderr, so the whole stdout is pastable. Auth is the caller's own ``gcloud`` ADC — a
member of the group holding ``objectAdmin`` on the bucket, so a capture can also be retracted.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

from google.api_core import exceptions as api_exceptions
from google.cloud import storage

_DEFAULT_PROJECT = 'cpg-themis-dev'
_BUCKET_SUFFIX = 'pr-screenshots'
_PUBLIC_URL = 'https://storage.googleapis.com/{bucket}/{name}'
_CONTENT_TYPE = 'image/png'
_CACHE_CONTROL = 'public, max-age=31536000, immutable'
_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


def _png_bytes(path: pathlib.Path) -> bytes:
    """Read `path`, refusing anything the bucket would serve under the wrong content-type.

    Args:
        path: The capture to read.

    Returns:
        The file's bytes.

    Raises:
        SystemExit: If `path` is not a readable file, or is not a PNG.
    """
    if not path.is_file():
        raise SystemExit(f'{path}: not a file')
    data = path.read_bytes()
    if not data.startswith(_PNG_MAGIC):
        raise SystemExit(f'{path}: not a PNG; every object is served as {_CONTENT_TYPE}')
    return data


def _object_name(data: bytes) -> str:
    """The content-addressed object name for `data`."""
    return f'{hashlib.sha256(data).hexdigest()}.png'


def _markdown_link(alt: str, bucket: str, name: str) -> str:
    return f'![{alt}]({_PUBLIC_URL.format(bucket=bucket, name=name)})'


def _upload(bucket: storage.Bucket, name: str, data: bytes) -> bool:
    """Store `data` under `name`, leaving an object already there untouched.

    Args:
        bucket: The destination bucket.
        name: The object name, from `_object_name`.
        data: The PNG bytes `name` hashes.

    Returns:
        True if this call wrote the object, False if it was already stored.
    """
    blob = bucket.blob(name)
    blob.cache_control = _CACHE_CONTROL
    try:
        # `ifGenerationMatch=0` writes only when the object is absent, so the sole 412 is
        # "that hash is already stored".
        blob.upload_from_string(data, content_type=_CONTENT_TYPE, if_generation_match=0, checksum='crc32c')
    except api_exceptions.PreconditionFailed:
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('paths', nargs='+', type=pathlib.Path, metavar='PNG', help='screenshot(s) to upload')
    parser.add_argument(
        '--project',
        default=_DEFAULT_PROJECT,
        help='GCP project owning the bucket (default: %(default)s)',
    )
    args = parser.parse_args()

    # Every file is read and checked before anything is uploaded, and no link is printed
    # until every upload has succeeded, so a failure can't leave a half-written PR body.
    captures = [(path, _png_bytes(path)) for path in args.paths]

    bucket_name = f'{args.project}-{_BUCKET_SUFFIX}'
    bucket = storage.Client(project=args.project).bucket(bucket_name)
    links: list[str] = []
    for path, data in captures:
        name = _object_name(data)
        if not _upload(bucket, name, data):
            print(f'{path}: already stored as {name}', file=sys.stderr)
        links.append(_markdown_link(path.stem, bucket_name, name))
    print('\n'.join(links))


if __name__ == '__main__':
    main()
