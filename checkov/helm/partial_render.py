from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess  # nosec
import tempfile
import time
from pathlib import Path
from collections.abc import Callable, Sequence
from typing import Any

import yaml


def _dependency_error(diagnostic: str) -> str:
    # Helm can emit warnings before its final error. A template author's own
    # error message (from the Helm "fail" function) can contain newlines too.
    error = next((s for s in diagnostic.splitlines() if s.startswith('Error: ')), '')
    if (error.startswith(('Error: could not download ', "Error: can't get a valid version for ",
                          'Error: found in Chart.yaml, but missing in charts/ directory:'))
            or re.fullmatch(r'Error: directory .+ not found', error)):
        return error
    return ''


def _named_failures(error: str, missing: set[str], dependencies: list[dict[str, Any]], root: Path) -> set[str]:
    failed = set()
    for dep in dependencies:
        name = dep['name']
        if name not in missing:
            continue
        repository = dep.get('repository', '')
        if (repository.startswith('oci://')
                and error.startswith(f"Error: could not download {repository.rstrip('/')}/{name}:")):
            failed.add(name)
        elif repository.startswith('file://'):
            local_path = os.path.normpath(os.path.join(root, repository[7:]))
            if error == f'Error: directory {local_path} not found':
                failed.add(name)
        elif error.startswith("Error: can't get a valid version for ") and f'"{name}" (repository ' in error:
            failed.add(name)
    return failed


def _can_update(deps: list[dict[str, Any]], lock: Any, stage: Path) -> bool:
    for dep in deps:
        repository = dep.get('repository', '')
        if repository.startswith('file://'):
            path = Path(repository[7:])
            # Only resolve local references whose source is already in the checked copy.
            if (path.is_absolute() or not (stage / path).resolve().is_relative_to(stage.resolve())
                    or not (stage / path / 'Chart.yaml').is_file()):
                return False
        elif not repository.startswith(('https://', 'http://', 'oci://')):
            return False
        if lock is not None:
            # Helm updates every declaration when an archive is missing. Never
            # re-resolve locked ranges to a newer version in a reduced chart.
            if not isinstance(lock, dict) or not isinstance(lock.get('dependencies'), list):
                return False
            version = dep.get('version')
            if not isinstance(version, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?', version):
                return False
            if not any(isinstance(item, dict) and all(item.get(key) == dep.get(key)
                       for key in ('name', 'repository', 'version')) for item in lock['dependencies']):
                return False
    return True


def render_available_dependencies(
    chart_dir: str, helm_command: str, var_files: Sequence[str], timeout: int, diagnostic: str,
    blocked_repos: Callable[[str], list[str]],
) -> tuple[bytes, list[str]] | None:
    """Best-effort render of a v2 parent. The caller must always retain the original error.

    Only omit direct charts that Helm reports missing. Retained chart declarations
    keep their parent context, including conditions, aliases, and values overrides.
    If a missing library supplies a required helper, even the reduced render fails.
    """
    error = _dependency_error(diagnostic)
    if not error:
        return None

    deadline = time.monotonic() + timeout

    def invoke(args: list[str]) -> subprocess.CompletedProcess[bytes]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(helm_command, timeout)
        return subprocess.run([helm_command, *args], capture_output=True, timeout=remaining, check=False)  # nosec

    root = Path(chart_dir).absolute()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            stage = Path(temp_dir) / 'chart'
            if root.is_symlink() or stage.resolve().is_relative_to(root.resolve()):
                return None
            # Copy links as links, then reject them before opening any staged file
            # or invoking Helm. In particular Chart.yaml/Chart.lock must be private copies.
            shutil.copytree(root, stage, symlinks=True, ignore=shutil.ignore_patterns('.git'))
            if any(path.is_symlink() for path in stage.rglob('*')):
                return None
            if blocked_repos(str(stage)):
                return None
            metadata_path = stage / 'Chart.yaml'
            metadata = yaml.safe_load(metadata_path.read_bytes())
            if not isinstance(metadata, dict) or metadata.get('apiVersion') != 'v2':
                return None
            deps = metadata.get('dependencies')
            if not isinstance(deps, list) or not all(
                isinstance(dep, dict) and isinstance(dep.get('name'), str)
                and isinstance(dep.get('repository', ''), str) for dep in deps
            ):
                return None
            names = {dep['name'] for dep in deps}
            if len(names) != len(deps):  # aliases of the same chart need an unambiguous status per instance
                return None
            listing = invoke(['dependency', 'list', str(stage)])
            if listing.returncode != 0:
                return None
            rows = [[col.strip() for col in line.split('\t')]
                    for line in listing.stdout.decode('utf-8').splitlines()[1:] if line.strip()]
            if any(len(row) != 4 for row in rows) or {row[0] for row in rows} != names:
                return None
            missing = {row[0] for row in rows if row[3] == 'missing'}
            if not missing or any(row[3] not in ('ok', 'unpacked', 'missing') for row in rows):
                return None

            # The original lock describes the full set. Only the scratch copy's
            # lock may be regenerated by Helm after omitting declarations.
            lock_path = stage / 'Chart.lock'
            lock = yaml.safe_load(lock_path.read_bytes()) if lock_path.exists() else None
            if lock_path.exists() and not isinstance(lock, dict):
                return None
            lock_path.unlink(missing_ok=True)
            options = [arg for value in var_files for arg in ('--values', value)]

            def render(directory: Path, omitted: set[str], update: bool = False) -> tuple[bytes, list[str]] | None:
                metadata['dependencies'] = [dep for dep in deps if dep['name'] not in omitted]
                (directory / 'Chart.yaml').write_text(yaml.safe_dump(metadata, sort_keys=False), encoding='utf-8')
                args = ['template', *(['--dependency-update'] if update else []), str(directory), *options]
                proc = invoke(args)
                if proc.returncode == 0 and b'\n# Source: ' in proc.stdout:
                    return proc.stdout, sorted(omitted)
                return None

            failed = _named_failures(error, missing, deps, root)
            if failed and failed < missing and _can_update([dep for dep in deps if dep['name'] not in failed], lock, stage):
                # Helm's first update can fail atomically even after fetching some
                # archives. Try the remaining declared remotes together just once.
                # Keep the offline input untouched: an unsuccessful update may
                # leave downloaded archives that Helm would load as implicit charts.
                online_stage = Path(temp_dir) / 'online'
                shutil.copytree(stage, online_stage)
                recovered = render(online_stage, failed, update=True)
                if recovered:
                    return recovered
            return render(stage, missing)
    except (OSError, subprocess.SubprocessError, UnicodeError, yaml.YAMLError):
        logging.debug('Could not partially render Helm chart %s', chart_dir, exc_info=True)
        return None
