# Performance vs Security Tuning Map

Reference for RFSN hardening knobs and their tradeoffs.

## Knobs

| Setting | Security Impact | Performance Impact | Notes |
|---------|----------------|-------------------|-------|
| `RFSN_WARM_SANDBOX=0` | Strongest isolation | Slower cold start | Recommended for prod |
| `RFSN_VENV_MODE=per_run` | Deterministic replay | Slower installs | Replay-safe |
| `RFSN_VENV_MODE=shared` | Drift risk | Faster | Needs hash check |
| `RFSN_PATCH_GATE_REQUIRED=1` | Prevents semantic bugs | Small latency | Keep ON |
| `RFSN_AUTH_REQUIRED=1` | No silent downgrade | None | Mandatory |
| Snapshot hashing | Replay-safe | Small I/O | Recommended |
| Strict template checks | No injection | None | Mandatory |
| `RFSN_STRICT_IMAGE_DIGEST=1` | Prevents image swap | None | Mandatory |
| `RFSN_NETWORK_MIN_TIER=2` | Limits exfiltration | Slower deps | Recommended |
| `RFSN_HARDENING_STRICT=1` | Auto-repair drift | None | Recommended |

## Presets

### STRICT (research / replay)

```env
RFSN_DEV_MODE=0
RFSN_AUTH_REQUIRED=1
RFSN_PATCH_GATE_REQUIRED=1
RFSN_VENV_MODE=per_run
RFSN_WARM_SANDBOX=0
RFSN_NETWORK_MIN_TIER=2
RFSN_STRICT_IMAGE_DIGEST=1
RFSN_HARDENING_STRICT=1
```

### BALANCED (default)

```env
RFSN_DEV_MODE=0
RFSN_AUTH_REQUIRED=1
RFSN_PATCH_GATE_REQUIRED=1
RFSN_VENV_MODE=per_run
RFSN_WARM_SANDBOX=0
```

### FAST (non-critical / dev)

```env
RFSN_DEV_MODE=1
RFSN_VENV_MODE=shared
RFSN_WARM_SANDBOX=1
RFSN_PATCH_GATE_REQUIRED=0
```

## Dev Override

To bypass all hardening for local development:

```bash
export RFSN_DEV_MODE=1
```

This allows:

- Missing auth module
- Missing patch gate module
- Tag-based images (no digest)
- Warm sandbox reuse
- Shared venv mode
