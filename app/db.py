"""SQLite 存储层。

数据库路径通过环境变量 ``DATABASE_PATH`` 配置，默认为当前目录下的
``experiments.db``。表结构在应用启动时通过 :func:`init_db` 创建，
新增表均使用 ``CREATE TABLE IF NOT EXISTS``，新增列通过幂等的
``ALTER TABLE`` 补齐，因此已有数据库在启动时会自动迁移，无需手工介入。

表说明：
* ``experiments`` / ``assignments``：实验与受试者的完整条件序列，
  序列只保存在服务端，不通过创建 / 详情接口下发；
  ``experiments.washout_seconds`` 为周期之间的洗脱期秒数（旧库默认 0）；
* ``subject_progress``：每个受试者下一待揭盲周期 ``next_period``；
* ``unblindings``：已揭盲记录（周期 -> 条件）；
* ``period_completions``：周期完成记录（周期 -> completed_at），
  推进下一周期前上一周期必须已完成且洗脱期已届满；
* ``advance_requests`` / ``completion_requests``：推进与完成接口的
  幂等结果，按 ``(实验, request_id)`` 去重。
* ``advance_batches``：批量受控揭盲的幂等结果，按 ``(实验, batch_id)``
  去重，保存首次请求绑定的有序成员列表与首次揭盲结果。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 写事务排队等待锁的时间，配合 BEGIN IMMEDIATE 保证并发推进串行化
BUSY_TIMEOUT_MS = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id              TEXT PRIMARY KEY,
    conditions      TEXT NOT NULL,          -- JSON 数组
    seed            INTEGER NOT NULL,
    group_size      INTEGER NOT NULL,       -- 一组完整序列的条数
    num_subjects    INTEGER NOT NULL,
    washout_seconds INTEGER NOT NULL DEFAULT 0,  -- 周期间洗脱期（秒），旧数据按 0
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    experiment_id  TEXT NOT NULL REFERENCES experiments(id),
    subject_id     TEXT NOT NULL,
    sequence_index INTEGER NOT NULL,     -- 命中的 Williams 序列下标
    sequence       TEXT NOT NULL,        -- JSON 数组，映射后的完整条件序列（服务端持有）
    PRIMARY KEY (experiment_id, subject_id)
);

CREATE TABLE IF NOT EXISTS subject_progress (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    subject_id    TEXT NOT NULL,
    next_period   INTEGER NOT NULL,      -- 下一待揭盲周期（从 1 开始）
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (experiment_id, subject_id)
);

CREATE TABLE IF NOT EXISTS unblindings (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    subject_id    TEXT NOT NULL,
    period        INTEGER NOT NULL,      -- 已揭盲周期（从 1 开始）
    condition     TEXT NOT NULL,         -- 该周期揭盲的条件
    request_id    TEXT NOT NULL,         -- 首次成功推进的幂等键
    revealed_at   TEXT NOT NULL,
    PRIMARY KEY (experiment_id, subject_id, period)
);

CREATE TABLE IF NOT EXISTS period_completions (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    subject_id    TEXT NOT NULL,
    period        INTEGER NOT NULL,      -- 已完成周期（必须先已揭盲）
    request_id    TEXT NOT NULL,         -- 首次成功完成的幂等键
    completed_at  TEXT NOT NULL,         -- 服务端登记的完成时刻
    PRIMARY KEY (experiment_id, subject_id, period)
);

CREATE TABLE IF NOT EXISTS advance_requests (
    experiment_id   TEXT NOT NULL REFERENCES experiments(id),
    request_id      TEXT NOT NULL,
    subject_id      TEXT NOT NULL,       -- 首次请求绑定的受试者
    expected_period INTEGER NOT NULL,    -- 首次请求绑定的预期周期
    period          INTEGER NOT NULL,    -- 首次结果揭盲的周期
    condition       TEXT NOT NULL,       -- 首次结果揭盲的条件
    revealed_at     TEXT NOT NULL,
    PRIMARY KEY (experiment_id, request_id)
);

CREATE TABLE IF NOT EXISTS completion_requests (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    request_id    TEXT NOT NULL,
    subject_id    TEXT NOT NULL,         -- 首次请求绑定的受试者
    period        INTEGER NOT NULL,      -- 首次请求绑定 / 完成的周期
    completed_at  TEXT NOT NULL,         -- 首次结果登记的完成时刻
    PRIMARY KEY (experiment_id, request_id)
);

CREATE TABLE IF NOT EXISTS advance_batches (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    batch_id      TEXT NOT NULL,
    members       TEXT NOT NULL,         -- JSON：首次请求绑定的有序成员
                                          -- [{subject_id, expected_period}]
    results       TEXT NOT NULL,         -- JSON：首次结果（按输入顺序的揭盲列表）
    created_at    TEXT NOT NULL,
    PRIMARY KEY (experiment_id, batch_id)
);
"""


