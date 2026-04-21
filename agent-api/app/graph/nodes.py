"""LangGraph nodes.

Each node is an async function `(state) -> partial state`. Nodes are thin
delegators:
  * LLM-backed reasoning is owned by agent classes in `app/agents/`.
  * All filesystem / git / npm mutations go through the Tools API client.

The strict rule: nodes may call an agent OR the Tools API, never a shell.
Nodes never mutate the filesystem directly.
"""
from __future__ import annotations

from ..agents import planner_agent, summarizer_agent
from ..clients import ToolsApiClient, ToolsApiError
from ..core import get_logger, settings
from ..schemas.models import AppliedFix, FixAction
from ..utils import (
    build_report,
    commit_message,
    is_etarget_error,
    pick_fallback_candidates,
    pr_body,
    pr_head_branch,
    summarize_audit,
)
from .state import AgentState

logger = get_logger("agent.graph")


# ---------- node: create workspace ----------

async def create_workspace_node(state: AgentState) -> dict:
    logger.info(
        "node=create_workspace repo=%s branch=%s",
        state.get("repo_url"),
        state.get("branch"),
    )
    async with ToolsApiClient() as client:
        try:
            ws = await client.create_workspace(
                repo_url=state["repo_url"],
                branch=state.get("branch", "main"),
            )
            return {"workspace_id": ws["workspace_id"], "errors": state.get("errors", [])}
        except ToolsApiError as e:
            logger.exception(
                "create_workspace failed: repo=%s branch=%s status=%s body=%r",
                state.get("repo_url"), state.get("branch"), e.status_code, e.body,
            )
            errs = list(state.get("errors", []))
            errs.append(f"create_workspace failed: {e} :: body={e.body}")
            return {"workspace_id": None, "errors": errs}


# ---------- node: initial audit ----------

async def initial_audit_node(state: AgentState) -> dict:
    ws_id = state.get("workspace_id")
    if not ws_id:
        return {"initial_audit": None}
    logger.info("node=initial_audit workspace=%s", ws_id)
    async with ToolsApiClient() as client:
        try:
            audit = await client.npm_audit(ws_id)
            return {"initial_audit": audit}
        except ToolsApiError as e:
            logger.exception(
                "initial_audit failed: workspace=%s status=%s body=%r",
                ws_id, e.status_code, e.body,
            )
            errs = list(state.get("errors", []))
            errs.append(f"initial_audit failed: {e} :: body={e.body}")
            return {"initial_audit": None, "errors": errs}


# ---------- node: plan (LLM) ----------

async def plan_node(state: AgentState) -> dict:
    ws_id = state.get("workspace_id")
    if not ws_id:
        return {"fix_plan": []}

    reported = state.get("reported_vulnerabilities", []) or []
    audit = state.get("initial_audit") or {}
    audit_total = summarize_audit(audit).get("total", 0)

    plan, new_errs = await planner_agent.plan(reported, audit, audit_total)

    errs = list(state.get("errors", []))
    errs.extend(new_errs)
    return {"fix_plan": plan, "errors": errs}


# ---------- node: execute plan ----------

def _current_version_for(state: AgentState, package: str) -> str | None:
    """Look up the scanner-reported installed version for `package`."""
    for v in state.get("reported_vulnerabilities", []) or []:
        if v.package == package and v.current_version:
            return v.current_version
    return None


def _package_update_details(package: str, version: str, result: dict) -> str:
    return (
        f"npm install {package}@{version} "
        f"exit={result.get('exit_code')} "
        f"installed={result.get('installed_version')} "
        f"stderr={(result.get('stderr') or '')[:500]}"
    )


