"""npm domain service.

Thin domain layer over `core/clients/npm_client.py`. Exposes only a
fixed set of subcommands (audit, audit fix, install <pkg>@<ver>,
ls) — never arbitrary npm invocations. Input validation happens at
the model layer.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..core.clients import npm_client
from ..core.config import settings


class NpmError(Exception):
    def __init__(self, message: str, stderr: str = "", exit_code: int | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code


def _run(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return npm_client.run(args, cwd=cwd, timeout=timeout)
    except npm_client.NpmClientError as e:
        raise NpmError(str(e), stderr=e.stderr) from e


def audit(repo_path: Path) -> dict:
    """Run `npm audit --json`. npm returns a non-zero exit code whenever
    vulnerabilities are found, so we do NOT treat non-zero as an error here;
    we parse the JSON regardless and return it with the exit code attached.
    """
    result = _run(
        ["npm", "audit", "--json"],
        cwd=repo_path,
        timeout=settings.npm_audit_timeout_seconds,
    )
    parsed: dict
    if result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise NpmError(
                "failed to parse npm audit JSON output",
                stderr=f"{e}\n--stdout--\n{result.stdout[:2000]}\n--stderr--\n{result.stderr[:2000]}",
                exit_code=result.returncode,
            )
    else:
        parsed = {}
    parsed["_exit_code"] = result.returncode
    parsed["_stderr"] = result.stderr
    return parsed


def install_all(repo_path: Path) -> subprocess.CompletedProcess[str]:
    """Run `npm install` to populate node_modules and refresh the lockfile.
    `npm audit` needs a resolved dependency tree to report transitive
    vulnerabilities; on a freshly cloned repo without node_modules, audit
    output is incomplete. --no-audit avoids a redundant audit pass here;
    --no-fund silences funding noise."""
    return _run(
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=repo_path,
        timeout=settings.npm_install_timeout_seconds,
    )


def audit_fix(repo_path: Path) -> subprocess.CompletedProcess[str]:
    """Run `npm audit fix`. Applies SemVer-compatible fixes only.
    `audit fix --force` is intentionally NOT exposed — force can introduce
    breaking changes and is not appropriate for an automated agent."""
    return _run(
        ["npm", "audit", "fix"],
        cwd=repo_path,
        timeout=settings.npm_install_timeout_seconds,
    )


def install_package(
    repo_path: Path, package: str, version: str, dev: bool = False
) -> subprocess.CompletedProcess[str]:
    """Install a single package at a specific version.

    Inputs are validated at the model layer (package/version regexes), and we
    still pass them as separate argv elements, so there's no shell expansion.
    """
    spec = f"{package}@{version}"
    args = ["npm", "install"]
    if dev:
        args.append("--save-dev")
    else:
        args.append("--save")
    # --no-audit: we'll run audit separately and consume its output as JSON.
    # --no-fund:  silence funding messages that clutter stdout.
    args += ["--no-audit", "--no-fund", spec]
    return _run(args, cwd=repo_path, timeout=settings.npm_install_timeout_seconds)


def list_versions(repo_path: Path, package: str) -> list[str]:
    """Return all versions published to the registry for `package`.

    Uses `npm view <pkg> versions --json`. Callers pass this through the
    fallback-version picker when a specific install target (e.g. an invented
    version) returns ETARGET, so the executor can retry with a real release.
    """
    result = _run(
        ["npm", "view", package, "versions", "--json"],
        cwd=repo_path,
        timeout=settings.npm_audit_timeout_seconds,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    # `npm view` returns a JSON array when multiple versions exist and a bare
    # string when only one is published. Normalize to a list either way.
    if isinstance(data, str):
        return [data]
    if isinstance(data, list):
        return [v for v in data if isinstance(v, str)]
    return []


def _installed_version_anywhere(repo_path: Path, package: str) -> str | None:
    """Return the highest installed version of `package` found anywhere in
    the dependency tree (top-level hoisted OR nested), or None.

    Unlike `installed_version`, which only inspects direct deps, this walks
    the full tree via `npm ls --all` so transitive packages are visible.
    When multiple copies coexist we pick the highest, since that's the one
    an `overrides` pin should match.
    """
    result = _run(
        ["npm", "ls", package, "--json", "--all"],
        cwd=repo_path,
        timeout=settings.npm_audit_timeout_seconds,
    )
    if not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    found: list[str] = []

    def _walk(node: dict) -> None:
        for name, info in (node.get("dependencies") or {}).items():
            if not isinstance(info, dict):
                continue
            if name == package:
                v = info.get("version")
                if isinstance(v, str):
                    found.append(v)
            _walk(info)

    _walk(data if isinstance(data, dict) else {})
    if not found:
        return None

    def _key(v: str) -> tuple:
        parts: list[int] = []
        for chunk in v.split("-", 1)[0].split("."):
            try:
                parts.append(int(chunk))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    return max(found, key=_key)


def resync_manifest(
    repo_path: Path, packages: list[str] | None = None,
) -> dict[str, list[str]]:
    """Make remediation visible in package.json for every package in the
    allow-list. Returns a dict with two keys:

      * ``rewritten``       — direct deps whose version spec was re-saved.
      * ``overrides_added`` — transitive deps pinned via the ``overrides``
                              field at the installed version.

    Why this exists
    ---------------
    `npm audit fix` only touches package-lock.json when the upgraded version
    already satisfies the range declared in package.json (e.g. fixing
    `protobufjs@7.5.4 → 7.5.5` under an existing `^7.x` range). The
    manifest then doesn't reflect which packages were actually patched,
    hiding the remediation in code review.

    For direct deps: re-run `npm install <pkg>@<installed> --save[-dev]`
    so the manifest's version spec is rooted at the installed version.
    For transitive deps: record the installed version under
    ``overrides[<pkg>]`` in package.json and run `npm install` once so the
    lockfile reflects the forced resolution. That pins the fix against
    future unrelated installs pulling the vulnerable version back in.
    """
    pkg_json_path = repo_path / "package.json"
    if not pkg_json_path.is_file():
        return {"rewritten": [], "overrides_added": []}
    try:
        manifest = json.loads(pkg_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"rewritten": [], "overrides_added": []}

    deps = manifest.get("dependencies") or {}
    dev_deps = manifest.get("devDependencies") or {}
    direct_names = set(deps.keys()) | set(dev_deps.keys())
    wanted = set(packages) if packages is not None else None

    rewritten: list[str] = []
    # Walk direct deps first: resave them with --save so package.json's
    # version spec is rooted at the installed version.
    for name, dev in [(n, False) for n in deps] + [(n, True) for n in dev_deps]:
        if wanted is not None and name not in wanted:
            continue
        try:
            version = installed_version(repo_path, name)
        except NpmError:
            continue
        if not version:
            continue
        try:
            result = install_package(repo_path, name, version, dev=dev)
        except NpmError:
            continue
        if result.returncode == 0:
            rewritten.append(name)

    # For transitive deps in the allow-list, write an entry in `overrides`
    # pinning the installed (already-remediated) version. Do this after the
    # direct-dep re-saves because each of those can rewrite package.json,
    # so we reload the manifest here to preserve those edits.
    try:
        manifest = json.loads(pkg_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"rewritten": rewritten, "overrides_added": []}

    transitive_targets: list[str] = []
    if wanted is not None:
        transitive_targets = sorted(wanted - direct_names)

    overrides_added: list[str] = []
    if transitive_targets:
        overrides = manifest.get("overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        for name in transitive_targets:
            version = _installed_version_anywhere(repo_path, name)
            if not version:
                continue
            # Only touch entries we set ourselves — don't overwrite user
            # overrides that may intentionally pin a different version.
            existing = overrides.get(name)
            if isinstance(existing, str) and existing == version:
                continue
            if existing is None or isinstance(existing, str):
                overrides[name] = version
                overrides_added.append(name)

        if overrides_added:
            manifest["overrides"] = overrides
            try:
                pkg_json_path.write_text(
                    json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
                )
            except OSError:
                return {"rewritten": rewritten, "overrides_added": []}
            # Reinstall so package-lock.json reflects the forced resolution.
            install_all(repo_path)

    return {"rewritten": rewritten, "overrides_added": overrides_added}


def installed_version(repo_path: Path, package: str) -> str | None:
    """Read the currently installed version of `package` from node_modules.

    Uses `npm ls <pkg> --json --depth=0`. Returns None if the package isn't
    a direct dependency of the project (or isn't installed)."""
    result = _run(
        ["npm", "ls", package, "--json", "--depth=0"],
        cwd=repo_path,
        timeout=settings.npm_audit_timeout_seconds,
    )
    if not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    deps = data.get("dependencies", {}) or {}
    entry = deps.get(package)
    if isinstance(entry, dict):
        ver = entry.get("version")
        return ver if isinstance(ver, str) else None
    return None