class StalePeriodError(Exception):
    """expected_period 与受试者下一周期不一致（或实验已结束）。"""

    def __init__(self, next_period: int, complete: bool) -> None:
        self.next_period = next_period
        self.complete = complete
        super().__init__(
            f"expected period mismatch: next_period={next_period}, complete={complete}"
        )


class RequestIdConflictError(Exception):
    """request_id 已被另一条请求（不同 subject_id / period）占用。"""


class PriorPeriodNotCompletedError(Exception):
    """推进下一周期时，上一周期尚无完成记录。

    不携带任何后续周期信息，避免提前揭盲。
    """


class WashoutNotElapsedError(Exception):
    """推进下一周期时，洗脱期尚未届满。

    :param retry_after_seconds: 距离 ``completed_at + washout_seconds``
        还需等待的整秒数（至少 1）。
    """

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"washout not elapsed: retry_after={retry_after_seconds}s")


class CompletionOrderError(Exception):
    """登记完成的周期越序：尚未揭盲 / 已完成 / 落后或超前于当前周期。"""

    def __init__(self, current_period: int, already_completed: bool) -> None:
        self.current_period = current_period
        self.already_completed = already_completed
        super().__init__(
            f"completion out of order: current_period={current_period}, "
            f"already_completed={already_completed}"
        )


class BatchIdConflictError(Exception):
    """batch_id 已被成员列表不同的另一次批量请求占用。"""


class BatchMembersRejectedError(Exception):
    """批量揭盲中至少一个成员未通过门禁，整批必须零写入。

    :param failures: 每个失败成员一项，键为 ``subject_id`` / ``reason``，
        并按原因附带 ``next_period`` 或 ``retry_after_seconds``；
        不含任何条件 / 后续安排信息。
    """

    def __init__(self, failures: list[dict[str, Any]]) -> None:
        self.failures = failures
        super().__init__(f"batch members rejected: {len(failures)} failure(s)")


def utc_now() -> datetime:
    """服务端当前 UTC 时刻；测试可 monkeypatch 本函数以冻结时间。"""
    return datetime.now(timezone.utc)


def _database_path() -> str:
    return os.environ.get("DATABASE_PATH", "experiments.db")


def get_connection() -> sqlite3.Connection:
    path = Path(_database_path())
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def init_db() -> None:
    """建表 / 迁移（幂等），在应用启动时调用。

    * 新增表通过 ``CREATE TABLE IF NOT EXISTS`` 补齐；
    * 旧库的 ``experiments`` 表缺少 ``washout_seconds`` 列时以
      ``ALTER TABLE ... DEFAULT 0`` 补齐，旧实验按 0 秒洗脱期处理。
    """
    with closing(get_connection()) as conn:
        with conn:
            conn.executescript(SCHEMA)
            if not _column_exists(conn, "experiments", "washout_seconds"):
                conn.execute(
                    "ALTER TABLE experiments "
                    "ADD COLUMN washout_seconds INTEGER NOT NULL DEFAULT 0"
                )


def insert_experiment(
    experiment_id: str,
    conditions: list[str],
    seed: int,
    group_size: int,
    created_at: str,
    assignments: list[dict[str, Any]],
    washout_seconds: int = 0,
) -> None:
    with closing(get_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO experiments "
                "(id, conditions, seed, group_size, num_subjects, "
                "washout_seconds, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    json.dumps(conditions, ensure_ascii=False),
                    seed,
                    group_size,
                    len(assignments),
                    washout_seconds,
                    created_at,
                ),
            )
            conn.executemany(
                "INSERT INTO assignments "
                "(experiment_id, subject_id, sequence_index, sequence) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        experiment_id,
                        a["subject_id"],
                        a["sequence_index"],
                        json.dumps(a["sequence"], ensure_ascii=False),
                    )
                    for a in assignments
                ],
            )