async def _try_package_update_with_fallback(
    client: ToolsApiClient,
    ws_id: str,
    action: FixAction,
    current_version: str | None,
) -> AppliedFix:
    """Install `action.package@action.target_version`, retrying with
    lowest-impact real versions if npm reports ETARGET.

    On success, the returned AppliedFix reflects the version that was
    actually installed — which may differ from the planner's target when
    a fallback won. On total failure, `recommendation` points the reviewer
    at a concrete next version to try manually (or at the registry).
    """
    assert action.package and action.target_version
    pkg = action.package
    primary_target = action.target_version

    result = await client.update_package(
        workspace_id=ws_id, package=pkg, version=primary_target,
    )
    details = _package_update_details(pkg, primary_target, result)
    if result.get("exit_code") == 0:
        return AppliedFix(action=action, success=True, details=details)

    stderr = result.get("stderr") or ""
    if not is_etarget_error(stderr):
        # A different kind of failure (peer-dep conflict, auth, network) —
        # fallback won't help; surface the raw stderr as the recommendation
        # hint and let the reviewer triage.
        return AppliedFix(
            action=action,
            success=False,
            details=details,
            recommendation=(
                f"`npm install {pkg}@{primary_target}` failed for a reason other "
                f"than a missing version. Review the stderr above for the root "
                f"cause (common: peer-dep conflict, auth, or registry outage)."
            ),
        )

    logger.info(
        "execute_plan: %s@%s not published; attempting fallback versions",
        pkg, primary_target,
    )
    try:
        available = await client.list_package_versions(ws_id, pkg)
    except ToolsApiError as e:
        logger.warning("list_package_versions failed for %s: %s", pkg, e)
        return AppliedFix(
            action=action,
            success=False,
            details=details,
            recommendation=(
                f"Requested version `{primary_target}` is not published. "
                f"Check https://www.npmjs.com/package/{pkg}?activeTab=versions "
                f"for available releases."
            ),
        )

    candidates = pick_fallback_candidates(
        available, current=current_version, failed_target=primary_target,
    )
    if not candidates:
        return AppliedFix(
            action=action,
            success=False,
            details=details,
            recommendation=(
                f"Requested version `{primary_target}` is not published and no "
                f"newer stable release was found on the registry. Review "
                f"https://www.npmjs.com/package/{pkg}?activeTab=versions manually."
            ),
        )

    last_details = details
    for candidate in candidates:
        logger.info("execute_plan: retrying %s@%s", pkg, candidate)
        retry = await client.update_package(
            workspace_id=ws_id, package=pkg, version=candidate,
        )
        last_details = _package_update_details(pkg, candidate, retry)
        if retry.get("exit_code") == 0:
            # Record the version we actually shipped so the PR body doesn't
            # claim we installed the (nonexistent) planner target.
            effective_action = FixAction(
                type=action.type,
                package=action.package,
                target_version=candidate,
                reason=(
                    f"{action.reason} (fell back from {primary_target}; "
                    f"planner target not published)"
                ).strip(),
                addresses=action.addresses,
            )
            return AppliedFix(
                action=effective_action,
                success=True,
                details=last_details,
            )

    return AppliedFix(
        action=action,
        success=False,
        details=last_details,
        recommendation=(
            f"`{primary_target}` is not published and fallback attempts "
            f"({', '.join(candidates)}) also failed. Try `npm install "
            f"{pkg}@{candidates[0]}` manually and review the stderr."
        ),
    )


