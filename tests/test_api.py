"""API 与 Williams 序列的自动化测试。"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.williams import group_size, williams_sequences


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def assert_balanced(sequences: list[list[str]], n: int) -> None:
    """校验位置平衡与有向相邻对平衡。"""
    size = group_size(n)
    assert len(sequences) % size == 0
    k = len(sequences) // size  # 完整组数
    per_position = k * (1 if n % 2 == 0 else 2)

    # 每个条件在每个位置出现次数相等
    for pos in range(n):
        counts = Counter(seq[pos] for seq in sequences)
        assert len(counts) == n
        assert all(v == per_position for v in counts.values())

    # 每个有向相邻条件对出现次数相等
    pairs: Counter = Counter()
    for seq in sequences:
        for a, b in zip(seq, seq[1:]):
            pairs[(a, b)] += 1
    assert len(pairs) == n * (n - 1)
    assert len(set(pairs.values())) == 1


def create_experiment(
    client: TestClient,
    conditions: list[str] | None = None,
    subject_ids: list[str] | None = None,
    seed: int = 42,
    washout_seconds: int = 0,
):
    conditions = conditions or ["A", "B", "C"]
    subject_ids = subject_ids or [f"s{i}" for i in range(6)]
    resp = client.post(
        "/experiments",
        json={
            "conditions": conditions,
            "subject_ids": subject_ids,
            "seed": seed,
            "washout_seconds": washout_seconds,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json(), subject_ids


def complete_period(
    client: TestClient,
    experiment_id: str,
    subject_id: str,
    period: int,
    request_id: str | None = None,
):
    resp = client.post(
        f"/experiments/{experiment_id}/complete",
        json={
            "subject_id": subject_id,
            "period": period,
            "request_id": request_id or f"{subject_id}-c{period}",
        },
    )
    return resp


def reveal_period(
    client: TestClient,
    experiment_id: str,
    subject_id: str,
    period: int,
    req_prefix: str = "",
):
    return client.post(
        f"/experiments/{experiment_id}/advance",
        json={
            "subject_id": subject_id,
            "expected_period": period,
            "request_id": f"{req_prefix}{subject_id}-p{period}",
        },
    )


def reveal_sequence(
    client: TestClient,
    experiment_id: str,
    subject_id: str,
    num_periods: int,
    req_prefix: str = "",
) -> list[str]:
    """通过推进接口逐周期揭盲，重建该受试者的条件序列。

    默认实验 washout=0，因此每个周期揭盲后登记完成即可立刻推进下一周期。
    """
    sequence: list[str] = []
    for period in range(1, num_periods + 1):
        resp = reveal_period(
            client, experiment_id, subject_id, period, req_prefix
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["period"] == period
        sequence.append(body["condition"])
        if period < num_periods:
            done = complete_period(
                client,
                experiment_id,
                subject_id,
                period,
                f"{req_prefix}{subject_id}-c{period}",
            )
            assert done.status_code == 200, done.text
    return sequence


def assert_no_sequence_leak(payload: object) -> None:
    """响应中任何层级都不得出现条件序列或序列下标。"""
    forbidden = {"sequence", "sequence_index"}
    if isinstance(payload, dict):
        assert forbidden.isdisjoint(payload.keys())
        for value in payload.values():
            assert_no_sequence_leak(value)
    elif isinstance(payload, list):
        for item in payload:
            assert_no_sequence_leak(item)


# ---------- Williams 序列单元测试 ----------

@pytest.mark.parametrize("n", range(2, 9))
def test_williams_sequences_balanced(n: int) -> None:
    sequences = [[str(i) for i in seq] for seq in williams_sequences(n)]
    assert len(sequences) == group_size(n)
    assert_balanced(sequences, n)


# ---------- 创建 / 查询接口（不再下发完整序列） ----------

def test_create_even_conditions(client: TestClient) -> None:
    body, subject_ids = create_experiment(
        client,
        conditions=["A", "B", "C", "D"],
        subject_ids=[f"s{i}" for i in range(8)],
    )
    assert body["group_size"] == 4
    assert body["num_subjects"] == 8
    assert body["num_periods"] == 4
    assert len(body["assignments"]) == 8
    # 名册只含受试者 ID，且顺序与提交一致
    assert [a["subject_id"] for a in body["assignments"]] == subject_ids
    assert set(body["assignments"][0].keys()) == {"subject_id"}
    assert_no_sequence_leak(body)

    # 序列只能通过逐周期揭盲重建，且仍满足 Williams 平衡
    sequences = [
        reveal_sequence(client, body["experiment_id"], s, 4)
        for s in subject_ids
    ]
    assert_balanced(sequences, 4)
    # 2 个完整组：每条 Williams 序列恰好被使用 2 次
    assert sorted(Counter(tuple(seq) for seq in sequences).values()) == [
        2, 2, 2, 2
    ]


def test_create_odd_conditions(client: TestClient) -> None:
    body, subject_ids = create_experiment(
        client,
        conditions=["A", "B", "C"],
        subject_ids=[f"s{i}" for i in range(6)],
        seed=7,
    )
    assert body["group_size"] == 6
    assert body["num_periods"] == 3
    assert_no_sequence_leak(body)
    sequences = [
        reveal_sequence(client, body["experiment_id"], s, 3)
        for s in subject_ids
    ]
    assert_balanced(sequences, 3)


def test_same_input_and_seed_gives_same_assignment(client: TestClient) -> None:
    payload = {
        "conditions": ["A", "B", "C"],
        "subject_ids": [f"s{i}" for i in range(12)],
        "seed": 123,
    }
    first = client.post("/experiments", json=payload).json()
    second = client.post("/experiments", json=payload).json()
    assert first["assignments"] == second["assignments"]
    # 逐周期揭盲重建的序列也完全一致（确定性分配）
    first_seqs = [
        reveal_sequence(client, first["experiment_id"], s, 3, "first-")
        for s in payload["subject_ids"]
    ]
    second_seqs = [
        reveal_sequence(client, second["experiment_id"], s, 3, "second-")
        for s in payload["subject_ids"]
    ]
    assert first_seqs == second_seqs


def test_subject_count_not_multiple_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C"],
            "subject_ids": [f"s{i}" for i in range(7)],
            "seed": 1,
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["group_size"] == 6
    assert detail["subject_count"] == 7
    assert detail["nearest_acceptable"] == [6, 12]


def test_subject_count_below_one_group_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C", "D"],
            "subject_ids": ["s1", "s2"],
            "seed": 1,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["nearest_acceptable"] == [4]


def test_duplicate_conditions_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "A", "B"],
            "subject_ids": [f"s{i}" for i in range(6)],
            "seed": 1,
        },
    )
    assert resp.status_code == 422


def test_duplicate_subjects_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C"],
            "subject_ids": ["s1", "s1", "s2", "s3", "s4", "s5"],
            "seed": 1,
        },
    )
    assert resp.status_code == 422


def test_too_few_conditions_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={"conditions": ["A"], "subject_ids": ["s1"], "seed": 1},
    )
    assert resp.status_code == 422


def test_get_experiment_does_not_leak_sequence(client: TestClient) -> None:
    created, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    experiment_id = created["experiment_id"]

    resp = client.get(f"/experiments/{experiment_id}")
    assert resp.status_code == 200
    assert resp.json() == created
    assert_no_sequence_leak(resp.json())

    listing = client.get("/experiments").json()
    entry = next(e for e in listing if e["experiment_id"] == experiment_id)
    assert entry["num_periods"] == 2
    assert_no_sequence_leak(entry)
    assert "assignments" not in entry


def test_get_unknown_experiment_returns_404(client: TestClient) -> None:
    assert client.get("/experiments/nope").status_code == 404


# ---------- 按周期受控揭盲 ----------

def test_advance_reveals_one_period_and_progresses(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B", "C"], subject_ids=[f"s{i}" for i in range(6)]
    )
    eid = body["experiment_id"]

    progress = client.get(f"/experiments/{eid}/subjects/s0/progress").json()
    assert progress["next_period"] == 1
    assert progress["num_periods"] == 3
    assert progress["washout_seconds"] == 0
    assert progress["completed"] is False
    assert progress["next_available_at"] is None
    assert progress["reveals"] == []

    first = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s0", "expected_period": 1, "request_id": "r1"},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["period"] == 1
    assert first_body["condition"] in {"A", "B", "C"}
    assert first_body["request_id"] == "r1"
    assert "revealed_at" in first_body

    progress = client.get(f"/experiments/{eid}/subjects/s0/progress").json()
    assert progress["next_period"] == 2
    assert progress["next_available_at"] is None
    assert progress["reveals"] == [
        {
            "period": 1,
            "condition": first_body["condition"],
            "revealed_at": first_body["revealed_at"],
            "completed_at": None,
        }
    ]
    assert_no_sequence_leak(progress)


def test_stale_or_skipped_period_returns_409_and_reveals_nothing(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=3
    )
    eid = body["experiment_id"]

    # 跳过周期 1 直接请求周期 2
    resp = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "skip"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "period_mismatch"
    assert detail["next_period"] == 1
    assert "condition" not in detail

    # 失败不推进、不留幂等记录：同一 request_id 在正确周期上仍可首次成功
    ok = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "skip"},
    )
    assert ok.status_code == 200

    # 周期 1 已揭盲后，过期请求周期 1 再次冲突
    stale = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "late"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["next_period"] == 2

    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


def test_same_request_id_retry_is_idempotent(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=9
    )
    eid = body["experiment_id"]
    payload = {
        "subject_id": "s1",
        "expected_period": 1,
        "request_id": "idem-1",
    }

    first = client.post(f"/experiments/{eid}/advance", json=payload)
    assert first.status_code == 200
    first_body = first.json()

    for _ in range(3):
        retry = client.post(f"/experiments/{eid}/advance", json=payload)
        assert retry.status_code == 200
        assert retry.json() == first_body

    # 重试不重复推进
    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1

    # 登记周期 1 完成（washout=0，完成后即可推进）
    done = complete_period(client, eid, "s1", 1, "c1")
    assert done.status_code == 200

    # 推进到周期 2 后，用旧 request_id 重放仍返回首次（周期 1）结果
    second = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "idem-2"},
    )
    assert second.status_code == 200
    assert second.json()["period"] == 2

    replay = client.post(f"/experiments/{eid}/advance", json=payload)
    assert replay.status_code == 200
    assert replay.json() == first_body


def test_request_id_reused_with_different_payload_returns_409(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=4
    )
    eid = body["experiment_id"]

    first = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "shared"},
    )
    assert first.status_code == 200

    # 同一 request_id 绑定不同受试者
    other_subject = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s2", "expected_period": 1, "request_id": "shared"},
    )
    assert other_subject.status_code == 409
    assert other_subject.json()["detail"]["reason"] == "request_id_conflict"

    # s2 未因此被推进或揭盲
    progress = client.get(f"/experiments/{eid}/subjects/s2/progress").json()
    assert progress["next_period"] == 1
    assert progress["reveals"] == []


def test_concurrent_advance_has_single_winner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 11,
            },
        ).json()
    eid = created["experiment_id"]

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        # 每个线程使用独立 client（独立门户线程），共享同一个 SQLite 文件
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/advance",
                json={
                    "subject_id": "s1",
                    "expected_period": 1,
                    "request_id": f"race-{i}",
                },
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1

    with TestClient(app) as verify:
        progress = verify.get(
            f"/experiments/{eid}/subjects/s1/progress"
        ).json()
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


def test_advance_after_final_period_returns_409(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=8
    )
    eid = body["experiment_id"]

    sequence = reveal_sequence(client, eid, "s1", 2)
    assert len(sequence) == 2

    done = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 3, "request_id": "over"},
    )
    assert done.status_code == 409
    assert done.json()["detail"]["reason"] == "experiment_complete"
    assert done.json()["detail"]["next_period"] == 3

    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["completed"] is True
    assert progress["next_period"] == 3
    assert [r["period"] for r in progress["reveals"]] == [1, 2]


def test_advance_unknown_experiment_or_subject_returns_404(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=6
    )
    eid = body["experiment_id"]
    payload = {
        "subject_id": "ghost",
        "expected_period": 1,
        "request_id": "x",
    }

    assert client.post("/experiments/missing/advance", json=payload).status_code == 404
    assert client.post(f"/experiments/{eid}/advance", json=payload).status_code == 404
    assert client.get(f"/experiments/{eid}/subjects/ghost/progress").status_code == 404


def test_invalid_advance_payload_rejected(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=2
    )
    eid = body["experiment_id"]

    resp = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 0, "request_id": "r"},
    )
    assert resp.status_code == 422

    resp = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": ""},
    )
    assert resp.status_code == 422


# ---------- 周期完成与洗脱期门禁 ----------

def test_washout_configured_on_create_and_defaults_zero(client: TestClient) -> None:
    body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=["s1", "s2"],
        washout_seconds=900,
    )
    assert body["washout_seconds"] == 900
    listing = client.get("/experiments").json()
    assert next(
        e for e in listing if e["experiment_id"] == body["experiment_id"]
    )["washout_seconds"] == 900

    default_body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=["s3", "s4"],
        seed=3,
    )
    assert default_body["washout_seconds"] == 0

    # 负数洗脱期被拒绝
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B"],
            "subject_ids": ["s5", "s6"],
            "seed": 1,
            "washout_seconds": -1,
        },
    )
    assert resp.status_code == 422


def test_complete_only_allowed_for_revealed_current_period(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=3
    )
    eid = body["experiment_id"]

    # 尚未揭盲任何周期
    early = complete_period(client, eid, "s1", 1, "early")
    assert early.status_code == 409
    detail = early.json()["detail"]
    assert detail["reason"] == "period_out_of_order"
    assert detail["current_period"] == 0

    first = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )
    assert first.status_code == 200

    # 只能登记当前已揭盲周期，不能提前 / 补登
    skip = complete_period(client, eid, "s1", 2, "skip")
    assert skip.status_code == 409
    assert skip.json()["detail"]["reason"] == "period_out_of_order"
    assert skip.json()["detail"]["current_period"] == 1

    ok = complete_period(client, eid, "s1", 1, "c1")
    assert ok.status_code == 200
    completed_body = ok.json()
    assert completed_body["period"] == 1
    assert completed_body["request_id"] == "c1"
    assert "completed_at" in completed_body

    # 同一周期重复登记（换 request_id）→ 409，completed_at 不被改写
    again = complete_period(client, eid, "s1", 1, "c1-again")
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "period_already_completed"
    assert again.json()["detail"]["current_period"] == 1


def test_complete_request_id_is_idempotent_and_replays(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=9
    )
    eid = body["experiment_id"]
    client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )

    payload = {"subject_id": "s1", "period": 1, "request_id": "done-1"}
    first = client.post(f"/experiments/{eid}/complete", json=payload)
    assert first.status_code == 200
    for _ in range(3):
        replay = client.post(f"/experiments/{eid}/complete", json=payload)
        assert replay.status_code == 200
        assert replay.json() == first.json()

    # 同一完成幂等键绑定不同周期 / 受试者 → 409
    other_period = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s1", "period": 2, "request_id": "done-1"},
    )
    assert other_period.status_code == 409
    assert other_period.json()["detail"]["reason"] == "request_id_conflict"
    other_subject = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s2", "period": 1, "request_id": "done-1"},
    )
    assert other_subject.status_code == 409

    # 推进与完成的 request_id 命名空间互不影响
    second_advance = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
    )
    assert second_advance.status_code == 200


def test_advance_blocked_until_prior_period_completed(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=4
    )
    eid = body["experiment_id"]
    client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )

    # washout=0 但上一周期尚未完成 → 409，且不泄露下一周期条件
    blocked = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
    )
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["reason"] == "prior_period_not_completed"
    assert "condition" not in detail
    assert "next_period" not in detail

    # 门禁失败不消耗幂等键、不推进：登记完成后原 request_id 首次成功
    done = complete_period(client, eid, "s1", 1, "c1")
    assert done.status_code == 200
    ok = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
    )
    assert ok.status_code == 200
    assert ok.json()["period"] == 2


def test_washout_gate_uses_server_time_and_returns_retry_after(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "washout.db"))
    t0 = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

    class Clock:
        def __init__(self) -> None:
            self.now = t0

        def __call__(self) -> datetime:
            return self.now

    clock = Clock()
    monkeypatch.setattr(db, "utc_now", clock)

    with TestClient(app) as local:
        created = local.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 1,
                "washout_seconds": 3600,
            },
        ).json()
        eid = created["experiment_id"]

        first = local.post(
            f"/experiments/{eid}/advance",
            json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
        )
        assert first.status_code == 200

        clock.now += timedelta(seconds=120)
        done = local.post(
            f"/experiments/{eid}/complete",
            json={"subject_id": "s1", "period": 1, "request_id": "c1"},
        )
        assert done.status_code == 200
        completed_at = datetime.fromisoformat(done.json()["completed_at"])

        # 进度只给出下一周期最早可推进时间，不含下一周期条件
        progress = local.get(f"/experiments/{eid}/subjects/s1/progress").json()
        assert progress["washout_seconds"] == 3600
        assert progress["next_period"] == 2
        assert progress["next_available_at"] == (
            completed_at + timedelta(seconds=3600)
        ).isoformat()
        assert progress["reveals"][0]["completed_at"] == done.json()["completed_at"]
        assert_no_sequence_leak(progress)

        advance_payload = {
            "subject_id": "s1",
            "expected_period": 2,
            "request_id": "a2",
        }

        # 刚完成：洗脱期还差 3600 秒
        blocked = local.post(f"/experiments/{eid}/advance", json=advance_payload)
        assert blocked.status_code == 409
        detail = blocked.json()["detail"]
        assert detail["reason"] == "washout_not_elapsed"
        assert detail["retry_after_seconds"] == 3600
        assert "condition" not in detail
        assert "next_period" not in detail

        # 差 1 秒到期
        clock.now = completed_at + timedelta(seconds=3599)
        blocked = local.post(f"/experiments/{eid}/advance", json=advance_payload)
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["retry_after_seconds"] == 1

        # 到期瞬间放行；门禁期间被拒的幂等键仍可首次成功
        clock.now = completed_at + timedelta(seconds=3600)
        ok = local.post(f"/experiments/{eid}/advance", json=advance_payload)
        assert ok.status_code == 200
        assert ok.json()["period"] == 2

        progress = local.get(f"/experiments/{eid}/subjects/s1/progress").json()
        assert progress["next_available_at"] is None


def test_final_period_can_be_completed(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=8
    )
    eid = body["experiment_id"]
    reveal_sequence(client, eid, "s1", 2)

    final = complete_period(client, eid, "s1", 2, "c2")
    assert final.status_code == 200

    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["completed"] is True
    assert progress["next_period"] == 3
    assert progress["next_available_at"] is None
    assert [r["period"] for r in progress["reveals"]] == [1, 2]
    assert all(r["completed_at"] is not None for r in progress["reveals"])


def test_complete_unknown_experiment_or_subject_returns_404(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=6
    )
    eid = body["experiment_id"]
    payload = {"subject_id": "ghost", "period": 1, "request_id": "x"}

    assert client.post(
        "/experiments/missing/complete", json=payload
    ).status_code == 404
    assert client.post(
        f"/experiments/{eid}/complete", json=payload
    ).status_code == 404


def test_invalid_complete_payload_rejected(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=2
    )
    eid = body["experiment_id"]

    resp = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s1", "period": 0, "request_id": "r"},
    )
    assert resp.status_code == 422
    resp = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s1", "period": 1, "request_id": ""},
    )
    assert resp.status_code == 422


def test_concurrent_complete_has_single_winner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-complete.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 12,
            },
        ).json()
        setup.post(
            f"/experiments/{created['experiment_id']}/advance",
            json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
        )
    eid = created["experiment_id"]

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/complete",
                json={"subject_id": "s1", "period": 1, "request_id": f"c-{i}"},
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1

    with TestClient(app) as verify:
        progress = verify.get(
            f"/experiments/{eid}/subjects/s1/progress"
        ).json()
    assert progress["next_period"] == 2
    assert progress["reveals"][0]["completed_at"] is not None
    # 完成只登记一次：完成后下一周期立刻可推进（washout=0）
    with TestClient(app) as verify:
        advanced = verify.post(
            f"/experiments/{eid}/advance",
            json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
        )
    assert advanced.status_code == 200


def test_concurrent_advance_after_completion_has_single_winner(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-advance.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 13,
            },
        ).json()
        eid = created["experiment_id"]
        setup.post(
            f"/experiments/{eid}/advance",
            json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
        )
        setup.post(
            f"/experiments/{eid}/complete",
            json={"subject_id": "s1", "period": 1, "request_id": "c1"},
        )

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/advance",
                json={
                    "subject_id": "s1",
                    "expected_period": 2,
                    "request_id": f"race2-{i}",
                },
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1
    with TestClient(app) as verify:
        progress = verify.get(
            f"/experiments/{eid}/subjects/s1/progress"
        ).json()
    assert progress["next_period"] == 3
    assert len(progress["reveals"]) == 2


# ---------- 批量受控揭盲 ----------

def advance_batch(
    client: TestClient,
    experiment_id: str,
    members: list[dict],
    batch_id: str,
):
    return client.post(
        f"/experiments/{experiment_id}/advance-batch",
        json={"batch_id": batch_id, "members": members},
    )


def test_batch_advances_all_members_in_input_order(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]

    members = [
        {"subject_id": "s2", "expected_period": 1},
        {"subject_id": "s1", "expected_period": 1},
    ]
    resp = advance_batch(client, eid, members, "batch-1")
    assert resp.status_code == 200, resp.text
    batch = resp.json()
    assert batch["experiment_id"] == eid
    assert batch["batch_id"] == "batch-1"
    assert [r["subject_id"] for r in batch["results"]] == ["s2", "s1"]
    for result, member in zip(batch["results"], members):
        assert result["period"] == 1
        assert result["condition"] in {"A", "B"}
        assert result["request_id"].startswith("batch:batch-1:")
        assert "revealed_at" in result

    # 进度按整批推进
    for subject_id in ("s1", "s2"):
        progress = client.get(f"/experiments/{eid}/subjects/{subject_id}/progress").json()
        assert progress["next_period"] == 2
        assert len(progress["reveals"]) == 1
        assert progress["reveals"][0]["completed_at"] is None


def test_batch_allows_adjacent_periods_of_same_subject_in_order(
    client: TestClient,
) -> None:
    # 同一受试者的相邻周期可放在同一批中：周期 1 揭盲后需先完成才能揭盲周期 2，
    # 因此只有在之前已登记周期 1 完成时整批才通过
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]
    first = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    ).json()
    assert complete_period(client, eid, "s1", 1, "c1").status_code == 200

    members = [
        {"subject_id": "s1", "expected_period": 2},
        {"subject_id": "s2", "expected_period": 1},
    ]
    resp = advance_batch(client, eid, members, "batch-multi")
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [r["subject_id"] for r in results] == ["s1", "s2"]
    assert [r["period"] for r in results] == [2, 1]
    # 顺序揭盲：s1 的周期 2 条件即其完整序列第二项，与历史一致
    assert results[0]["condition"] != first["condition"]


def test_batch_failure_is_atomic_and_lists_failures_without_conditions(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]
    # s1 先揭盲周期 1，但不登记完成
    assert client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    ).status_code == 200

    members = [
        # s2 周期 1：本应通过
        {"subject_id": "s2", "expected_period": 1},
        # s1 周期 2：上一周期未完成 → 拒绝
        {"subject_id": "s1", "expected_period": 2},
        # ghost：受试者不存在 → 拒绝
        {"subject_id": "ghost", "expected_period": 1},
    ]
    resp = advance_batch(client, eid, members, "batch-fail")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "batch_member_rejected"
    assert detail["batch_id"] == "batch-fail"
    failures = detail["failures"]
    assert {f["subject_id"]: f["reason"] for f in failures} == {
        "s1": "prior_period_not_completed",
        "ghost": "subject_not_found",
    }
    # 失败项保持输入顺序（s1 在 ghost 之前）
    assert [f["subject_id"] for f in failures] == ["s1", "ghost"]
    # 任何失败项都不得携带条件信息
    assert_no_sequence_leak(detail)

    # 整批零写入：s2 也未被揭盲
    progress = client.get(f"/experiments/{eid}/subjects/s2/progress").json()
    assert progress["next_period"] == 1
    assert progress["reveals"] == []
    # 失败不消耗 batch_id：修正成员后同 batch_id 首次成功
    ok = advance_batch(
        client,
        eid,
        [{"subject_id": "s2", "expected_period": 1}],
        "batch-fail",
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["results"][0]["subject_id"] == "s2"


def test_batch_reports_period_mismatch_and_experiment_complete(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=8
    )
    eid = body["experiment_id"]
    reveal_sequence(client, eid, "s1", 2)  # s1 已完成全部周期

    resp = advance_batch(
        client,
        eid,
        [
            {"subject_id": "s2", "expected_period": 2},   # 超前 → mismatch
            {"subject_id": "s1", "expected_period": 3},   # 已结束
        ],
        "batch-stale",
    )
    assert resp.status_code == 409
    reasons = {f["subject_id"]: f for f in resp.json()["detail"]["failures"]}
    assert reasons["s2"]["reason"] == "period_mismatch"
    assert reasons["s2"]["next_period"] == 1
    assert reasons["s1"]["reason"] == "experiment_complete"
    assert reasons["s1"]["next_period"] == 3
    assert_no_sequence_leak(resp.json())


def test_batch_washout_failure_returns_retry_after_and_zero_writes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "batch-washout.db"))
    t0 = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

    class Clock:
        def __init__(self) -> None:
            self.now = t0

        def __call__(self) -> datetime:
            return self.now

    clock = Clock()
    monkeypatch.setattr(db, "utc_now", clock)

    with TestClient(app) as local:
        created = local.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 1,
                "washout_seconds": 3600,
            },
        ).json()
        eid = created["experiment_id"]
        # s1 已揭盲周期 1，并在 2 分钟后登记完成（距洗脱期届满还差 3480 秒）
        local.post(
            f"/experiments/{eid}/advance",
            json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
        )
        clock.now += timedelta(seconds=120)
        assert local.post(
            f"/experiments/{eid}/complete",
            json={"subject_id": "s1", "period": 1, "request_id": "c1"},
        ).status_code == 200

        # 再走 240 秒：完成于 t0+120，门禁时刻 t0+360，距届满还差 3360 秒
        clock.now += timedelta(seconds=240)
        resp = advance_batch(
            local,
            eid,
            [
                {"subject_id": "s1", "expected_period": 2},
                {"subject_id": "s2", "expected_period": 1},
            ],
            "batch-w",
        )
        assert resp.status_code == 409
        failures = resp.json()["detail"]["failures"]
        assert len(failures) == 1
        assert failures[0]["subject_id"] == "s1"
        assert failures[0]["reason"] == "washout_not_elapsed"
        assert failures[0]["retry_after_seconds"] == 3360
        assert_no_sequence_leak(resp.json())

        # 零写入：s2 未揭盲、s1 仍停在周期 2
        progress_s2 = local.get(f"/experiments/{eid}/subjects/s2/progress").json()
        assert progress_s2["reveals"] == []

        # 到期后用同一 batch_id 首次成功（门禁失败不消耗幂等键）
        clock.now += timedelta(seconds=3360)
        ok = advance_batch(
            local,
            eid,
            [
                {"subject_id": "s1", "expected_period": 2},
                {"subject_id": "s2", "expected_period": 1},
            ],
            "batch-w",
        )
        assert ok.status_code == 200, ok.text
        assert [r["subject_id"] for r in ok.json()["results"]] == ["s1", "s2"]


def test_batch_same_batch_id_replays_first_result(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=9
    )
    eid = body["experiment_id"]
    members = [
        {"subject_id": "s1", "expected_period": 1},
        {"subject_id": "s2", "expected_period": 1},
    ]
    first = advance_batch(client, eid, members, "idem-batch")
    assert first.status_code == 200
    first_body = first.json()

    for _ in range(3):
        retry = advance_batch(client, eid, members, "idem-batch")
        assert retry.status_code == 200
        assert retry.json() == first_body

    # 重放不重复推进
    for subject_id in ("s1", "s2"):
        progress = client.get(f"/experiments/{eid}/subjects/{subject_id}/progress").json()
        assert progress["next_period"] == 2
        assert len(progress["reveals"]) == 1

    # 即使成员已推进到更后周期，重放仍返回首次结果，不重复揭盲
    assert complete_period(client, eid, "s1", 1, "c1").status_code == 200
    assert client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
    ).status_code == 200
    replay = advance_batch(client, eid, members, "idem-batch")
    assert replay.status_code == 200
    assert replay.json() == first_body


def test_batch_same_batch_id_different_members_returns_409(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=4
    )
    eid = body["experiment_id"]

    first = advance_batch(
        client,
        eid,
        [{"subject_id": "s1", "expected_period": 1}],
        "shared-batch",
    )
    assert first.status_code == 200

    # 不同成员集合
    other_members = advance_batch(
        client,
        eid,
        [{"subject_id": "s2", "expected_period": 1}],
        "shared-batch",
    )
    assert other_members.status_code == 409
    detail = other_members.json()["detail"]
    assert detail["reason"] == "batch_id_conflict"
    assert detail["batch_id"] == "shared-batch"

    # 相同成员但周期不同
    other_period = advance_batch(
        client,
        eid,
        [{"subject_id": "s1", "expected_period": 2}],
        "shared-batch",
    )
    assert other_period.status_code == 409
    assert other_period.json()["detail"]["reason"] == "batch_id_conflict"

    # 相同成员但顺序不同也视为不同请求（使用全新实验避免与上面的状态叠加）
    body2, _ = create_experiment(
        client, conditions=["A", "B", "C", "D"],
        subject_ids=[f"t{i}" for i in range(8)], seed=7,
    )
    eid2 = body2["experiment_id"]
    two = [
        {"subject_id": "t1", "expected_period": 1},
        {"subject_id": "t2", "expected_period": 1},
    ]
    assert advance_batch(client, eid2, two, "ordered-batch").status_code == 200
    reordered = advance_batch(
        client, eid2, list(reversed(two)), "ordered-batch"
    )
    assert reordered.status_code == 409
    assert reordered.json()["detail"]["reason"] == "batch_id_conflict"

    # 冲突的失败路径零写入：t1 / t2 只保留首批各一条揭盲，未重复推进
    for subject_id in ("t1", "t2"):
        progress = client.get(
            f"/experiments/{eid2}/subjects/{subject_id}/progress"
        ).json()
        assert progress["next_period"] == 2
        assert len(progress["reveals"]) == 1


def test_batch_members_validation(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=2
    )
    eid = body["experiment_id"]

    # 空成员列表
    resp = advance_batch(client, eid, [], "empty")
    assert resp.status_code == 422

    # 重复的 subject_id / expected_period 对
    resp = advance_batch(
        client,
        eid,
        [
            {"subject_id": "s1", "expected_period": 1},
            {"subject_id": "s1", "expected_period": 1},
        ],
        "dup",
    )
    assert resp.status_code == 422

    # expected_period < 1 / batch_id 为空
    assert advance_batch(
        client, eid, [{"subject_id": "s1", "expected_period": 0}], "bad"
    ).status_code == 422
    assert advance_batch(
        client, eid, [{"subject_id": "s1", "expected_period": 1}], ""
    ).status_code == 422

    # 超过 100 个成员（受试者名册只建了 2 人，校验在入库前发生）
    too_many = [
        {"subject_id": f"u{i}", "expected_period": 1} for i in range(101)
    ]
    assert advance_batch(client, eid, too_many, "too-many").status_code == 422


def test_batch_boundary_size_of_100_accepted(client: TestClient) -> None:
    # 100 个互异成员（2 条件一组 2 人，100 为整组倍数）
    subject_ids = [f"u{i}" for i in range(100)]
    body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=subject_ids,
        seed=1,
    )
    eid = body["experiment_id"]
    members = [{"subject_id": s, "expected_period": 1} for s in subject_ids]
    resp = advance_batch(client, eid, members, "batch-100")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["results"]) == 100
    assert [r["subject_id"] for r in resp.json()["results"]] == subject_ids


def test_batch_unknown_experiment_returns_404(client: TestClient) -> None:
    resp = advance_batch(
        client,
        "missing",
        [{"subject_id": "s1", "expected_period": 1}],
        "b",
    )
    assert resp.status_code == 404


def test_concurrent_batches_are_serialized(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-batch.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 21,
            },
        ).json()
    eid = created["experiment_id"]

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = advance_batch(
                local_client,
                eid,
                [{"subject_id": "s1", "expected_period": 1}],
                f"batch-race-{i}",
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # BEGIN IMMEDIATE 串行化：只有一批成功，其余看到 period_mismatch
    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1
    with TestClient(app) as verify:
        progress = verify.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


# ---------- 旧库启动时自动补齐表结构 ----------

def test_startup_migrates_legacy_database(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    # 模拟旧版本数据库：只有 experiments / assignments 两张表
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                conditions TEXT NOT NULL,
                seed INTEGER NOT NULL,
                group_size INTEGER NOT NULL,
                num_subjects INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                subject_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                sequence TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-1", '["A","B","C"]', 1, 6, 6, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-1", "s0", 0, '["A","C","B"]'),
        )

    # 应用启动（lifespan）自动补齐表结构，旧数据仍可查询与揭盲
    with TestClient(app) as migrated:
        detail = migrated.get("/experiments/legacy-1").json()
        assert detail["num_subjects"] == 6
        # 旧实验缺少 washout_seconds 列：迁移后按 0 秒处理
        assert detail["washout_seconds"] == 0
        assert_no_sequence_leak(detail)

        progress = migrated.get(
            "/experiments/legacy-1/subjects/s0/progress"
        ).json()
        assert progress["next_period"] == 1
        assert progress["num_periods"] == 3
        assert progress["washout_seconds"] == 0
        assert progress["next_available_at"] is None
        assert progress["reveals"] == []

        resp = migrated.post(
            "/experiments/legacy-1/advance",
            json={
                "subject_id": "s0",
                "expected_period": 1,
                "request_id": "boot-1",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["condition"] == "A"

        # 未完成上一周期不能推进；旧实验 washout=0，完成后立即放行
        blocked = migrated.post(
            "/experiments/legacy-1/advance",
            json={
                "subject_id": "s0",
                "expected_period": 2,
                "request_id": "boot-2",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["reason"] == "prior_period_not_completed"

        done = migrated.post(
            "/experiments/legacy-1/complete",
            json={"subject_id": "s0", "period": 1, "request_id": "done-1"},
        )
        assert done.status_code == 200

        second = migrated.post(
            "/experiments/legacy-1/advance",
            json={
                "subject_id": "s0",
                "expected_period": 2,
                "request_id": "boot-2",
            },
        )
        assert second.status_code == 200
        assert second.json()["condition"] == "C"