def advance_period(
    experiment_id: str,
    subject_id: str,
    expected_period: int,
    request_id: str,
) -> dict[str, Any] | None:
    """原子推进一个受试者的揭盲进度。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：``(experiment_id, request_id)`` 已存在时直接返回首次结果，
       不再次推进；若同一 request_id 绑定了不同的 subject_id /
       expected_period，抛出 :class:`RequestIdConflictError`；
    2. 周期校验：``expected_period`` 必须等于受试者当前 ``next_period``，
       且实验尚未结束，否则抛出 :class:`StalePeriodError`（409），
       失败路径不写入任何数据、不揭露任何条件；
    3. 完成与洗脱期门禁：目标周期大于 1 时，上一周期必须存在完成记录，
       否则抛出 :class:`PriorPeriodNotCompletedError`；且服务端当前时间
       必须已达到 ``completed_at + washout_seconds``，否则抛出
       :class:`WashoutNotElapsedError`（携带 retry_after_seconds）。
       门禁失败只返回“需要等待”，不揭露下一周期条件；
    4. 揭盲：写入 ``unblindings``、推进 ``subject_progress``、
       落盘 ``advance_requests``，三者一起提交或一起回滚。

    服务端时间在事务内读取，保证并发完成 / 推进下判断与写入原子一致。
    返回首次 / 重放的揭盲结果；实验或受试者不存在时返回 ``None``。
    """
    conn = get_connection()
    try:
        # 显式事务，避免 sqlite3 隐式的 deferred 事务
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            result = _advance_locked(
                conn,
                experiment_id,
                subject_id,
                expected_period,
                request_id,
            )
            if result is not None and not result["_replayed"]:
                conn.execute("COMMIT")
                committed = True
            if result is not None:
                result.pop("_replayed", None)
            return result
        finally:
            if not committed:
                # 重放（只读）、409、404 或异常时结束事务
                conn.rollback()
    finally:
        conn.close()


def _get_washout(conn: sqlite3.Connection, experiment_id: str) -> int | None:
    row = conn.execute(
        "SELECT washout_seconds FROM experiments WHERE id = ?",
        (experiment_id,),
    ).fetchone()
    return None if row is None else int(row["washout_seconds"])


def _advance_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    subject_id: str,
    expected_period: int,
    request_id: str,
) -> dict[str, Any] | None:
    # 1) 幂等命中：返回首次结果，绝不重复推进
    request_row = conn.execute(
        "SELECT subject_id, expected_period, period, condition, revealed_at "
        "FROM advance_requests WHERE experiment_id = ? AND request_id = ?",
        (experiment_id, request_id),
    ).fetchone()
    if request_row is not None:
        if (
            request_row["subject_id"] != subject_id
            or request_row["expected_period"] != expected_period
        ):
            raise RequestIdConflictError()
        return {
            "experiment_id": experiment_id,
            "subject_id": subject_id,
            "period": request_row["period"],
            "condition": request_row["condition"],
            "request_id": request_id,
            "revealed_at": request_row["revealed_at"],
            "_replayed": True,
        }

    # 2) 实验必须存在（决定洗脱期），受试者序列必须存在（均不做下发）
    washout = _get_washout(conn, experiment_id)
    if washout is None:
        return None

    assignment_row = conn.execute(
        "SELECT sequence FROM assignments "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    if assignment_row is None:
        return None

    sequence = json.loads(assignment_row["sequence"])
    num_periods = len(sequence)

    progress_row = conn.execute(
        "SELECT next_period FROM subject_progress "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    next_period = progress_row["next_period"] if progress_row is not None else 1

    # 3) 仅当请求的正是下一周期且实验未结束时才继续（不揭露任何条件）
    if next_period > num_periods or expected_period != next_period:
        raise StalePeriodError(
            next_period=next_period, complete=next_period > num_periods
        )

    # 4) 周期完成与洗脱期门禁（周期 1 无前置周期）
    if next_period > 1:
        completion_row = conn.execute(
            "SELECT completed_at FROM period_completions "
            "WHERE experiment_id = ? AND subject_id = ? AND period = ?",
            (experiment_id, subject_id, next_period - 1),
        ).fetchone()
        if completion_row is None:
            raise PriorPeriodNotCompletedError()
        completed_at = datetime.fromisoformat(completion_row["completed_at"])
        # 服务端时间在锁内读取：与并发的完成登记互斥，判断结果原子一致
        now = utc_now()
        next_available_at = completed_at + timedelta(seconds=washout)
        if now < next_available_at:
            retry_after = math.ceil((next_available_at - now).total_seconds())
            raise WashoutNotElapsedError(max(1, retry_after))
        revealed_at = now.isoformat()
    else:
        revealed_at = utc_now().isoformat()

    condition = sequence[next_period - 1]

    conn.execute(
        "INSERT INTO unblindings "
        "(experiment_id, subject_id, period, condition, request_id, revealed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            experiment_id,
            subject_id,
            next_period,
            condition,
            request_id,
            revealed_at,
        ),
    )
    conn.execute(
        "INSERT INTO subject_progress "
        "(experiment_id, subject_id, next_period, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(experiment_id, subject_id) DO UPDATE SET "
        "next_period = excluded.next_period, updated_at = excluded.updated_at",
        (experiment_id, subject_id, next_period + 1, revealed_at),
    )
    conn.execute(
        "INSERT INTO advance_requests "
        "(experiment_id, request_id, subject_id, expected_period, "
        "period, condition, revealed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            experiment_id,
            request_id,
            subject_id,
            expected_period,
            next_period,
            condition,
            revealed_at,
        ),
    )

    return {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "period": next_period,
        "condition": condition,
        "request_id": request_id,
        "revealed_at": revealed_at,
        "_replayed": False,
    }