async def execute_plan_node(state: AgentState) -> dict:
    ws_id = state.get("workspace_id")
    plan = state.get("fix_plan", []) or []
    applied: list[AppliedFix] = []
    errs = list(state.get("errors", []))

    if not ws_id or not plan:
        return {"applied_fixes": applied}

    logger.info("node=execute_plan actions=%d", len(plan))
    async with ToolsApiClient() as client:
        for action in plan:
            try:
                if action.type == "npm_audit_fix":
                    result = await client.npm_audit_fix(ws_id)
                    success = (result.get("exit_code") == 0)
                    applied.append(AppliedFix(
                        action=action,
                        success=success,
                        details=f"npm audit fix exit={result.get('exit_code')} "
                                f"stderr={(result.get('stderr') or '')[:500]}",
                        recommendation=(
                            "" if success else
                            "Run `npm audit` locally to see the unresolved advisories "
                            "and consider `npm audit fix --force` only after reviewing "
                            "the breaking-change notes."
                        ),
                    ))
                elif action.type == "package_update":
                    assert action.package and action.target_version
                    current_version = _current_version_for(state, action.package)
                    applied.append(await _try_package_update_with_fallback(
                        client, ws_id, action, current_version,
                    ))
                else:
                    errs.append(f"unknown action type: {action.type}")
            except ToolsApiError as e:
                logger.exception(
                    "execute_plan action failed: workspace=%s action=%s status=%s body=%r",
                    ws_id, getattr(action, "type", None), e.status_code, e.body,
                )
                applied.append(AppliedFix(
                    action=action,
                    success=False,
                    details=f"tools api error: {e} :: body={e.body}",
                    recommendation=(
                        "The Tools API returned an error before npm could run. "
                        "Check the tools-api service logs; this is typically a "
                        "workspace or configuration issue rather than a package one."
                    ),
                ))

    return {"applied_fixes": applied, "errors": errs}


# ---------- node: resync manifest ----------

async def resync_manifest_node(state: AgentState) -> dict:
    """Ensure package.json reflects every remediated direct dependency.

    `npm audit fix` often updates only package-lock.json when the new version
    already satisfies the existing range in package.json. That hides the fix
    from code review. We ask the Tools API to re-save every direct dep that
    was vulnerable at the start, pinning its declared range to the installed
    version. Transitive-only deps are skipped by the service layer.
    """
    ws_id = state.get("workspace_id")
    applied = state.get("applied_fixes", []) or []
    if not ws_id or not any(a.success for a in applied):
        return {}

    initial = state.get("initial_audit") or {}
    initial_pkgs = sorted((initial.get("vulnerabilities") or {}).keys())
    if not initial_pkgs:
        return {}

    logger.info("node=resync_manifest packages=%d", len(initial_pkgs))
    async with ToolsApiClient() as client:
        try:
            outcome = await client.resync_manifest(ws_id, packages=initial_pkgs)
            logger.info(
                "resync_manifest rewrote=%s overrides_added=%s",
                outcome.get("rewritten"), outcome.get("overrides_added"),
            )
            return {}
        except ToolsApiError as e:
            # Non-fatal: the remediation already succeeded via lockfile. The
            # manifest just won't reflect it — reviewer sees a lock-only diff.
            logger.warning(
                "resync_manifest failed (non-fatal): workspace=%s status=%s body=%r",
                ws_id, e.status_code, e.body,
            )
            errs = list(state.get("errors", []))
            errs.append(f"resync_manifest failed: {e} :: body={e.body}")
            return {"errors": errs}


# ---------- node: final audit ----------

async def final_audit_node(state: AgentState) -> dict:
    ws_id = state.get("workspace_id")
    if not ws_id:
        return {"final_audit": None}
    logger.info("node=final_audit workspace=%s", ws_id)
    async with ToolsApiClient() as client:
        try:
            return {"final_audit": await client.npm_audit(ws_id)}
        except ToolsApiError as e:
            logger.exception(
                "final_audit failed: workspace=%s status=%s body=%r",
                ws_id, e.status_code, e.body,
            )
            errs = list(state.get("errors", []))
            errs.append(f"final_audit failed: {e} :: body={e.body}")
            return {"final_audit": None, "errors": errs}


# ---------- node: commit ----------

