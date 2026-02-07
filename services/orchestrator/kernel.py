import json
import re
import yaml  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

# Banned patterns in patch content (test skipping, test deletion, etc.)
_BANNED_PATCH_PATTERNS = [
    r"pytest\.skip",
    r"unittest\.skip",
    r"@skip",
    r"@pytest\.mark\.skip",
    r"xfail",
    r"remove\s+test",
    r"disable\s+test",
    r"__import__\s*\(",   # dynamic imports
    r"eval\s*\(",          # eval injection
    r"exec\s*\(",          # exec injection
    r"subprocess\.",       # subprocess in patches
    r"os\.system\(",       # os.system in patches
]


class Kernel:
    def __init__(self, schema_path: str, allowlist_path: str):
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                self.schema = json.load(f)
        except (FileNotFoundError, PermissionError) as exc:
            print(
                "FATAL: Cannot load schema"
                f" {schema_path}: {exc}",
                flush=True,
            )
            raise SystemExit(1) from exc
        self.validator = Draft202012Validator(self.schema)
        try:
            with open(allowlist_path, "r", encoding="utf-8") as f:
                self.allow = yaml.safe_load(f) or {}
        except (FileNotFoundError, PermissionError) as exc:
            print(
                "FATAL: Cannot load allowlist"
                f" {allowlist_path}: {exc}",
                flush=True,
            )
            raise SystemExit(1) from exc

    def validate_bundle(self, bundle: dict) -> list[dict]:
        errs = []
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
            return errs

        allowed = set(self.allow.get("allowed_step_types", []))
        for i, s in enumerate(bundle.get("steps", [])):
            t = s.get("type")
            if t not in allowed:
                errs.append({
                    "code": "STEP_TYPE_BLOCKED",
                    "msg": f"Step type blocked: {t}",
                    "path": ["steps", i, "type"],
                })

            # Deep content inspection: Kernel is FINAL AUTHORITY
            if t == "apply_patch":
                patch = s.get("patch", "")
                if patch:
                    # Check for banned patterns in patch content
                    plus_lines = [
                        line[1:] for line in patch.splitlines()
                        if line.startswith("+") and not line.startswith("+++")
                    ]
                    plus_blob = "\n".join(plus_lines)
                    for pat in _BANNED_PATCH_PATTERNS:
                        if re.search(pat, plus_blob, re.IGNORECASE):
                            errs.append({
                                "code": "PATCH_CONTENT_BLOCKED",
                                "msg": f"Banned pattern in patch: {pat}",
                                "path": ["steps", i, "patch"],
                            })

            if t == "repo_search":
                pattern = s.get("pattern", "")
                if len(pattern) > 500:
                    errs.append({
                        "code": "PATTERN_TOO_LONG",
                        "msg": "Search pattern exceeds 500 chars",
                        "path": ["steps", i, "pattern"],
                    })

        return errs

    def validate_and_plan(self, bundle: dict) -> dict:
        errs = self.validate_bundle(bundle)
        if errs:
            return {"ok": False, "errors": errs, "approved_steps": []}
        return {
            "ok": True,
            "errors": [],
            "approved_steps": bundle.get("steps", []),
        }