def advance_batch(
    experiment_id: str,
    batch_id: str,
    members: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """在单个写事务内批量受控揭盲并推进。

    流程（全部在一个 ``BEGIN IMMEDIATE`` 事务内完成）：

    1. 幂等检查：``(experiment_id, batch_id)`` 已存在时，仅当本次成员列表
       （``subject_id`` / ``expected_period`` 及顺序）与首次完全一致才返回
       首次结果；成员或顺序不同则抛出 :class:`BatchIdConflictError`；
    2. 门禁校验：对每个成员**先统一**做现有单条推进的全部校验——
       受试者必须存在、``expected_period`` 必须等于其下一周期且实验未结束、
       周期大于 1 时上一周期必须已完成且洗脱期已届满。门禁时刻以同一
       服务端时间读取，任一项失败即汇总全部失败项并抛出
       :class:`BatchMembersRejectedError`，**整批回滚、零写入**；
    3. 揭盲：仅当全部成员通过后，才**按输入顺序**写入每条揭盲记录、
       推进 ``subject_progress``，最后落盘 ``advance_batches`` 幂等结果，
       一起提交或一起回滚。

    失败项只携带 ``subject_id`` 与原因（及安全的 ``next_period`` /
    ``retry_after_seconds``），不附带任何条件。实验不存在时返回 ``None``。
    """
    conn = get_connection()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            result = _advance_batch_locked(
                conn, experiment_id, batch_id, members
            )
            if result is not None and not result["_replayed"]:
                conn.execute("COMMIT")
                committed = True
            if result is not None:
                result.pop("_replayed", None)
            return result
        finally:
            if not committed:
                conn.rollback()
    finally:
        conn.close()


def _advance_batch_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    batch_id: str,
    members: list[dict[str, Any]],
) -> dict[str, Any] | None:
    # 1) 幂等命中：成员列表（含顺序）一致才返回首次结果，否则 409
    batch_row = conn.execute(
        "SELECT members, results FROM advance_batches "
        "WHERE experiment_id = ? AND batch_id = ?",
        (experiment_id, batch_id),
    ).fetchone()
    if batch_row is not None:
        bound_members = [
            {"subject_id": m["subject_id"], "expected_period": m["expected_period"]}
            for m in json.loads(batch_row["members"])
        ]
        request_members = [
            {"subject_id": m["subject_id"], "expected_period": m["expected_period"]}
            for m in members
        ]
        if bound_members != request_members:
            raise BatchIdConflictError()
        return {
            "experiment_id": experiment_id,
            "batch_id": batch_id,
            "results": json.loads(batch_row["results"]),
            "_replayed": True,
        }

    # 2) 实验必须存在（决定洗脱期）
    washout = _get_washout(conn, experiment_id)
    if washout is None:
        return None

    # 一次取齐全部成员的序列与进度（同一事务快照，期间无其他写入）
    subject_ids = [m["subject_id"] for m in members]
    placeholders = ",".join("?" for _ in subject_ids)
    assignment_rows = {
        row["subject_id"]: row["sequence"]
        for row in conn.execute(
            f"SELECT subject_id, sequence FROM assignments "
            f"WHERE experiment_id = ? AND subject_id IN ({placeholders})",
            (experiment_id, *subject_ids),
        ).fetchall()
    }
    progress_rows = {
        row["subject_id"]: row["next_period"]
        for row in conn.execute(
            f"SELECT subject_id, next_period FROM subject_progress "
            f"WHERE experiment_id = ? AND subject_id IN ({placeholders})",
            (experiment_id, *subject_ids),
        ).fetchall()
    }

    # 同一批内同一受试者出现两次（周期不同）时，第二个人工复制的成员
    # 会以“推进后的下一周期”视角参与门禁校验，确保整批零写入而非部分推进
    pending_next: dict[str, int] = {}
    # 只取本批成员的上一周期完成时间（周期 1 无需完成记录）
    completion_rows = {
        (row["subject_id"], row["period"]): row["completed_at"]
        for row in conn.execute(
            f"SELECT subject_id, period, completed_at FROM period_completions "
            f"WHERE experiment_id = ? AND subject_id IN ({placeholders})",
            (experiment_id, *subject_ids),
        ).fetchall()
    }

    # 门禁时刻：整批使用同一服务端时间（在写锁内读取）
    gate_now = utc_now()

    # 计划列表：通过门禁的成员收集其待揭盲信息；失败则汇总后整批拒绝
    plan: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for member in members:
        subject_id = member["subject_id"]
        expected_period = member["expected_period"]
        failure: dict[str, Any] | None = None

        sequence_json = assignment_rows.get(subject_id)
        if sequence_json is None:
            failure = {"subject_id": subject_id, "reason": "subject_not_found"}
        else:
            sequence = json.loads(sequence_json)
            num_periods = len(sequence)
            next_period = pending_next.get(
                subject_id, progress_rows.get(subject_id, 1)
            )

            if next_period > num_periods or expected_period != next_period:
                failure = {
                    "subject_id": subject_id,
                    "reason": (
                        "experiment_complete"
                        if next_period > num_periods
                        else "period_mismatch"
                    ),
                    "next_period": next_period,
                }
            elif next_period > 1:
                completed_at_text = completion_rows.get(
                    (subject_id, next_period - 1)
                )
                if completed_at_text is None:
                    failure = {
                        "subject_id": subject_id,
                        "reason": "prior_period_not_completed",
                    }
                else:
                    completed_at = datetime.fromisoformat(completed_at_text)
                    next_available_at = completed_at + timedelta(seconds=washout)
                    if gate_now < next_available_at:
                        retry_after = math.ceil(
                            (next_available_at - gate_now).total_seconds()
                        )
                        failure = {
                            "subject_id": subject_id,
                            "reason": "washout_not_elapsed",
                            "retry_after_seconds": max(1, retry_after),
                        }

        if failure is None:
            plan.append(
                {
                    "subject_id": subject_id,
                    "period": next_period,
                    "condition": sequence[next_period - 1],
                }
            )
            # 同受试者后续成员看到推进后的下一周期
            pending_next[subject_id] = next_period + 1
        else:
            failures.append(failure)

    if failures:
        # 整批零写入：不揭盲任何条件，由外层事务回滚
        raise BatchMembersRejectedError(failures)

    # 3) 全部通过：按输入顺序揭盲并推进（每条时间戳在锁内顺序生成）
    results: list[dict[str, Any]] = []
    for item in plan:
        subject_id = item["subject_id"]
        period = item["period"]
        condition = item["condition"]
        # 批量揭盲在 advance_requests 之外使用带前缀的派生幂等键，
        # 与单条推进的 request_id 空间互不干扰
        derived_request_id = f"batch:{batch_id}:{subject_id}:{period}"
        revealed_at = utc_now().isoformat()

        conn.execute(
            "INSERT INTO unblindings "
            "(experiment_id, subject_id, period, condition, request_id, revealed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                experiment_id,
                subject_id,
                period,
                condition,
                derived_request_id,
                revealed_at,
            ),
        )
        conn.execute(
            "INSERT INTO subject_progress "
            "(experiment_id, subject_id, next_period, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(experiment_id, subject_id) DO UPDATE SET "
            "next_period = excluded.next_period, updated_at = excluded.updated_at",
            (experiment_id, subject_id, period + 1, revealed_at),
        )
        results.append(
            {
                "experiment_id": experiment_id,
                "subject_id": subject_id,
                "period": period,
                "condition": condition,
                "request_id": derived_request_id,
                "revealed_at": revealed_at,
            }
        )

    conn.execute(
        "INSERT INTO advance_batches "
        "(experiment_id, batch_id, members, results, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            experiment_id,
            batch_id,
            json.dumps(
                [
                    {"subject_id": m["subject_id"], "expected_period": m["expected_period"]}
                    for m in members
                ],
                ensure_ascii=False,
            ),
            json.dumps(results, ensure_ascii=False),
            gate_now.isoformat(),
        ),
    )

    return {
        "experiment_id": experiment_id,
        "batch_id": batch_id,
        "results": results,
        "_replayed": False,
    }


