import json
import re
from dataclasses import dataclass, field
from typing import List, Set, Tuple
import yaml  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]


_BANNED_PATCH_PATTERNS = [
    r"pytest\.skip",
    r"unittest\.skip",
    r"@skip",
    r"@pytest\.mark\.skip",
    r"xfail",
    r"remove\s+test",
    r"disable\s+test",
    r"__import__\s*\(",
    r"eval\s*\(",
    r"exec\s*\(",
    r"subprocess\.",
    r"os\.system\(",
]

_FORBIDDEN_CI_PREFIXES = (
    ".github/workflows/",
    "ci/",
    "scripts/",
)

_DEP_MANIFEST_FILES = (
    "requirements.txt",
    "requirements.in",
    "constraints.txt",
    "pyproject.toml",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
)


def _extract_touched_files(
    unified_diff: str,
) -> Set[str]:
    files: Set[str] = set()
    for line in unified_diff.splitlines():
        if line.startswith("+++ b/"):
            files.add(
                line[len("+++ b/"):].strip(),
            )
    return files


def _count_diff_lines(
    unified_diff: str,
) -> Tuple[int, int]:
    add = rem = 0
    for ln in unified_diff.splitlines():
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if ln.startswith("+"):
            add += 1
        elif ln.startswith("-"):
            rem += 1
    return add, rem


@dataclass
class GateDecision:
    ok: bool
    errors: List[dict] = field(default_factory=list)
    approved_steps: List[dict] = field(
        default_factory=list,
    )
    risk_score: int = 0
    enforced_steps: List[dict] = field(
        default_factory=list,
    )
    touched_files: List[str] = field(
        default_factory=list,
    )


