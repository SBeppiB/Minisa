#!/usr/bin/env python3
"""Build the candidate release manifest from the base release and the tree.

The manifest is the reviewable contract of a staged, not-yet-deployed release:
which runtime files differ from the base release, what the release expects from
the database, and which systemd drop-in would be installed later.

The builder never copies anything. It compares the base release with the
working tree, checks that the staged release already contains exactly those
files, and writes the manifest. A mismatch is an error, not a warning.

    ops/build_candidate_manifest.py \
        --base /home/sbeppi/minisa-releases/response-context-20261017T010152Z \
        --release /home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021 \
        --tree /home/sbeppi/minisa-working-assistant-20261018/candidate \
        --dropin deploy/minisa-learning-cycle-timeout.conf.candidate \
        --out /home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021.manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

RUNTIME_ROOTS = ('core', 'runtime')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def runtime_files(root: Path) -> dict:
    found = {}
    for name in RUNTIME_ROOTS:
        directory = root / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts:
                continue
            if any(part.endswith('.bak') or '.bak-' in part for part in path.parts):
                continue
            found[path.relative_to(root).as_posix()] = path
    return found


def compare(base: Path, tree: Path) -> tuple:
    base_files = runtime_files(base)
    tree_files = runtime_files(tree)
    changed, added = [], []
    for relative, path in sorted(tree_files.items()):
        if relative not in base_files:
            added.append(relative)
        elif sha256(base_files[relative]) != sha256(path):
            changed.append(relative)
    removed = [relative for relative in sorted(base_files) if relative not in tree_files]
    return changed, added, removed


def build(arguments) -> dict:
    base = Path(arguments.base).resolve(strict=True)
    release = Path(arguments.release).resolve(strict=True)
    tree = Path(arguments.tree).resolve(strict=True)
    dropin = Path(arguments.dropin).resolve(strict=True)

    changed, added, removed = compare(base, tree)
    if removed:
        raise ValueError(f"the tree removes runtime files from the base release: {removed}")
    entries = []
    for relative in sorted(changed + added):
        source = tree / relative
        staged = release / relative
        if not staged.is_file():
            raise ValueError(f"staged release is missing {relative}")
        payload = source.read_bytes()
        staged_payload = staged.read_bytes()
        if staged_payload != payload:
            raise ValueError(f"staged release differs from the tree: {relative}")
        entries.append({'path': relative, 'bytes': len(payload),
                        'sha256': hashlib.sha256(payload).hexdigest()})
    manifest = {
        'base_release': str(base),
        'candidate_release': str(release),
        'candidate_supported_schema_version': arguments.supported_schema_version,
        'candidate_systemd_dropin': {
            'planned_install_path': arguments.planned_install_path,
            'sha256': sha256(dropin),
            'source': str(dropin),
        },
        'changed_runtime_files': entries,
        'live_global_revision_at_check': arguments.live_global_revision,
        'live_schema_version': arguments.live_schema_version,
        'migration_required': arguments.migration_required,
        'migration_note': arguments.migration_note,
        'core_service_migration_required': arguments.core_service_migration_required,
        'core_service_note': arguments.core_service_note,
        'production_deployed': False,
        'qualification_route_enabled_by_production_config': False,
        'resolved_candidate_dream_timeout_seconds': arguments.dream_timeout_seconds,
    }
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--tree', required=True)
    parser.add_argument('--dropin', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--supported-schema-version', type=int, default=5)
    parser.add_argument('--live-schema-version', type=int, default=4)
    parser.add_argument('--live-global-revision', type=int, default=0)
    parser.add_argument('--dream-timeout-seconds', type=int, default=40)
    parser.add_argument('--planned-install-path',
                        default='/etc/systemd/system/minisa.service.d/20-dream-recovery.conf')
    parser.add_argument('--migration-required', action='store_true', default=False)
    parser.add_argument('--core-service-migration-required', action='store_true', default=False)
    parser.add_argument('--migration-note', default='')
    parser.add_argument('--core-service-note', default='')
    arguments = parser.parse_args()
    try:
        manifest = build(arguments)
    except Exception as error:
        print(f"manifest build failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    Path(arguments.out).write_text(json.dumps(manifest, indent=1, sort_keys=True) + '\n')
    print(json.dumps({'manifest': arguments.out,
                      'changed_runtime_files': len(manifest['changed_runtime_files']),
                      'migration_required': manifest['migration_required'],
                      'production_deployed': manifest['production_deployed']}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
