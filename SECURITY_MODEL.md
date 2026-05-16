# SECURITY_MODEL.md

## RFSN Agent — Security Model

> **Status: Prototype / Repair Stage.  Not safe for untrusted repositories
> unless a secure Docker sandbox is configured and tested.**

---

## Trusted / Untrusted Boundaries

| Context | Trust level | Safe to use? |
|:---|:---|:---|
| Local dev mode, your own repo | Trusted | ✅ Yes, with awareness |
| Local dev mode, third-party repo | Untrusted | ❌ No — use Docker mode |
| Docker sandbox, no network | Semi-trusted | ⚠️ Only if daemon is configured |
| Docker sandbox, network enabled | Untrusted | ❌ Requires explicit network tier |

---

## Active Sandbox Mode

Default: **`local_dev`**

Set via environment variable:

```
RFSN_SANDBOX_MODE=local_dev
RFSN_ALLOW_LOCAL_EXEC=1
RFSN_DEV_MODE=1
RFSN_EXEC_USE_DOCKER=0
```

**Local dev mode warnings:**
- No filesystem isolation.
- No network isolation.
- No process isolation.
- Only for trusted repos you control.
- Do not use against untrusted code.

**Docker mode** (`RFSN_EXEC_USE_DOCKER=1`):
- Requires a running Docker daemon.
- Executor verifies daemon availability at startup.
- If unavailable, fails clearly (no silent downgrade).
- Containers are: no-network, non-root, no-new-privileges, dropped capabilities,
  memory/CPU/PID limited, controlled workspace, timeout enforced.

---

## Command Execution Policy

**Prohibited:**
- `shell=True` in any runtime path.
- `os.system()`.
- `subprocess.run(cmd_as_string)` with agent-controlled content.
- Backtick expansion, `eval`, `exec`.
- Network commands.
- Package install commands (not allowed by default).

**Allowed:**
- `subprocess.run(argv_list, shell=False)` with structured arg lists.
- Allowlisted command templates only (see `ALLOWED_COMMAND_TEMPLATES` in
  `autofix/apply.py` and `policies/tool_allowlist.yaml`).
- Paths validated to remain inside workspace root.
- Shell metacharacters (`;`, `|`, `&`, `>`, `<`, `$()`, backticks, newlines)
  rejected in all path arguments.
- Hard timeout required for all commands.
- Output capture size-limited.

---

## Patch Policy

- All patch tools (`apply_patch`, `apply_semantic_patch`) go through the
  patch risk gate (`rfsn_swebench/gate.py`).
- Protected file classes:
  - Test files (`tests/`, `*_test.py`, etc.)
  - CI configuration (`.github/workflows/`, `ci/`, `scripts/`)
  - Dependency manifests (`pyproject.toml`, `requirements.txt`, etc.)
- Banned patch content patterns: `pytest.skip`, `eval(`, `exec(`,
  `subprocess.`, `os.system(`, etc.
- No-op patches are rejected — a patch that changes nothing must not report
  success.
- Patch size limits enforced via `gate_policy.yaml`.

---

## Tool Registry Policy

- Every tool is declared in `rfsn_kernel/tool_registry.py`.
- Disabled tools (`enabled=False`) fail closed at:
  1. `validate.py` — rejected with `TOOL_DISABLED` error.
  2. `dispatcher.py` — rejected before handler is called.
  3. Executor — handler replaced with a clear error raise.
- Unknown tools (not in registry) fail closed with `UNKNOWN_ACTION` error.
- YAML allowlist (`policies/tool_allowlist.yaml`) must not list tools that
  are disabled in the registry.

---

## Disabled Dangerous Tools

| Tool | Reason | Re-enable condition |
|:---|:---|:---|
| `trace_execution` | Used `os.system()` with agent-controlled strings | Complete safe rewrite + all tests pass |
| `apply_semantic_patch` | Inconsistent API; no no-op detection | All `test_semantic_patch_safety.py` tests pass |

---

## Known Risks

1. **Local dev mode is unsafe for untrusted code** — no isolation.
2. **No LLM planner** — orchestrator is in dry-run mode; autonomous repair
   is not functional.
3. **Docker socket** — if mounting host Docker socket, the container has
   host-level Docker access.  Document this threat model before enabling.
4. **Replay log** — contains step inputs/outputs; may contain sensitive data
   from the repo being repaired.  Protect accordingly.
5. **`scripts/verify_hardening.py`** — still uses `shell=True` for
   verification commands; acceptable in a dev/verification script but must
   not be imported into runtime paths.

---

## What Must Be True Before Running Against Untrusted Repos

1. `RFSN_EXEC_USE_DOCKER=1` and Docker daemon is running and verified.
2. Docker container config verified: `--network none`, `--user 1000:1000`,
   `--cap-drop ALL`, `--no-new-privileges`, memory/CPU/PID limits set.
3. `trace_execution` remains disabled.
4. `apply_semantic_patch` is only enabled after all safety tests pass.
5. Patch gate is enabled (`RFSN_PATCH_GATE_REQUIRED=1`).
6. Auth middleware is enabled (`RFSN_AUTH_REQUIRED=1`).
7. All tests in `tests/` pass with no failures.
