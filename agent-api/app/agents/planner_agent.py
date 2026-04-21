"""Planner agent — decides what actions to take.

The planner consumes:
  * the scanner-reported vulnerabilities (VulnerabilityReport),
  * an `npm audit --json` payload for the workspace,

and returns a validated list of `FixAction` objects describing what the
executor should try. The heavy lifting is an LLM call with a tightly
constrained JSON-array output format; if the LLM is unavailable or returns
garbage, a deterministic heuristic fallback keeps the graph advancing.

Singleton usage
---------------
The LLM client is expensive to construct (it reads keys, sets up transport),
so the module exposes a single `planner_agent` instance that callers reuse:

    from app.agents import planner_agent
    plan, errors = await planner_agent.plan(reported, audit)

The agent's LLM is lazily built on first `plan()` call so importing this
module has no side effects. Test code can stub `planner_agent._llm` directly
or swap the whole singleton out.
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ..core import build_chat_llm, get_logger, settings
from ..schemas.models import FixAction, VulnerabilityReport

logger = get_logger("agent.planner")


PLANNER_SYSTEM = """You are the planning component of an automated vulnerability remediation system.

Given:
  1. A list of vulnerabilities reported by an external scanner.
  2. The output of `npm audit` for the affected project.

Produce a JSON fix plan. The plan is a JSON array of action objects.

Allowed action types:
  - {"type": "npm_audit_fix", "reason": "<why>", "addresses": ["<id>", ...]}
      Run `npm audit fix`. Use this when the vulnerabilities are resolvable
      by SemVer-compatible upgrades (npm audit itself reports `fixAvailable`
      with no breaking-change flag).
  - {"type": "package_update", "package": "<name>", "target_version": "<semver>",
     "reason": "<why>", "addresses": ["<id>", ...]}
      Update a single package to a specific version. Use this when the scanner
      reported a `fixed_version` that `npm audit fix` would not apply
      automatically (e.g. a major version bump), or when the scanner report
      identifies a package that `npm audit` does not cover.

Rules:
  - Output ONLY a JSON array. No prose, no markdown fences, no preamble.
  - Prefer a single `npm_audit_fix` action over many individual updates when
    `npm audit` reports that fixes are available.
  - For `package_update`, `target_version` must be an exact version or a
    standard SemVer range. Do NOT invent versions that are not supported by
    the scanner report or the audit output.
  - If there is nothing to do, return [].
  - Each action SHOULD populate `addresses` with the vulnerability IDs it
    resolves so the final report can trace coverage.

Major-version upgrades (IMPORTANT):
  - `npm audit fix` will NOT apply upgrades that cross a major-version
    boundary, because they can introduce breaking changes. We intentionally
    do not use `--force`.
  - When a vulnerability entry in `npm_audit.vulnerabilities` has
    `fixAvailable` as an object with `isSemVerMajor: true`, you MUST emit a
    `package_update` action using the `version` field from that object as
    `target_version`. Do NOT rely on `npm_audit_fix` to resolve it.
  - When `fixAvailable` is `true` (a bare boolean) or an object with
    `isSemVerMajor: false`, a single `npm_audit_fix` is sufficient.
  - You may combine one `npm_audit_fix` action (for the non-major fixes)
    with several `package_update` actions (one per major bump) in the same
    plan.
