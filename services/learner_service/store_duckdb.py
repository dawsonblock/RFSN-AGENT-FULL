import time
import duckdb  # type: ignore[import-untyped]


class DuckStore:
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
        self, context_key: str,
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
            "INSERT INTO episodes"
            " VALUES (?, ?, ?, ?, ?, ?)",
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
