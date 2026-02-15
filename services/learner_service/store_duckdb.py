import time
from typing import Optional
import duckdb  # type: ignore[import-untyped]


class DuckStore:
    """Persistent outcome intelligence store.

    Tables:
      strategy_stats  — Thompson sampling posteriors
      episodes        — per-run episode log
      outcome_map     — patch → outcome mapping for reuse
      failure_index   — parsed failure signature index
                        for routing and clustering
    """

    def __init__(self, path: str) -> None:
        self.con = duckdb.connect(path)
        self._init()

    def _init(self) -> None:
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_stats(
              strategy_id VARCHAR,
              context_key VARCHAR,
              alpha DOUBLE,
              beta DOUBLE,
              trials BIGINT,
              wins BIGINT,
              losses BIGINT,
              last_updated DOUBLE,
              PRIMARY KEY(strategy_id, context_key)
            );
            """
        )
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS episodes(
              run_id VARCHAR,
              context_key VARCHAR,
              strategy_id VARCHAR,
              success BOOLEAN,
              failure_signature VARCHAR,
              ts DOUBLE
            );
            """
        )
        # Patch → outcome mapping: what patches
        # were tried, on what repo/task, and what
        # happened. Enables patch reuse and
        # avoidance of known-bad patches.
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS outcome_map(
              run_id VARCHAR,
              repo_id VARCHAR,
              task_hash VARCHAR,
              patch_hash VARCHAR,
              patch_files VARCHAR,
              patch_added_lines INTEGER,
              patch_deleted_lines INTEGER,
              test_exit_code INTEGER,
              tests_passed INTEGER,
              tests_failed INTEGER,
              tests_total INTEGER,
              failure_class VARCHAR,
              failure_signature VARCHAR,
              strategy_id VARCHAR,
              success BOOLEAN,
              dense_reward DOUBLE,
              ts DOUBLE
            );
            """
        )
        # Failure signature index: parsed and
        # clustered failure signatures for routing.
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS failure_index(
              signature_hash VARCHAR PRIMARY KEY,
              failure_class VARCHAR,
              failure_module VARCHAR,
              failure_test VARCHAR,
              failure_message VARCHAR,
              repo_id VARCHAR,
              occurrence_count INTEGER,
              last_seen DOUBLE,
              best_strategy_id VARCHAR,
              best_strategy_win_rate DOUBLE
            );
            """
        )
        # Trajectories: full step-by-step execution logs
        # for offline reinforcement learning (DPO/CQL).
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS trajectories(
              run_id VARCHAR PRIMARY KEY,
              repo_id VARCHAR,
              task_hash VARCHAR,
              strategy_id VARCHAR,
              success BOOLEAN,
              steps JSON,
              ts DOUBLE
            );
            """
        )

    # ── Strategy stats (Thompson sampling) ────

    def upsert_prior(
        self,
        strategy_id: str,
        context_key: str,
        a: float = 1.0,
        b: float = 1.0,
    ) -> None:
        self.con.execute(
            """
            INSERT INTO strategy_stats(
              strategy_id, context_key,
              alpha, beta,
              trials, wins, losses,
              last_updated
            )
            VALUES (?, ?, ?, ?, 0, 0, 0, ?)
            ON CONFLICT(strategy_id, context_key)
            DO NOTHING;
            """,
            [strategy_id, context_key, a, b, time.time()],
        )

    def get_posteriors(
        self,
        context_key: str,
    ) -> dict:
        rows = self.con.execute(
            """
            SELECT strategy_id,
                   alpha, beta,
                   trials, wins, losses
            FROM strategy_stats
            WHERE context_key = ?
            """,
            [context_key],
        ).fetchall()
        out: dict = {}
        for r in rows:
            out[r[0]] = {
                "alpha": float(r[1]),
                "beta": float(r[2]),
                "trials": int(r[3]),
                "wins": int(r[4]),
                "losses": int(r[5]),
            }
        return out

    def record_episode(
        self,
        run_id: str,
        context_key: str,
        strategy_id: str,
        success: bool,
        failure_signature: str,
    ) -> None:
        ts = time.time()
        self.con.execute(
            "INSERT INTO episodes" " VALUES (?, ?, ?, ?, ?, ?)",
            [
                run_id,
                context_key,
                strategy_id,
                bool(success),
                failure_signature or "",
                ts,
            ],
        )
        self.con.execute(
            """
            UPDATE strategy_stats
            SET alpha = alpha + ?,
                beta  = beta  + ?,
                trials = trials + 1,
                wins   = wins + ?,
                losses = losses + ?,
                last_updated = ?
            WHERE strategy_id = ?
              AND context_key = ?
            """,
            [
                1.0 if success else 0.0,
                0.0 if success else 1.0,
                1 if success else 0,
                0 if success else 1,
                ts,
                strategy_id,
                context_key,
            ],
        )

    # ── Outcome mapping (patch → result) ──────

    def record_outcome(
        self,
        run_id: str,
        repo_id: str,
        task_hash: str,
        patch_hash: str,
        patch_files: str,
        patch_added: int,
        patch_deleted: int,
        test_exit_code: int,
        tests_passed: int,
        tests_failed: int,
        tests_total: int,
        failure_class: str,
        failure_signature: str,
        strategy_id: str,
        success: bool,
        dense_reward: float,
    ) -> None:
        """Record a patch → outcome mapping."""
        self.con.execute(
            """
            INSERT INTO outcome_map VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
             ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                repo_id,
                task_hash,
                patch_hash,
                patch_files,
                patch_added,
                patch_deleted,
                test_exit_code,
                tests_passed,
                tests_failed,
                tests_total,
                failure_class,
                failure_signature,
                strategy_id,
                bool(success),
                dense_reward,
                time.time(),
            ],
        )

    def lookup_patch_history(
        self,
        task_hash: str,
        limit: int = 10,
    ) -> list[dict]:
        """Find past outcomes for similar tasks."""
        rows = self.con.execute(
            """
            SELECT patch_hash, patch_files,
                   success, dense_reward,
                   strategy_id, failure_class,
                   tests_passed, tests_failed
            FROM outcome_map
            WHERE task_hash = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            [task_hash, limit],
        ).fetchall()
        return [
            {
                "patch_hash": r[0],
                "patch_files": r[1],
                "success": bool(r[2]),
                "dense_reward": float(r[3]),
                "strategy_id": r[4],
                "failure_class": r[5],
                "tests_passed": int(r[6]),
                "tests_failed": int(r[7]),
            }
            for r in rows
        ]

    def latest_outcome(
        self,
        task_hash: str,
    ) -> Optional[dict]:
        rows = self.con.execute(
            """
            SELECT patch_hash, failure_signature,
                   tests_failed, tests_total
            FROM outcome_map
            WHERE task_hash = ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            [task_hash],
        ).fetchall()
        if not rows:
            return None
        r = rows[0]
        return {
            "patch_hash": str(r[0] or ""),
            "failure_signature": str(r[1] or ""),
            "tests_failed": int(r[2] or 0),
            "tests_total": int(r[3] or 0),
        }

    # ── Trajectories ──────────────────────────

    def record_trajectory(
        self,
        run_id: str,
        repo_id: str,
        task_hash: str,
        strategy_id: str,
        success: bool,
        steps: list[dict],
    ) -> None:
        """Record a full execution trajectory."""
        import json

        self.con.execute(
            """
            INSERT INTO trajectories(
              run_id, repo_id, task_hash,
              strategy_id, success, steps, ts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
              steps = excluded.steps,
              success = excluded.success,
              ts = excluded.ts
            """,
            [
                run_id,
                repo_id,
                task_hash,
                strategy_id,
                success,
                json.dumps(steps),
                time.time(),
            ],
        )

    def get_strategy_win_rates(
        self,
        context_key: str,
    ) -> dict[str, float]:
        """Get win rates per strategy for context."""
        rows = self.con.execute(
            """
            SELECT strategy_id,
                   SUM(CASE WHEN success
                       THEN 1 ELSE 0 END)
                     AS wins,
                   COUNT(*) AS total
            FROM outcome_map
            WHERE task_hash LIKE ?
            GROUP BY strategy_id
            """,
            [f"%{context_key}%"],
        ).fetchall()
        return {r[0]: float(r[1]) / max(r[2], 1) for r in rows}

    # ── Failure signature index ───────────────

    def upsert_failure(
        self,
        signature_hash: str,
        failure_class: str,
        failure_module: str,
        failure_test: str,
        failure_message: str,
        repo_id: str,
        best_strategy_id: Optional[str] = None,
        best_win_rate: Optional[float] = None,
    ) -> None:
        """Index a parsed failure signature."""
        self.con.execute(
            """
            INSERT INTO failure_index(
              signature_hash, failure_class,
              failure_module, failure_test,
              failure_message, repo_id,
              occurrence_count, last_seen,
              best_strategy_id,
              best_strategy_win_rate
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(signature_hash)
            DO UPDATE SET
              occurrence_count =
                failure_index.occurrence_count + 1,
              last_seen = ?,
              best_strategy_id = COALESCE(
                ?, failure_index.best_strategy_id
              ),
              best_strategy_win_rate = COALESCE(
                ?,
                failure_index.best_strategy_win_rate
              )
            """,
            [
                signature_hash,
                failure_class,
                failure_module,
                failure_test,
                failure_message,
                repo_id,
                time.time(),
                best_strategy_id,
                best_win_rate,
                time.time(),
                best_strategy_id,
                best_win_rate,
            ],
        )

    def lookup_failure(
        self,
        signature_hash: str,
    ) -> Optional[dict]:
        """Look up a known failure signature."""
        rows = self.con.execute(
            """
            SELECT failure_class, failure_module,
                   failure_test, failure_message,
                   occurrence_count,
                   best_strategy_id,
                   best_strategy_win_rate
            FROM failure_index
            WHERE signature_hash = ?
            """,
            [signature_hash],
        ).fetchall()
        if not rows:
            return None
        r = rows[0]
        return {
            "failure_class": r[0],
            "failure_module": r[1],
            "failure_test": r[2],
            "failure_message": r[3],
            "occurrence_count": int(r[4]),
            "best_strategy_id": r[5],
            "best_strategy_win_rate": (float(r[6]) if r[6] else None),
        }

    def find_similar_failures(
        self,
        failure_class: str,
        limit: int = 5,
    ) -> list[dict]:
        """Find failures with the same error class."""
        rows = self.con.execute(
            """
            SELECT signature_hash,
                   failure_class,
                   failure_module,
                   failure_test,
                   occurrence_count,
                   best_strategy_id,
                   best_strategy_win_rate
            FROM failure_index
            WHERE failure_class = ?
            ORDER BY occurrence_count DESC
            LIMIT ?
            """,
            [failure_class, limit],
        ).fetchall()
        return [
            {
                "signature_hash": r[0],
                "failure_class": r[1],
                "failure_module": r[2],
                "failure_test": r[3],
                "occurrence_count": int(r[4]),
                "best_strategy_id": r[5],
                "best_strategy_win_rate": (float(r[6]) if r[6] else None),
            }
            for r in rows
        ]
