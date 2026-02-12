import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple
import yaml  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]


try:
    from phase_tracker import (  # type: ignore[import-not-found]
        PhaseTracker,
        RfsnPhase,
    )
except ImportError:  # pragma: no cover
    from services.orchestrator.phase_tracker import (  # type: ignore[import-not-found]
        PhaseTracker,
        RfsnPhase,
    )


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

# Default step budgets when policy omits a type.
_DEFAULT_BUDGETS: Dict[str, Dict[str, Any]] = {
    "repo_search": {
        "max_per_iter": 4,
        "timeout_s": 30,
    },
    "repo_read_range": {
        "max_per_iter": 6,
        "max_lines_per_read": 300,
        "timeout_s": 15,
    },
    "apply_patch": {
        "max_per_iter": 2,
        "timeout_s": 60,
    },
    "ensure_deps": {
        "max_per_iter": 1,
        "timeout_s": 420,
    },
    "run_tests": {
        "max_per_iter": 4,
        "timeout_s": 900,
    },
}


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
    budget_usage: Dict[str, int] = field(
        default_factory=dict,
    )


def _is_safe_read_path(
    path: str,
    blocked_prefixes: List[str],
    blocked_suffixes: List[str],
) -> bool:
    """Deterministic path safety for repo_read_range.

    Blocks traversal, absolute paths, and
    sensitive prefixes/suffixes.
    """
    norm = os.path.normpath(path)
    # Absolute or traversal
    if norm.startswith("/") or norm.startswith("~"):
        return False
    if ".." in norm.split(os.sep):
        return False
    # Null bytes
    if "\x00" in path:
        return False
    low = path.lower()
    for pfx in blocked_prefixes:
        if low.startswith(pfx.lower()):
            return False
    for sfx in blocked_suffixes:
        if low.endswith(sfx.lower()):
            return False
    return True


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
            "budget_usage": decision.budget_usage,
        }

    # ── helpers ──────────────────────────────

    def _budget_for(
        self, step_type: str,
    ) -> Dict[str, Any]:
        cfg = self.policy.get(
            "step_budgets", {},
        )
        entry = cfg.get(step_type, {})
        defaults = _DEFAULT_BUDGETS.get(
            step_type, {},
        )
        merged: Dict[str, Any] = {}
        merged.update(defaults)
        merged.update(entry)
        return merged

    def _clamp_timeout(
        self, step: dict, budget: Dict[str, Any],
    ) -> None:
        """Clamp step timeout_s to policy max."""
        max_t = int(budget.get("timeout_s", 900))
        cur = step.get("timeout_s")
        if cur is None:
            step["timeout_s"] = max_t
        elif int(cur) > max_t:
            step["timeout_s"] = max_t

    # ── core gate ────────────────────────────

    def _validate(
        self, bundle: dict,
    ) -> GateDecision:
        errs: List[dict] = []

        # 1. Schema validation
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

        # 2. Total bundle size cap
        max_steps = int(
            self.policy.get(
                "max_steps_per_bundle", 15,
            ),
        )
        if len(steps) > max_steps:
            errs.append({
                "code": "BUNDLE_TOO_LARGE",
                "msg": (
                    f"{len(steps)} steps"
                    f" > {max_steps}"
                ),
                "path": ["steps"],
            })
            return GateDecision(
                ok=False, errors=errs,
            )

        # Policy knobs
        max_files = int(
            self.policy.get("max_patch_files", 3),
        )
        max_lines = int(
            self.policy.get(
                "max_patch_total_lines", 80,
            ),
        )
        max_added_lines = int(
            self.policy.get(
                "max_added_lines", 40,
            ),
        )
        max_deleted_lines = int(
            self.policy.get(
                "max_deleted_lines", 40,
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

        # Read-path blocklists
        blocked_read_pfx: List[str] = list(
            self.policy.get(
                "blocked_read_prefixes", [],
            ),
        )
        blocked_read_sfx: List[str] = list(
            self.policy.get(
                "blocked_read_suffixes", [],
            ),
        )

        risk = 0
        touched: Set[str] = set()
        type_counts: Counter = Counter()

        # 3. Per-step validation
        for i, s in enumerate(steps):
            t = s.get("type")
            if t not in allowed:
                errs.append({
                    "code": "STEP_TYPE_BLOCKED",
                    "msg": (
                        f"Step type blocked: {t}"
                    ),
                    "path": ["steps", i, "type"],
                })
                continue

            type_counts[t] += 1
            budget = self._budget_for(t)

            # ── budget: count per type ───────
            cap = int(
                budget.get("max_per_iter", 999),
            )
            if type_counts[t] > cap:
                errs.append({
                    "code": "BUDGET_EXCEEDED",
                    "msg": (
                        f"{t}: {type_counts[t]}"
                        f" > {cap}"
                    ),
                    "path": ["steps", i, "type"],
                })

            # ── clamp timeout_s to policy max ─
            self._clamp_timeout(s, budget)

            # ── repo_search ──────────────────
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

            # ── repo_read_range ──────────────
            if t == "repo_read_range":
                rpath = s.get("path", "")
                if not _is_safe_read_path(
                    rpath,
                    blocked_read_pfx,
                    blocked_read_sfx,
                ):
                    errs.append({
                        "code": (
                            "READ_PATH_BLOCKED"
                        ),
                        "msg": (
                            f"Blocked read: {rpath}"
                        ),
                        "path": [
                            "steps", i, "path",
                        ],
                    })
                # Line-range cap
                max_lpr = int(
                    budget.get(
                        "max_lines_per_read", 300,
                    ),
                )
                ls = int(
                    s.get("line_start", 1),
                )
                le = int(
                    s.get("line_end", ls),
                )
                span = le - ls + 1
                if span > max_lpr:
                    errs.append({
                        "code": (
                            "READ_RANGE_TOO_LARGE"
                        ),
                        "msg": (
                            f"{span} lines"
                            f" > {max_lpr}"
                        ),
                        "path": [
                            "steps",
                            i,
                            "line_end",
                        ],
                    })
                if span < 0:
                    errs.append({
                        "code": (
                            "READ_RANGE_INVALID"
                        ),
                        "msg": (
                            "line_end < line_start"
                        ),
                        "path": [
                            "steps",
                            i,
                            "line_end",
                        ],
                    })

            # ── apply_patch ──────────────────
            if t == "apply_patch":
                patch = s.get("patch", "") or ""
                plus_lines = [
                    line[1:]
                    for line in patch.splitlines()
                    if (
                        line.startswith("+")
                        and not line.startswith(
                            "+++",
                        )
                    )
                ]
                plus_blob = "\n".join(plus_lines)
                for pat in _BANNED_PATCH_PATTERNS:
                    if re.search(
                        pat,
                        plus_blob,
                        re.IGNORECASE,
                    ):
                        errs.append({
                            "code": (
                                "PATCH_CONTENT"
                                "_BLOCKED"
                            ),
                            "msg": (
                                "Banned pattern"
                                " in patch:"
                                f" {pat}"
                            ),
                            "path": [
                                "steps",
                                i,
                                "patch",
                            ],
                        })

                files = _extract_touched_files(
                    patch,
                )
                touched |= files
                add, rem = _count_diff_lines(
                    patch,
                )
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
                            f"{total}"
                            f" > {max_lines}"
                        ),
                        "path": [
                            "steps", i, "patch",
                        ],
                    })

                # Per-side line limits (unified
                # with diff_guard policy).
                if add > max_added_lines:
                    errs.append({
                        "code": (
                            "PATCH_TOO_MANY_ADDS"
                        ),
                        "msg": (
                            f"added {add}"
                            f" > {max_added_lines}"
                        ),
                        "path": [
                            "steps", i, "patch",
                        ],
                    })
                if rem > max_deleted_lines:
                    errs.append({
                        "code": (
                            "PATCH_TOO_MANY_DELS"
                        ),
                        "msg": (
                            f"deleted {rem}"
                            f" > {max_deleted_lines}"
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
                        for p in (
                            _FORBIDDEN_CI_PREFIXES
                        )
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

        budget_usage = dict(type_counts)

        if errs:
            return GateDecision(
                ok=False,
                errors=errs,
                risk_score=risk,
                touched_files=sorted(touched),
                budget_usage=budget_usage,
            )

        # 4. Test enforcement + ordering
        enforced: List[dict] = []
        has_patch = any(
            s.get("type") == "apply_patch"
            for s in steps
        )
        test_steps = [
            s for s in steps
            if s.get("type") == "run_tests"
        ]

        if has_patch and bool(
            self.policy.get(
                "enforce_tests", True,
            ),
        ):
            targeted = [
                s for s in test_steps
                if s.get("template_id")
                == "pytest_targeted"
            ]
            suite = [
                s for s in test_steps
                if s.get("template_id")
                == "pytest_suite"
            ]
            if not targeted:
                enforced.append({
                    "id": "gate-t1",
                    "type": "run_tests",
                    "template_id": (
                        "pytest_targeted"
                    ),
                    "template_params": {
                        "target": "tests",
                    },
                    "timeout_s": 240,
                })
            if not suite:
                enforced.append({
                    "id": "gate-t2",
                    "type": "run_tests",
                    "template_id": (
                        "pytest_suite"
                    ),
                    "template_params": {
                        "target": "",
                    },
                    "timeout_s": 900,
                })

        # 5. Risk threshold (λ-weighted scoring)
        # score = λ * risk - (1-λ) * expected_value
        # λ controls the balance: lower λ favours
        # patches with higher expected value even if
        # risk is moderate.
        reject_thr = int(
            self.policy.get(
                "reject_risk_score", 60,
            ),
        )
        # Expected value heuristic: minimal diffs
        # touching source (not tests/ci/deps) get
        # a bonus that offsets some risk.
        ev = 0
        src_files = [
            f for f in touched
            if not (
                f.startswith("tests/")
                or "/tests/" in f
            )
            and not any(
                f.startswith(p)
                for p in _FORBIDDEN_CI_PREFIXES
            )
            and f not in _DEP_MANIFEST_FILES
        ]
        if src_files:
            # Reward: fewer files + smaller diff
            # = higher expected value.
            total_changed = sum(
                _count_diff_lines(
                    s.get("patch", ""),
                )[0]
                + _count_diff_lines(
                    s.get("patch", ""),
                )[1]
                for s in steps
                if s.get("type") == "apply_patch"
            )
            if total_changed <= 20:
                ev = 30  # small, focused patch
            elif total_changed <= 50:
                ev = 15  # medium patch
            else:
                ev = 5   # large patch

        lam = float(
            self.policy.get("risk_lambda", 0.7),
        )
        effective_risk = (
            lam * risk - (1 - lam) * ev
        )
        if effective_risk >= reject_thr:
            return GateDecision(
                ok=False,
                errors=[{
                    "code": "RISK_REJECT",
                    "msg": (
                        f"risk={risk}"
                        f" ev={ev}"
                        f" effective="
                        f"{effective_risk:.0f}"
                        f" >= {reject_thr}"
                    ),
                }],
                risk_score=risk,
                touched_files=sorted(touched),
                budget_usage=budget_usage,
            )

        approved = steps + enforced

        # Enforce ordering: non-test steps first,
        # then targeted tests, then suite tests.
        if has_patch:
            non_test = [
                s for s in approved
                if s.get("type") != "run_tests"
            ]
            targeted_s = [
                s for s in approved
                if s.get("type") == "run_tests"
                and s.get("template_id")
                == "pytest_targeted"
            ]
            suite_s = [
                s for s in approved
                if s.get("type") == "run_tests"
                and s.get("template_id")
                != "pytest_targeted"
            ]
            approved = (
                non_test + targeted_s + suite_s
            )

        return GateDecision(
            ok=True,
            approved_steps=approved,
            risk_score=risk,
            enforced_steps=enforced,
            touched_files=sorted(touched),
            budget_usage=budget_usage,
        )
