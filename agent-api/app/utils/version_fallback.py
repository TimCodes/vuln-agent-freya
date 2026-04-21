"""Fallback-version selection for failed `npm install <pkg>@<ver>` attempts.

When the executor asks npm to install a version that doesn't exist on the
registry (e.g. the planner invented `handlebars@4.8.0` when only `4.7.x` is
published), npm fails with ETARGET. Rather than leave the package
unremediated, the executor asks the Tools API for the list of real published
versions and retries with the lowest-impact alternative.

"Lowest impact" means: strictly newer than what's installed, and as close to
the currently installed version as possible — a patch bump on the same
major.minor line beats a minor bump, which beats a major bump. Only stable
releases are considered; prerelease tags (``1.0.0-beta.1``) are skipped
because an automated agent shouldn't ship a beta without human review.
"""
from __future__ import annotations

import re


# Strict MAJOR.MINOR.PATCH — prereleases and build metadata are deliberately
# excluded so the fallback picker never suggests a beta/rc.
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _parse(version: str) -> tuple[int, int, int] | None:
    m = _VERSION_RE.match(version.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_etarget_error(stderr: str) -> bool:
    """True iff the npm error text indicates the requested version doesn't exist."""
    if not stderr:
        return False
    lowered = stderr.lower()
    return "etarget" in lowered or "no matching version found" in lowered


def pick_fallback_candidates(
    available: list[str],
    current: str | None,
    failed_target: str,
    limit: int = 3,
) -> list[str]:
    """Rank fallback versions to try after `failed_target` returned ETARGET.

    Returns up to `limit` candidates, best-first. Callers retry each in
    order until one installs.
    """
    stable: list[tuple[tuple[int, int, int], str]] = []
    for v in available:
        parsed = _parse(v)
        if parsed is not None:
            stable.append((parsed, v))
    if not stable:
        return []

    current_parsed = _parse(current or "")
    target_parsed = _parse(failed_target)

    # Only upgrades count — going backwards won't remediate the vuln.
    if current_parsed:
        stable = [s for s in stable if s[0] > current_parsed]
    # Skip the exact version we just failed on, even if (somehow) it is now
    # listed, to avoid loops.
    if target_parsed:
        stable = [s for s in stable if s[0] != target_parsed]

    if not stable:
        return []

    def _rank(parsed: tuple[int, int, int]) -> tuple[int, tuple[int, int, int]]:
        # Lower rank = tried first.
        if current_parsed:
            if parsed[0] == current_parsed[0] and parsed[1] == current_parsed[1]:
                bucket = 0  # same major.minor — patch bump
            elif parsed[0] == current_parsed[0]:
                bucket = 1  # same major — minor bump
            else:
                bucket = 2  # major bump (last resort)
        else:
            bucket = 0
        return (bucket, parsed)

    stable.sort(key=lambda s: _rank(s[0]))
    return [v for _, v in stable[:limit]]
