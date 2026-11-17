"""Generate a `docker buildx bake` definition and `$GITHUB_ENV` bindings from `.github/images.json`.

`deploy.yml` builds and pushes every declared service and job image in one `docker buildx bake --push`
pass, reading and writing each image's BuildKit layer cache under `<image_prefix>/cache-<name>:latest`.
Keeping cache manifests in `cache-*` packages leaves application image packages (`<image_prefix>/<name>`)
untouched by the registry's untagged-cache cleanup policy (`infra/themis_infra/baseline.py`).
"""

from __future__ import annotations

import argparse
import collections.abc
import dataclasses
import json
import pathlib
import re
import sys

# Coupled with `infra/themis_infra/baseline.py` (`BUILD_CACHE_PACKAGE_PREFIX`).
CACHE_PACKAGE_PREFIX = 'cache-'

_REQUIRED_KEYS = ('name', 'context', 'file', 'env')
_ENV_RE = re.compile(r'^THEMIS_[A-Z0-9_]+_IMAGE$')
_SAFE_NAME_CHARS = frozenset('abcdefghijklmnopqrstuvwxyz0123456789-')
_SAFE_RELPATH_RE = re.compile(r'^[A-Za-z0-9._/-]+$')
_IMAGE_PREFIX_RE = re.compile(r'^[a-z0-9._-]+(?::[0-9]+)?(?:/[a-z0-9._-]+)+$')
_IMAGE_TAG_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$')


@dataclasses.dataclass(frozen=True)
class ImageSpec:
    """One service or job image entry from `.github/images.json`."""

    name: str
    context: str
    dockerfile_in_context: str
    env: str


def _require_non_empty_str(entry: collections.abc.Mapping[str, object], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f'image entry must carry a non-empty string {key!r}: {entry!r}')
    return value


def _validate_repo_relpath(raw: str, *, label: str, allow_dot: bool) -> pathlib.PurePosixPath:
    if not _SAFE_RELPATH_RE.fullmatch(raw):
        raise ValueError(f'{label} must be a normalized repo-relative path, got {raw!r}')
    candidate = pathlib.PurePosixPath(raw)
    if candidate.is_absolute() or '..' in candidate.parts or any(part.startswith('-') for part in candidate.parts):
        raise ValueError(f'{label} must be a normalized repo-relative path, got {raw!r}')
    is_canonical = (allow_dot and raw == '.') or (raw != '.' and not raw.startswith('./') and str(candidate) == raw)
    if not is_canonical:
        raise ValueError(f'{label} must be a normalized repo-relative path, got {raw!r}')
    return candidate


def _dockerfile_relative_to_context(dockerfile: str, context: str) -> str:
    """Resolve repo-relative `dockerfile` relative to repo-relative `context`, as `buildx bake` reads it.

    `docker build -f <file> <context>` resolves `<file>` from the caller's working directory, whereas
    `docker buildx bake` resolves a target's `dockerfile` inside its `context`.

    Raises:
        ValueError: If either path is non-canonical, uses a URL/scheme, traverses `..`, or `dockerfile`
            lies outside `context`.
    """
    file_path = _validate_repo_relpath(dockerfile, label='file', allow_dot=False)
    context_path = _validate_repo_relpath(context, label='context', allow_dot=True)
    try:
        relative = file_path.relative_to(context_path)
    except ValueError as e:
        raise ValueError(f'dockerfile {dockerfile!r} must lie inside build context {context!r}') from e
    if str(relative) == '.':
        raise ValueError(f'dockerfile {dockerfile!r} names the context directory {context!r}, not a file')
    return str(relative)


def _validate_coordinates(*, image_prefix: str, tag: str) -> tuple[str, str]:
    prefix = image_prefix.rstrip('/')
    if not _IMAGE_PREFIX_RE.fullmatch(prefix):
        raise ValueError(f'invalid image_prefix {image_prefix!r}')
    if not _IMAGE_TAG_RE.fullmatch(tag):
        raise ValueError(f'invalid image tag {tag!r}')
    return prefix, tag