"""


class PlannerAgent:
    """Builds a validated fix plan from reported vulns + npm audit output.

    Responsibilities
    ----------------
    * Assemble a compact JSON payload for the LLM (the full audit tree is
      trimmed; only the fields the planner actually needs are sent).
    * Call the LLM and extract a JSON array from its reply, tolerating
      common decorations like ```json fences.
    * Fall back to a deterministic heuristic plan if the LLM call fails.
    * Validate every raw action through `FixAction` and drop malformed ones,
      collecting errors for the caller.
    * Enforce `settings.max_targeted_updates_per_run` as a hard cap on
      `package_update` actions per run.

    The agent does not mutate the filesystem and never calls the Tools API.
    It is a pure "think" step whose output is handed to the executor node.
    """

    def __init__(self) -> None:
        self._llm: BaseChatModel | None = None

    def _get_llm(self) -> BaseChatModel:
        """Lazy-build the LLM client on first use."""
        if self._llm is None:
            self._llm = build_chat_llm()
        return self._llm

    def build_user_payload(
        self,
        reported: list[VulnerabilityReport],
        audit: dict[str, Any],
    ) -> dict[str, Any]:
        """Shape the planner input. The audit's `vulnerabilities` tree is
        trimmed to just the fields the planner actually reasons about —
        the raw tree can be enormous on real projects."""
        return {
            "reported_vulnerabilities": [v.model_dump() for v in reported],
            "npm_audit": {
                "metadata": audit.get("metadata", {}),
                "vulnerabilities": {
                    name: {
                        "severity": v.get("severity"),
                        "via": [
                            x if isinstance(x, str) else x.get("title")
                            for x in (v.get("via") or [])
                        ],
                        "effects": v.get("effects"),
                        "range": v.get("range"),
                        "fixAvailable": v.get("fixAvailable"),
                    }
                    for name, v in (audit.get("vulnerabilities") or {}).items()
                },
            },
        }

    def extract_json_array(self, text: str) -> list[dict]:
        """Robustly pull a JSON array out of the model's reply. Strips common
        code-fence decorations and trims to the first `[ ... ]` block."""
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end < start:
            raise ValueError(f"no JSON array found in planner output: {text[:500]}")
        snippet = text[start : end + 1]
        parsed = json.loads(snippet)
        if not isinstance(parsed, list):
            raise ValueError("planner output was not a JSON array")
        return parsed

    def heuristic_plan(
        self,
        reported: list[VulnerabilityReport],
        audit: dict[str, Any],
    ) -> list[dict]:
        """Deterministic fallback used when the LLM is unavailable.

        Strategy:
          * Partition audit entries into "semver-compatible fix" (handled by
            a single `npm audit fix`) and "major-version fix" (each gets its
            own `package_update` because `npm audit fix` without `--force`
            will skip them).
          * Additionally, for any reported vuln with an explicit
            `fixed_version` and no overriding major-bump already planned,
            emit a `package_update` so scanner-known fixes aren't missed.
        """
        actions: list[dict] = []
        audit_vulns = audit.get("vulnerabilities") or {}

        semver_compat_ids: list[str] = []
        major_fixes: dict[str, tuple[str, str]] = {}  # pkg -> (version, addresses_id)
        for name, v in audit_vulns.items():
            fix = v.get("fixAvailable")
            if fix is True:
                semver_compat_ids.append(name)
            elif isinstance(fix, dict):
                if fix.get("isSemVerMajor"):
                    version = fix.get("version")
                    if isinstance(version, str) and version:
                        major_fixes[name] = (version, name)
                else:
                    semver_compat_ids.append(name)

        if semver_compat_ids:
            actions.append({
                "type": "npm_audit_fix",
                "reason": "npm audit reports SemVer-compatible fixes are available",
                "addresses": semver_compat_ids,
            })

        scanner_addresses: dict[str, list[str]] = {}
        for v in reported:
            scanner_addresses.setdefault(v.package, []).append(v.id)

        for pkg, (version, _) in major_fixes.items():
            actions.append({
                "type": "package_update",
                "package": pkg,
                "target_version": version,
                "reason": (
                    f"npm audit reports a major-version upgrade is required for "
                    f"{pkg}; `npm audit fix` will not apply it."
                ),
                "addresses": scanner_addresses.get(pkg, [pkg]),
            })

        already_planned_majors = set(major_fixes.keys())
        for v in reported:
            if v.fixed_version and v.package not in already_planned_majors:
                actions.append({
                    "type": "package_update",
                    "package": v.package,
                    "target_version": v.fixed_version,
                    "reason": f"Scanner-reported fixed version for {v.id}",
                    "addresses": [v.id],
                })
        return actions

    def validate_actions(self, raw_actions: list[dict]) -> tuple[list[FixAction], list[str]]:
        """Validate raw action dicts through FixAction, collecting errors
        for malformed entries and enforcing the targeted-update cap."""
        plan: list[FixAction] = []
        errs: list[str] = []
        for raw_action in raw_actions:
            try:
                action = FixAction.model_validate(raw_action)
            except Exception as e:
                errs.append(f"dropped invalid plan action {raw_action!r}: {e}")
                continue
            if action.type == "package_update" and (not action.package or not action.target_version):
                errs.append(f"dropped package_update missing package/version: {raw_action!r}")
                continue
            plan.append(action)

        targeted = [a for a in plan if a.type == "package_update"]
        if len(targeted) > settings.max_targeted_updates_per_run:
            errs.append(
                f"plan had {len(targeted)} package_update actions; capping at "
                f"{settings.max_targeted_updates_per_run}"
            )
            keep_targeted = set(
                id(a) for a in targeted[: settings.max_targeted_updates_per_run]
            )
            plan = [a for a in plan if a.type != "package_update" or id(a) in keep_targeted]
        return plan, errs

    async def plan(
        self,
        reported: list[VulnerabilityReport],
        audit: dict[str, Any] | None,
        audit_total: int,
    ) -> tuple[list[FixAction], list[str]]:
        """Produce a validated fix plan. Returns `(plan, errors_added)`.

        `audit_total` is the vuln count from a pre-computed audit summary;
        passed in so the caller can short-circuit when there's nothing to do
        without the agent needing to re-summarize.
        """
        audit = audit or {}
        logger.info("planner reported=%d audit_total=%d", len(reported), audit_total)

        if not reported and audit_total == 0:
            return [], []

        user_payload = self.build_user_payload(reported, audit)
        try:
            resp = await self._get_llm().ainvoke([
                SystemMessage(content=PLANNER_SYSTEM),
                HumanMessage(content=json.dumps(user_payload, indent=2)),
            ])
            raw = resp.content if isinstance(resp.content, str) else str(resp.content)
            raw_actions = self.extract_json_array(raw)
        except Exception as e:
            logger.warning("planner LLM call failed, falling back to heuristic plan: %s", e)
            raw_actions = self.heuristic_plan(reported, audit)

        return self.validate_actions(raw_actions)


planner_agent = PlannerAgent()
