"""Classifier agent — decides whether each reported vulnerability lives in
the code (npm) or in a container image.

The remediation pipeline can only auto-fix npm code vulns. Image vulns
(OS packages from a base image, e.g. `openssl` in `python:3.11-slim`) need
an image rebuild that's outside this PoC's scope — we still want to surface
them in the PR as an informational section. A small "unclassified" bucket
catches anything the heuristics and LLM can't confidently place; those
rows also land in the PR so reviewers know manual triage is required.

Design follows the planner: fast deterministic rules first, LLM fallback
for genuinely ambiguous rows. The LLM is optional — if it fails or is
unavailable, confidently-ambiguous rows end up ``unclassified`` rather
than being forced into one bucket.
"""
from __future__ import annotations

import json

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ..core import build_chat_llm, get_logger
from ..schemas.models import VulnerabilityKind, VulnerabilityReport

logger = get_logger("agent.classifier")


# Manifest-path signals. These are the strongest classifier inputs; if the
# scanner told us a `package-lock.json` flagged the row, it's code, full stop.
_CODE_MANIFEST_SUFFIXES = (
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
)

_IMAGE_MANIFEST_HINTS = (
    "dockerfile",
    "containerfile",
    ".tar",
    "image-manifest",
    "/layers/",
    "rootfs",
)

# Well-known OS / base-image package names that are never published on the
# npm registry. Matching by exact package name (lowercased) — fuzzy matching
# is left to the LLM fallback.
_OS_PACKAGE_NAMES = frozenset({
    "openssl", "libssl", "libssl1.1", "libssl3",
    "glibc", "libc6", "libc-bin", "musl",
    "zlib", "zlib1g", "libz",
    "libxml2", "libxslt", "libxml2-utils",
    "libcurl", "curl",
    "bash", "busybox", "coreutils", "util-linux",
    "apt", "dpkg", "rpm", "yum", "apk",
    "perl", "perl-base", "python", "python3", "python2",
    "ruby", "go", "golang",
    "gcc", "libgcc", "libstdc++",
    "krb5", "libkrb5", "gssapi",
    "pam", "libpam", "libpam-modules",
    "sudo", "shadow", "passwd",
    "openssh", "openssh-client", "openssh-server",
    "nginx", "apache2", "httpd",
    "tzdata", "ca-certificates",
    "ncurses", "libncurses",
    "pcre", "pcre2", "libpcre",
    "libgcrypt", "libgpg-error",
    "freetype", "libfreetype",
    "e2fsprogs", "libext2fs",
    "systemd", "libsystemd",
    "dbus", "libdbus",
    "expat", "libexpat",
})


CLASSIFIER_SYSTEM = """You are the classification component of an automated vulnerability triage system.

Each input is a vulnerability reported against one package. Decide whether
the vulnerability is:
  - "code": a dependency of an application's source tree that can be fixed by
    running `npm install <pkg>@<version>` (or equivalent JS package-manager
    command). Examples: `lodash`, `handlebars`, `@scope/foo`, `protobufjs`.
  - "image": an operating-system or base-image package, reachable only by
    rebuilding the container image. Examples: `openssl`, `glibc`, `zlib`,
    `libxml2`, `bash`, `curl`. These have no JS registry equivalent.
  - "unclassified": you cannot decide with reasonable confidence.

Signals you should use, in order of strength:
  1. `manifest_path`: `package.json` / `*lock*` / `yarn.lock` → code.
     `Dockerfile` / `Containerfile` / image-layer paths → image.
  2. `package` name: npm-style (lowercase, hyphen-separated, optionally
     `@scope/name`) used in the npm ecosystem → code; well-known OS
     libraries → image.
  3. `description`: mentions of Docker, container, base image, Alpine,
     Debian, `apt install`, `apk add` → image. Mentions of npm, yarn,
     JavaScript, Node.js → code.

Output ONLY a JSON array. Each element must be `{"id": "<vuln-id>", "kind": "code"|"image"|"unclassified"}`.
No prose, no markdown fences. Preserve the input order. Return an element
for every input id.
"""


