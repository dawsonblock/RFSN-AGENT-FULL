
# Performance vs Security Tuning Map (PST)

| Setting | Security | Performance | Notes |
|--------|----------|-------------|------|
| RFSN_WARM_SANDBOX=0 | Highest isolation | Slower start | Recommended prod |
| RFSN_VENV_MODE=per_run | Deterministic | Slower install | Replay-safe |
| RFSN_VENV_MODE=shared | Drift risk | Faster | Needs hash check |
| PATCH_GATE_REQUIRED=1 | Prevents semantic bad patches | Small latency | Keep ON |
| AUTH_REQUIRED=1 | No silent downgrade | None | Mandatory |
| Snapshot hashing ON | Replay-safe | Small IO | Recommended |
| Strict template checks | No injection | None | Mandatory |
| Parallel tests | Neutral | Faster | Safe |
| Network tier gate | Limits exfil | Slower deps | Recommended |
| Warm sandbox=1 | Weak isolation | Fastest | Dev only |

## Suggested Presets

### STRICT (Research / Replay)

```bash
RFSN_DEV_MODE=0
RFSN_AUTH_REQUIRED=1
RFSN_PATCH_GATE_REQUIRED=1
RFSN_VENV_MODE=per_run
RFSN_WARM_SANDBOX=0
RFSN_HARDENING_STRICT=1
```

### BALANCED (Default)

```bash
RFSN_VENV_MODE=per_run
RFSN_WARM_SANDBOX=0
RFSN_PATCH_GATE_REQUIRED=1
```

### FAST (Dev / Non-critical)

```bash
RFSN_DEV_MODE=1
RFSN_VENV_MODE=shared
RFSN_WARM_SANDBOX=1
RFSN_PATCH_GATE_REQUIRED=0
```