def complete_period(
    experiment_id: str,
    subject_id: str,
    period: int,
    request_id: str,
) -> dict[str, Any] | None:
    """原子登记一个受试者当前周期的完成时间。

    在单个 ``BEGIN IMMEDIATE`` 事务内完成：

    1. 幂等检查：``(experiment_id, request_id)`` 已存在时直接返回首次结果；
       同一 request_id 绑定不同 subject_id / period 时抛出
       :class:`RequestIdConflictError`；
    2. 越序校验：只有“已揭盲且尚未完成的当前周期”可登记完成。
       周期尚未揭盲、已经完成、落后 / 超前于当前周期（含实验未开始或
       已结束），均抛出 :class:`CompletionOrderError`（409）；
    3. 登记：服务端当前时间写入 ``period_completions`` 并落盘
       ``completion_requests``，一起提交或一起回滚。

    返回首次 / 重放的完成结果；实验或受试者不存在时返回 ``None``。
    """
    conn = get_connection()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            result = _complete_locked(
                conn, experiment_id, subject_id, period, request_id
            )
            if result is not None and not result["_replayed"]:
                conn.execute("COMMIT")
                committed = True
            if result is not None:
                result.pop("_replayed", None)
            return result
        finally:
            if not committed:
                conn.rollback()
    finally:
        conn.close()