class ClassifierAgent:
    """Annotates each VulnerabilityReport with a `kind`.

    Heuristic rules handle the easy cases deterministically (and without
    paying for an LLM call). Anything the rules can't decide goes to the
    LLM; anything the LLM also can't decide stays ``unclassified``.
    """

    def __init__(self) -> None:
        self._llm: BaseChatModel | None = None

    def _get_llm(self) -> BaseChatModel:
        if self._llm is None:
            self._llm = build_chat_llm()
        return self._llm

    # ---------- rule-based ----------

    @staticmethod
    def _rule_classify(report: VulnerabilityReport) -> VulnerabilityKind | None:
        """Return a kind iff rules can decide confidently, else None."""
        path = (report.manifest_path or "").lower().strip()
        if path:
            if any(path.endswith(s) for s in _CODE_MANIFEST_SUFFIXES):
                return "code"
            if any(h in path for h in _IMAGE_MANIFEST_HINTS):
                return "image"

        pkg = (report.package or "").lower().strip()
        if pkg in _OS_PACKAGE_NAMES:
            return "image"
        # Scoped npm packages (`@foo/bar`) are unambiguously code.
        if pkg.startswith("@") and "/" in pkg:
            return "code"

        return None

    # ---------- LLM fallback ----------

    def _build_llm_payload(self, reports: list[VulnerabilityReport]) -> list[dict]:
        return [
            {
                "id": r.id,
                "package": r.package,
                "manifest_path": r.manifest_path,
                # Truncate description — we already cap in the CSV parser, but
                # re-truncate defensively in case callers bypass the CSV path.
                "description": (r.description or "")[:400],
            }
            for r in reports
        ]

    @staticmethod
    def _extract_json_array(text: str) -> list[dict]:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end < start:
            raise ValueError(f"no JSON array found in classifier output: {text[:500]}")
        parsed = json.loads(text[start : end + 1])
        if not isinstance(parsed, list):
            raise ValueError("classifier output was not a JSON array")
        return parsed

    async def _llm_classify(
        self, reports: list[VulnerabilityReport]
    ) -> dict[str, VulnerabilityKind]:
        """Return `{id: kind}`. Missing/invalid ids are simply absent — the
        caller treats absence as ``unclassified``."""
        payload = self._build_llm_payload(reports)
        try:
            resp = await self._get_llm().ainvoke([
                SystemMessage(content=CLASSIFIER_SYSTEM),
                HumanMessage(content=json.dumps(payload, indent=2)),
            ])
            raw = resp.content if isinstance(resp.content, str) else str(resp.content)
            parsed = self._extract_json_array(raw)
        except Exception as e:
            logger.warning("classifier LLM call failed: %s", e)
            return {}

        out: dict[str, VulnerabilityKind] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            vid = item.get("id")
            kind = item.get("kind")
            if isinstance(vid, str) and kind in ("code", "image", "unclassified"):
                out[vid] = kind  # type: ignore[assignment]
        return out

    # ---------- public entrypoint ----------

    async def classify(
        self, reports: list[VulnerabilityReport]
    ) -> list[VulnerabilityReport]:
        """Return a new list of reports, each with `kind` populated.

        Rules handle the confident cases; the LLM handles the rest; anything
        the LLM couldn't confidently place is marked ``unclassified``.
        """
        if not reports:
            return reports

        # Preserve input order by working on index pairs.
        rule_decisions: list[VulnerabilityKind | None] = [
            self._rule_classify(r) for r in reports
        ]
        needs_llm_idx = [i for i, k in enumerate(rule_decisions) if k is None]

        llm_decisions: dict[str, VulnerabilityKind] = {}
        if needs_llm_idx:
            llm_decisions = await self._llm_classify(
                [reports[i] for i in needs_llm_idx]
            )

        out: list[VulnerabilityReport] = []
        for i, original in enumerate(reports):
            kind = rule_decisions[i]
            if kind is None:
                kind = llm_decisions.get(original.id, "unclassified")
            out.append(original.model_copy(update={"kind": kind}))

        code_n = sum(1 for r in out if r.kind == "code")
        image_n = sum(1 for r in out if r.kind == "image")
        unclass_n = sum(1 for r in out if r.kind == "unclassified")
        logger.info(
            "classifier: total=%d code=%d image=%d unclassified=%d (rules=%d llm=%d)",
            len(out), code_n, image_n, unclass_n,
            len(out) - len(needs_llm_idx), len(needs_llm_idx),
        )
        return out


classifier_agent = ClassifierAgent()