async def commit_node(state: AgentState) -> dict:
    ws_id = state.get("workspace_id")
    applied = state.get("applied_fixes", []) or []
    if not ws_id or not any(a.success for a in applied):
        return {"commit_sha": None}

    report = build_report(
        initial_audit=state.get("initial_audit"),
        final_audit=state.get("final_audit"),
        applied_fixes=applied,
        reported=state.get("reported_vulnerabilities", []) or [],
    )
    message = commit_message(report)

    async with ToolsApiClient() as client:
        try:
            result = await client.commit(
                workspace_id=ws_id,
                message=message,
                author_name=settings.commit_author_name,
                author_email=settings.commit_author_email,
            )
            files = result.get("files_changed") or []
            return {
                "commit_sha": result.get("commit_sha"),
                "committed_files": [f for f in files if isinstance(f, str)],
            }
        except ToolsApiError as e:
            errs = list(state.get("errors", []))
            # A 400 here typically means "no changes to commit" — not fatal.
            if e.status_code == 400:
                logger.info(
                    "commit: no changes to commit for workspace=%s (body=%r)",
                    ws_id, e.body,
                )
                return {"commit_sha": None, "committed_files": [], "errors": errs}
            logger.exception(
                "commit failed: workspace=%s status=%s body=%r",
                ws_id, e.status_code, e.body,
            )
            errs.append(f"commit failed: {e} :: body={e.body}")
            return {"commit_sha": None, "committed_files": [], "errors": errs}


# ---------- node: open pull request ----------

async def open_pr_node(state: AgentState) -> dict:
    ws_id = state.get("workspace_id")
    commit_sha = state.get("commit_sha")
    if not ws_id or not commit_sha:
        return {"pr_url": None, "pr_number": None, "pr_branch": None}

    base_branch = state.get("branch") or "main"
    head_branch = pr_head_branch(ws_id)
    applied = state.get("applied_fixes", []) or []
    report = build_report(
        initial_audit=state.get("initial_audit"),
        final_audit=state.get("final_audit"),
        applied_fixes=applied,
        reported=state.get("reported_vulnerabilities", []) or [],
    )
    resolved_n = len(report.resolved_packages)
    if resolved_n:
        title = f"VulnFix: resolve {resolved_n} vulnerable package(s)"
    else:
        title = "VulnFix: automated vulnerability remediation"

    committed_files = state.get("committed_files") or []

    logger.info("node=open_pr workspace=%s head=%s base=%s", ws_id, head_branch, base_branch)
    async with ToolsApiClient() as client:
        try:
            result = await client.open_pull_request(
                workspace_id=ws_id,
                base_branch=base_branch,
                head_branch=head_branch,
                title=title,
                body=pr_body(report, committed_files=committed_files),
            )
            return {
                "pr_url": result.get("url"),
                "pr_number": result.get("number"),
                "pr_branch": head_branch,
            }
        except ToolsApiError as e:
            logger.exception(
                "open_pull_request failed: workspace=%s head=%s base=%s status=%s body=%r",
                ws_id, head_branch, base_branch, e.status_code, e.body,
            )
            errs = list(state.get("errors", []))
            errs.append(f"open_pull_request failed: {e} :: body={e.body}")
            return {"pr_url": None, "pr_number": None, "pr_branch": head_branch, "errors": errs}


# ---------- node: summarize (LLM) ----------

async def summarize_node(state: AgentState) -> dict:
    payload = {
        "repo_url": state.get("repo_url"),
        "branch": state.get("branch"),
        "initial_audit_summary": summarize_audit(state.get("initial_audit")),
        "final_audit_summary": summarize_audit(state.get("final_audit")),
        "fix_plan": [a.model_dump() for a in (state.get("fix_plan") or [])],
        "applied_fixes": [a.model_dump() for a in (state.get("applied_fixes") or [])],
        "commit_sha": state.get("commit_sha"),
        "pull_request": {
            "url": state.get("pr_url"),
            "number": state.get("pr_number"),
            "branch": state.get("pr_branch"),
        },
        "errors": state.get("errors", []),
    }
    summary = await summarizer_agent.summarize(payload)
    return {"summary": summary}