def parse_manifest(raw_entries: object) -> list[ImageSpec]:
    """Validate decoded JSON from `.github/images.json` into `ImageSpec` records.

    Raises:
        ValueError: If the manifest is empty, malformed, duplicates a name or env var, uses the
            reserved `cache-` package prefix, or places a Dockerfile outside its context.
    """
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError('image manifest must be a non-empty JSON array')

    specs: list[ImageSpec] = []
    seen_names: set[str] = set()
    seen_envs: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError(f'image entry must be a JSON object, got {raw!r}')
        values = {key: _require_non_empty_str(raw, key) for key in _REQUIRED_KEYS}
        name = values['name']
        env = values['env']
        if not set(name) <= _SAFE_NAME_CHARS or name.startswith('-') or name.endswith('-'):
            raise ValueError(f'invalid image name {name!r}')
        if name.startswith(CACHE_PACKAGE_PREFIX):
            raise ValueError(f'image name {name!r} starts with reserved build-cache prefix {CACHE_PACKAGE_PREFIX!r}')
        if not _ENV_RE.fullmatch(env):
            raise ValueError(f'invalid image env var name {env!r}')
        if name in seen_names:
            raise ValueError(f'duplicate image name {name!r}')
        if env in seen_envs:
            raise ValueError(f'duplicate image env var {env!r}')
        seen_names.add(name)
        seen_envs.add(env)
        dockerfile_in_context = _dockerfile_relative_to_context(values['file'], values['context'])
        specs.append(
            ImageSpec(
                name=name,
                context=values['context'],
                dockerfile_in_context=dockerfile_in_context,
                env=env,
            )
        )
    return specs


def bake_definition(specs: collections.abc.Sequence[ImageSpec], *, image_prefix: str, tag: str) -> dict[str, object]:
    """Build the `docker buildx bake` JSON structure for `specs`.

    Args:
        specs: Validated image declarations.
        image_prefix: Artifact Registry prefix (`<region>-docker.pkg.dev/<project>/themis`).
        tag: Immutable tag for this commit's images (`GITHUB_SHA`).

    Returns:
        A JSON-serializable mapping with `group.default` and one target per `ImageSpec`.

    Raises:
        ValueError: If `specs` is empty or `image_prefix` / `tag` is not a valid OCI coordinate.
    """
    if not specs:
        raise ValueError('specs must not be empty')
    prefix, valid_tag = _validate_coordinates(image_prefix=image_prefix, tag=tag)

    targets: dict[str, dict[str, object]] = {}
    for spec in specs:
        cache_ref = f'{prefix}/{CACHE_PACKAGE_PREFIX}{spec.name}:latest'
        targets[spec.name] = {
            'context': spec.context,
            'dockerfile': spec.dockerfile_in_context,
            'platforms': ['linux/amd64'],
            'tags': [f'{prefix}/{spec.name}:{valid_tag}'],
            'cache-from': [f'type=registry,ref={cache_ref}'],
            # `mode=max` exports intermediate stages (`deps`, `build`, `guest`), and `image-manifest`
            # writes a single flat OCI manifest so Artifact Registry indexes and expires it cleanly.
            'cache-to': [f'type=registry,ref={cache_ref},mode=max,image-manifest=true,oci-mediatypes=true'],
        }
    return {
        'group': {'default': {'targets': [spec.name for spec in specs]}},
        'target': targets,
    }


def github_env_lines(specs: collections.abc.Sequence[ImageSpec], *, image_prefix: str, tag: str) -> str:
    """Format `THEMIS_*_IMAGE=<ref>` lines for `$GITHUB_ENV`.

    Raises:
        ValueError: If `specs` is empty or `image_prefix` / `tag` is not a valid OCI coordinate.
    """
    if not specs:
        raise ValueError('specs must not be empty')
    prefix, valid_tag = _validate_coordinates(image_prefix=image_prefix, tag=tag)
    return ''.join(f'{spec.env}={prefix}/{spec.name}:{valid_tag}\n' for spec in specs)


def main(argv: collections.abc.Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Generate a docker buildx bake file from images.json.')
    parser.add_argument('--manifest', type=pathlib.Path, required=True, help='path to .github/images.json')
    parser.add_argument('--image-prefix', required=True, help='Artifact Registry repository prefix')
    parser.add_argument('--tag', required=True, help='commit SHA tag for built images')
    parser.add_argument('--bake-out', type=pathlib.Path, required=True, help='output path for bake JSON')
    parser.add_argument('--env-out', type=pathlib.Path, required=True, help='output path for GITHUB_ENV lines')
    args = parser.parse_args(argv)

    try:
        specs = parse_manifest(json.loads(args.manifest.read_text('utf-8')))
        bake = bake_definition(specs, image_prefix=args.image_prefix, tag=args.tag)
        env_text = github_env_lines(specs, image_prefix=args.image_prefix, tag=args.tag)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f'error: {e}', file=sys.stderr)
        sys.exit(1)

    args.bake_out.write_text(json.dumps(bake, indent=2) + '\n', 'utf-8')
    args.env_out.write_text(env_text, 'utf-8')


if __name__ == '__main__':
    main()