def _complete_locked(
    conn: sqlite3.Connection,
    experiment_id: str,
    subject_id: str,
    period: int,
    request_id: str,
) -> dict[str, Any] | None:
    # 1) 幂等命中：返回首次结果
    request_row = conn.execute(
        "SELECT subject_id, period, completed_at FROM completion_requests "
        "WHERE experiment_id = ? AND request_id = ?",
        (experiment_id, request_id),
    ).fetchone()
    if request_row is not None:
        if request_row["subject_id"] != subject_id or request_row["period"] != period:
            raise RequestIdConflictError()
        return {
            "experiment_id": experiment_id,
            "subject_id": subject_id,
            "period": request_row["period"],
            "request_id": request_id,
            "completed_at": request_row["completed_at"],
            "_replayed": True,
        }

    # 2) 实验 / 受试者必须存在
    if _get_washout(conn, experiment_id) is None:
        return None
    assignment_row = conn.execute(
        "SELECT sequence FROM assignments "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    if assignment_row is None:
        return None

    num_periods = len(json.loads(assignment_row["sequence"]))

    progress_row = conn.execute(
        "SELECT next_period FROM subject_progress "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    next_period = progress_row["next_period"] if progress_row is not None else 1
    current_period = next_period - 1  # 已揭盲但未必已完成的当前周期

    # 3) 只有已揭盲（period == current_period）且尚未完成的周期可登记
    if not (1 <= period <= num_periods) or period != current_period:
        raise CompletionOrderError(
            current_period=max(current_period, 0), already_completed=False
        )
    completion_row = conn.execute(
        "SELECT 1 FROM period_completions "
        "WHERE experiment_id = ? AND subject_id = ? AND period = ?",
        (experiment_id, subject_id, period),
    ).fetchone()
    if completion_row is not None:
        raise CompletionOrderError(
            current_period=current_period, already_completed=True
        )

    # 服务端时间在锁内生成
    completed_at = utc_now().isoformat()
    conn.execute(
        "INSERT INTO period_completions "
        "(experiment_id, subject_id, period, request_id, completed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (experiment_id, subject_id, period, request_id, completed_at),
    )
    conn.execute(
        "INSERT INTO completion_requests "
        "(experiment_id, request_id, subject_id, period, completed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (experiment_id, request_id, subject_id, period, completed_at),
    )

    return {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "period": period,
        "request_id": request_id,
        "completed_at": completed_at,
        "_replayed": False,
    }


def get_progress(experiment_id: str, subject_id: str) -> dict[str, Any] | None:
    """查询受试者揭盲进度。

    只返回已揭盲记录（含各自的完成时间）、下一周期以及下一周期最早可
    推进时间 ``next_available_at``，不包含完整序列或后续安排。
    实验或受试者不存在时返回 ``None``。
    """
    with closing(get_connection()) as conn:
        experiment_row = conn.execute(
            "SELECT washout_seconds FROM experiments WHERE id = ?",
            (experiment_id,),
        ).fetchone()
        assignment_row = conn.execute(
            "SELECT sequence FROM assignments "
            "WHERE experiment_id = ? AND subject_id = ?",
            (experiment_id, subject_id),
        ).fetchone()
        if assignment_row is None or experiment_row is None:
            return None
        num_periods = len(json.loads(assignment_row["sequence"]))
        washout = int(experiment_row["washout_seconds"])

        reveal_rows = conn.execute(
            "SELECT period, condition, revealed_at FROM unblindings "
            "WHERE experiment_id = ? AND subject_id = ? ORDER BY period",
            (experiment_id, subject_id),
        ).fetchall()
        completion_rows = conn.execute(
            "SELECT period, completed_at FROM period_completions "
            "WHERE experiment_id = ? AND subject_id = ? ORDER BY period",
            (experiment_id, subject_id),
        ).fetchall()
        progress_row = conn.execute(
            "SELECT next_period FROM subject_progress "
            "WHERE experiment_id = ? AND subject_id = ?",
            (experiment_id, subject_id),
        ).fetchone()

    next_period = progress_row["next_period"] if progress_row is not None else 1
    completed_at_by_period = {
        row["period"]: row["completed_at"] for row in completion_rows
    }

    # next_available_at 仅由“上一周期完成记录 + 洗脱期”推导；
    # 周期 1、实验已结束或上一周期尚未完成时为 None（不泄露任何安排）。
    next_available_at: str | None = None
    if 1 < next_period <= num_periods:
        prior_completed_at = completed_at_by_period.get(next_period - 1)
        if prior_completed_at is not None:
            next_available_at = (
                datetime.fromisoformat(prior_completed_at)
                + timedelta(seconds=washout)
            ).isoformat()

    return {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "next_period": next_period,
        "num_periods": num_periods,
        "washout_seconds": washout,
        "completed": next_period > num_periods,
        "next_available_at": next_available_at,
        "reveals": [
            {
                "period": row["period"],
                "condition": row["condition"],
                "revealed_at": row["revealed_at"],
                "completed_at": completed_at_by_period.get(row["period"]),
            }
            for row in reveal_rows
        ],
    }


def get_experiment(experiment_id: str) -> dict[str, Any] | None:
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if row is None:
            return None
        assignment_rows = conn.execute(
            "SELECT subject_id FROM assignments "
            "WHERE experiment_id = ? ORDER BY rowid",
            (experiment_id,),
        ).fetchall()
    return _experiment_dict(row, assignment_rows)


def list_experiments() -> list[dict[str, Any]]:
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT * FROM experiments ORDER BY created_at, id"
        ).fetchall()
    return [_experiment_dict(row, None) for row in rows]


def _experiment_dict(
    row: sqlite3.Row, assignment_rows: list[sqlite3.Row] | None
) -> dict[str, Any]:
    conditions = json.loads(row["conditions"])
    experiment: dict[str, Any] = {
        "experiment_id": row["id"],
        "conditions": conditions,
        "seed": row["seed"],
        "group_size": row["group_size"],
        "num_subjects": row["num_subjects"],
        "num_periods": len(conditions),
        "washout_seconds": row["washout_seconds"],
        "created_at": row["created_at"],
    }
    if assignment_rows is not None:
        # 只回传受试者名册，完整条件序列保留在服务端，按周期受控揭盲
        experiment["assignments"] = [
            {"subject_id": a["subject_id"]} for a in assignment_rows
        ]
    return experiment