class Kernel:
    """Deterministic kernel gate (final authority).

    Validates schema + allowlist, enforces
    patch/path budgets and content bans, computes
    risk_score deterministically, and can
    enforce/inject required test steps.
    """

    def __init__(
        self,
        schema_path: str,
        allowlist_path: str,
        policy_path: str = "/policies/gate_policy.yaml",
    ) -> None:
        try:
            with open(
                schema_path, "r", encoding="utf-8",
            ) as f:
                self.schema = json.load(f)
        except (
            FileNotFoundError, PermissionError,
        ) as exc:
            print(
                "FATAL: Cannot load schema"
                f" {schema_path}: {exc}",
                flush=True,
            )
            raise SystemExit(1) from exc
        self.validator = Draft202012Validator(
            self.schema,
        )

        try:
            with open(
                allowlist_path, "r", encoding="utf-8",
            ) as f:
                self.allow = yaml.safe_load(f) or {}
        except (
            FileNotFoundError, PermissionError,
        ) as exc:
            print(
                "FATAL: Cannot load allowlist"
                f" {allowlist_path}: {exc}",
                flush=True,
            )
            raise SystemExit(1) from exc

        try:
            with open(
                policy_path, "r", encoding="utf-8",
            ) as f:
                self.policy = yaml.safe_load(f) or {}
        except (
            FileNotFoundError, PermissionError,
        ) as exc:
            print(
                "FATAL: Cannot load gate policy"
                f" {policy_path}: {exc}",
                flush=True,
            )
            raise SystemExit(1) from exc

    def validate_and_plan(
        self, bundle: dict,
    ) -> dict:
        decision = self._validate(bundle)
        return {
            "ok": decision.ok,
            "errors": decision.errors,
            "approved_steps": decision.approved_steps,
            "risk_score": decision.risk_score,
            "enforced_steps": decision.enforced_steps,
            "touched_files": decision.touched_files,
        }

    def _validate(
        self, bundle: dict,
    ) -> GateDecision:
        errs: List[dict] = []
        for e in sorted(
            self.validator.iter_errors(bundle),
            key=lambda x: x.path,
        ):
            errs.append({
                "code": "SCHEMA_FAIL",
                "msg": e.message,
                "path": list(e.path),
            })
        if errs:
            return GateDecision(
                ok=False, errors=errs,
            )

        allowed = set(
            self.allow.get(
                "allowed_step_types", [],
            ),
        )
        steps = list(bundle.get("steps", []))

        max_files = int(
            self.policy.get("max_patch_files", 6),
        )
        max_lines = int(
            self.policy.get(
                "max_patch_total_lines", 300,
            ),
        )
        forbid_tests = bool(
            self.policy.get(
                "forbid_test_edits", True,
            ),
        )
        forbid_ci = bool(
            self.policy.get(
                "forbid_ci_edits", True,
            ),
        )
        forbid_deps = bool(
            self.policy.get(
                "forbid_dep_manifest_edits", True,
            ),
        )

        risk = 0
        touched: Set[str] = set()

        for i, s in enumerate(steps):
            t = s.get("type")
            if t not in allowed:
                errs.append({
                    "code": "STEP_TYPE_BLOCKED",
                    "msg": f"Step type blocked: {t}",
                    "path": ["steps", i, "type"],
                })
                continue

            if t == "repo_search":
                pattern = s.get("pattern", "")
                if len(pattern) > 500:
                    errs.append({
                        "code": "PATTERN_TOO_LONG",
                        "msg": (
                            "Search pattern"
                            " exceeds 500 chars"
                        ),
                        "path": [
                            "steps", i, "pattern",
                        ],
                    })

            if t == "apply_patch":
                patch = s.get("patch", "") or ""
                plus_lines = [
                    line[1:]
                    for line in patch.splitlines()
                    if (
                        line.startswith("+")
                        and not line.startswith("+++")
                    )
                ]
                plus_blob = "\n".join(plus_lines)
                for pat in _BANNED_PATCH_PATTERNS:
                    if re.search(
                        pat, plus_blob, re.IGNORECASE,
                    ):
                        errs.append({
                            "code": (
                                "PATCH_CONTENT_BLOCKED"
                            ),
                            "msg": (
                                "Banned pattern in"
                                f" patch: {pat}"
                            ),
                            "path": [
                                "steps", i, "patch",
                            ],
                        })

                files = _extract_touched_files(patch)
                touched |= files
                add, rem = _count_diff_lines(patch)
                total = add + rem

                if len(files) > max_files:
                    errs.append({
                        "code": (
                            "PATCH_TOO_MANY_FILES"
                        ),
                        "msg": (
                            f"{len(files)}"
                            f" > {max_files}"
                        ),
                        "path": [
                            "steps", i, "patch",
                        ],
                    })
                if total > max_lines:
                    errs.append({
                        "code": "PATCH_TOO_LARGE",
                        "msg": (
                            f"{total} > {max_lines}"
                        ),
                        "path": [
                            "steps", i, "patch",
                        ],
                    })

                if total > 120:
                    risk += 10

                for f in files:
                    is_test = (
                        f.startswith("tests/")
                        or "/tests/" in f
                    )
                    is_ci = any(
                        f.startswith(p)
                        for p in _FORBIDDEN_CI_PREFIXES
                    )
                    is_dep = (
                        f in _DEP_MANIFEST_FILES
                        or f.endswith(
                            _DEP_MANIFEST_FILES,
                        )
                    )

                    if is_ci:
                        risk += 30
                        if forbid_ci:
                            errs.append({
                                "code": (
                                    "PATCH_FORBIDDEN"
                                    "_CI_PATH"
                                ),
                                "msg": f,
                                "path": [
                                    "steps",
                                    i,
                                    "patch",
                                ],
                            })
                    if is_dep:
                        risk += 20
                        if forbid_deps:
                            errs.append({
                                "code": (
                                    "PATCH_FORBIDDEN"
                                    "_DEP_MANIFEST"
                                ),
                                "msg": f,
                                "path": [
                                    "steps",
                                    i,
                                    "patch",
                                ],
                            })
                    if is_test:
                        risk += 25
                        if forbid_tests:
                            errs.append({
                                "code": (
                                    "PATCH_FORBIDDEN"
                                    "_TEST_EDIT"
                                ),
                                "msg": f,
                                "path": [
                                    "steps",
                                    i,
                                    "patch",
                                ],
                            })

        if errs:
            return GateDecision(
                ok=False,
                errors=errs,
                risk_score=risk,
                touched_files=sorted(touched),
            )

        enforced: List[dict] = []
        has_patch = any(
            s.get("type") == "apply_patch"
            for s in steps
        )
        has_tests = any(
            s.get("type") == "run_tests"
            for s in steps
        )

        if (
            has_patch
            and bool(
                self.policy.get(
                    "enforce_tests", True,
                ),
            )
            and not has_tests
        ):
            enforced.append({
                "id": "gate-t1",
                "type": "run_tests",
                "template_id": "pytest_targeted",
                "template_params": {
                    "target": "tests",
                },
                "timeout_s": 240,
            })
            enforced.append({
                "id": "gate-t2",
                "type": "run_tests",
                "template_id": "pytest_suite",
                "template_params": {"target": ""},
                "timeout_s": 900,
            })

        reject_thr = int(
            self.policy.get(
                "reject_risk_score", 60,
            ),
        )
        if risk >= reject_thr:
            return GateDecision(
                ok=False,
                errors=[{
                    "code": "RISK_REJECT",
                    "msg": (
                        f"risk={risk}"
                        f" >= {reject_thr}"
                    ),
                }],
                risk_score=risk,
                touched_files=sorted(touched),
            )

        approved = steps + enforced
        return GateDecision(
            ok=True,
            approved_steps=approved,
            risk_score=risk,
            enforced_steps=enforced,
            touched_files=sorted(touched),
        )
