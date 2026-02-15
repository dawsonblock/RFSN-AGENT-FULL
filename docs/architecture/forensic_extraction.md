# System Forensic Extraction & Hardening Roadmap

## 1. Core Shape of the System

This build is a deterministic repair + execution agent with four hard subsystems:

- **Kernel** (safety + ledger + gating)
- **Autofix pipeline** (analyze → plan → apply → verify)
- **Replay + forensic trace engine**
- **Learner** (behavior compression + outcome memory)

It is not autonomous intelligence. It is a controlled deterministic executor with learning hints.

## 2. Directory-Level Structural Map

### Control Plane (`autofix/`)

- `analyze.py`, `actions.py`, `apply.py`, `controller.py`, `ingest.py`, `verify.py`
- Flow: `ingest` → `analyze` → `plan` → `apply` → `verify` → `ledger` → `learner`

### Safety Kernel + Audit

- `data/kernel_ledger.jsonl`, `data/replay/manifests/`
- Provides immutable ledger, deterministic replay, run manifests.

### Learning Subsystem (`data/learner.duckdb`)

- Stores outcome traces, patch success/failure, fingerprints.
- Memory-based, not reasoning-based.

### Replay + Forensic Layer

- Stores inputs, actions, hashes, results for exact rebuilds and drift detection.

### Cluster Execution Mesh

- Distributed execution abstraction (currently weak isolation).

## 3. Security Extraction

### Strong Components

- **Ledger Immutability**: JSONL append-only.
- **Deterministic Replay**: Hash-based execution verification.
- **Verify Gate**: Mandatory validation.
- **Separated Pipeline**: Distinct stages for analyze/apply.

### Weak Points

- **No real sandbox**: Host-level execution risks escape.
- **No privilege boundary**: No capability isolation.
- **Learner not secured**: Writable runtime state (poisoning risk).
- **Verify is logical**: Checks outcome, not safety.

## 4. Hardening Architecture Design

### 1. Execution Containment

- **Capsule Model**: Read-only base, writable overlay workspace, no network, non-root, seccomp allowlist.
- **Implementation**: Namespaces, cgroups, unrestricted tmpdir.

### 2. Capability Segmentation

- Split privileges per stage (ingest=RO, apply=RW workspace, etc.).

### 3. Cryptographic Ledger Chain

- `hash_n = SHA256(hash_{n-1} + event_json)` to prevent tampering.

### 4. Physical Deterministic Replay

- Capture OS/kernel, env vars, seeds, clock source, temp FS snapshot.

### 5. Self-Healing Core

- **Failure Signal Extractor**: Convert verify/anomaly data to health signals.
- **Failure Clustering**: Group by root cause.
- **Risk Predictor**: Forecast failure probability.
- **Adaptive Hardening**: Tighten/relax constraints based on stability/risk.

### 6. Strategy Engine

- Map `root_cause` → `strategy` (local fix, refactor, rollback, etc.).
- Adaptive selection based on history.

### 7. Verification Strengthening

- Coverage delta, forbidden patterns, behavioral hashing, syscall profiling.

## 5. Implementation Plan (Proposed)

1. **Self-Healing Core**: Failure clustering, Root Cause Extraction, Stability Monitor.
2. **Execution Containment**: Implement the "Capsule" sandbox.
3. **Strategy Engine**: Connect Learner to Action Selection.
4. **Deterministic Replay Verifier**: Physical determinism checks.
