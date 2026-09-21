"""交叉实验顺序平衡 API。

使用 Williams 平衡拉丁方为受试者分配条件顺序，保证：
* 每个条件在每个位置上出现次数相等；
* 每个有向相邻条件对出现次数相等。

完整条件序列仅保存在服务端，通过推进接口按周期受控揭盲：
创建与详情接口只返回受试者名册，不暴露任何后续周期安排。
"""

from __future__ import annotations

import random
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from . import db
from .schemas import (
    AdvanceBatchOut,
    AdvanceBatchRequest,
    AdvanceOut,
    AdvanceRequest,
    CompleteOut,
    CompleteRequest,
    ExperimentCreate,
    ExperimentOut,
    ExperimentSummary,
    PauseRequest,
    ProgressOut,
    ResumeRequest,
    StateEventOut,
    StateEventsOut,
)
from .williams import group_size, williams_sequences


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器 / 进程启动时建表（旧库自动补齐新增表）
    db.init_db()
    yield


app = FastAPI(
    title="Crossover Experiment Order Balancer",
    description="基于 Williams 平衡拉丁方的交叉实验顺序分配与按周期受控揭盲服务",
    version="1.4.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _state_conflict_detail(exc: db.InvalidStateTransitionError) -> dict:
    """构造暂停 / 恢复状态冲突的 409 响应体（不含任何揭盲信息）。"""
    messages = {
        "already_paused": "实验已处于暂停状态，无需重复暂停",
        "already_active": "实验当前正在运行中，无需恢复",
        "state_version_mismatch": (
            "期望状态版本与服务端当前版本不一致（可能已被其他操作者变更）"
        ),
    }
    detail = {
        "message": messages[exc.reason],
        "reason": exc.reason,
        "current_status": exc.current_status,
        "state_version": exc.state_version,
    }
    if exc.expected_version is not None:
        detail["expected_version"] = exc.expected_version
    return detail


@app.post(
    "/experiments",
    status_code=201,
    response_model=ExperimentOut,
    summary="创建实验并生成分配结果（不返回条件序列）",
)
def create_experiment(payload: ExperimentCreate) -> dict:
    conditions = payload.conditions
    subject_ids = payload.subject_ids

    if any(not c.strip() for c in conditions):
        raise HTTPException(
            status_code=422, detail="conditions 不能为空字符串"
        )
    if len(set(conditions)) != len(conditions):
        raise HTTPException(
            status_code=422, detail="conditions 必须互异（存在重复条件）"
        )
    if any(not s.strip() for s in subject_ids):
        raise HTTPException(
            status_code=422, detail="subject_ids 不能为空字符串"
        )
    if len(set(subject_ids)) != len(subject_ids):
        raise HTTPException(
            status_code=422, detail="subject_ids 必须互异（存在重复受试者）"
        )

    n = len(conditions)
    size = group_size(n)
    count = len(subject_ids)

    if count % size != 0:
        lower = (count // size) * size
        upper = lower + size
        nearest = [lower, upper] if lower > 0 else [upper]
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"受试者数量 {count} 无法整分为完整的序列组"
                    f"（每组 {size} 条序列）"
                ),
                "subject_count": count,
                "group_size": size,
                "nearest_acceptable": nearest,
            },
        )

    # Williams 平衡序列（下标形式），再映射为实际条件。
    # 完整序列写入服务端存储，不在响应中返回。
    index_sequences = williams_sequences(n)
    condition_sequences = [
        [conditions[i] for i in seq] for seq in index_sequences
    ]

    # 用种子确定性地打乱“序列池”，再依次分配给受试者。
    # 池中每条序列恰好出现 count // size 次，保证整组完整。
    rng = random.Random(payload.seed)
    pool = list(range(size)) * (count // size)
    rng.shuffle(pool)

    assignments = [
        {
            "subject_id": subject_id,
            "sequence_index": pool[i],
            "sequence": condition_sequences[pool[i]],
        }
        for i, subject_id in enumerate(subject_ids)
    ]

    experiment_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    db.insert_experiment(
        experiment_id,
        conditions,
        payload.seed,
        size,
        created_at,
        assignments,
        washout_seconds=payload.washout_seconds,
    )

    return {
        "experiment_id": experiment_id,
        "conditions": conditions,
        "seed": payload.seed,
        "group_size": size,
        "num_subjects": count,
        "num_periods": n,
        "washout_seconds": payload.washout_seconds,
        "status": "active",
        "state_version": 0,
        "created_at": created_at,
        "assignments": [{"subject_id": s} for s in subject_ids],
    }


@app.get(
    "/experiments",
    response_model=list[ExperimentSummary],
    summary="列出全部实验",
)
def list_experiments() -> list[dict]:
    return db.list_experiments()


@app.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentOut,
    summary="查询实验（只返回受试者名册，不含条件序列）",
)
def get_experiment(experiment_id: str) -> dict:
    experiment = db.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment


@app.post(
    "/experiments/{experiment_id}/advance",
    response_model=AdvanceOut,
    summary=(
        "按周期受控揭盲：上一周期已完成且洗脱期届满时，"
        "仅当 expected_period 为下一周期才返回当前条件并原子推进"
    ),
)
def advance_period(experiment_id: str, payload: AdvanceRequest) -> dict:
    try:
        result = db.advance_period(
            experiment_id,
            payload.subject_id,
            payload.expected_period,
            payload.request_id,
        )
    except db.RequestIdConflictError:
        # 同一幂等键却提交了不同的受试者或周期
        raise HTTPException(
            status_code=409,
            detail={
                "message": "request_id 已被其他请求占用",
                "reason": "request_id_conflict",
                "request_id": payload.request_id,
            },
        )
    except db.StalePeriodError as exc:
        # 并发抢先推进或过期周期：不揭露任何条件
        raise HTTPException(
            status_code=409,
            detail={
                "message": "expected_period 与该受试者的下一周期不一致"
                if not exc.complete
                else "该受试者已完成全部周期",
                "reason": "experiment_complete"
                if exc.complete
                else "period_mismatch",
                "next_period": exc.next_period,
            },
        )
    except db.PriorPeriodNotCompletedError:
        # 上一周期尚无完成记录：只说明“未就绪”，不揭露下一周期条件
        raise HTTPException(
            status_code=409,
            detail={
                "message": "上一周期尚未登记完成，不能推进下一周期",
                "reason": "prior_period_not_completed",
            },
        )
    except db.WashoutNotElapsedError as exc:
        # 洗脱期未届满：给出服务端计算的重试间隔，不附带任何周期 / 条件信息
        raise HTTPException(
            status_code=409,
            detail={
                "message": "洗脱期尚未届满，下一周期暂不可推进",
                "reason": "washout_not_elapsed",
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )
    except db.ExperimentPausedError:
        # 实验已暂停：拒绝揭盲，响应不泄露任何条件 / 后续安排
        raise HTTPException(
            status_code=409,
            detail={
                "message": "实验已暂停，暂停期间不能揭盲",
                "reason": "experiment_paused",
            },
        )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "实验或受试者不存在",
                "experiment_id": experiment_id,
                "subject_id": payload.subject_id,
            },
        )
    return result


@app.post(
    "/experiments/{experiment_id}/advance-batch",
    response_model=AdvanceBatchOut,
    summary=(
        "批量受控揭盲：单个写事务内校验全部成员的完成登记与洗脱期门禁，"
        "全部通过才按输入顺序揭盲并推进；任一项失败整批零写入并返回 409"
    ),
)
def advance_batch(experiment_id: str, payload: AdvanceBatchRequest) -> dict:
    try:
        result = db.advance_batch(
            experiment_id,
            payload.batch_id,
            [m.model_dump() for m in payload.members],
        )
    except db.BatchIdConflictError:
        # 同一 batch_id 绑定了不同的成员或周期
        raise HTTPException(
            status_code=409,
            detail={
                "message": "batch_id 已被成员或周期不同的其他请求占用",
                "reason": "batch_id_conflict",
                "batch_id": payload.batch_id,
            },
        )
    except db.BatchMembersRejectedError as exc:
        # 整批门禁未通过：只列各失败项的 subject_id 与原因，不含任何条件
        raise HTTPException(
            status_code=409,
            detail={
                "message": "批量中存在未通过门禁的成员，整批未揭盲",
                "reason": "batch_member_rejected",
                "batch_id": payload.batch_id,
                "failures": exc.failures,
            },
        )
    except db.ExperimentPausedError:
        # 实验已暂停：整批拒绝，响应不泄露任何条件 / 后续安排
        raise HTTPException(
            status_code=409,
            detail={
                "message": "实验已暂停，暂停期间不能揭盲",
                "reason": "experiment_paused",
                "batch_id": payload.batch_id,
            },
        )

    if result is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return result


@app.post(
    "/experiments/{experiment_id}/complete",
    response_model=CompleteOut,
    summary=(
        "登记当前周期完成时间：只有已揭盲且尚未完成的当前周期可登记，"
        "相同 request_id 重放原结果"
    ),
)
def complete_period(experiment_id: str, payload: CompleteRequest) -> dict:
    try:
        result = db.complete_period(
            experiment_id,
            payload.subject_id,
            payload.period,
            payload.request_id,
        )
    except db.RequestIdConflictError:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "request_id 已被其他请求占用",
                "reason": "request_id_conflict",
                "request_id": payload.request_id,
            },
        )
    except db.CompletionOrderError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "该周期已登记完成"
                    if exc.already_completed
                    else "只能登记已揭盲且尚未完成的当前周期"
                ),
                "reason": (
                    "period_already_completed"
                    if exc.already_completed
                    else "period_out_of_order"
                ),
                "current_period": exc.current_period,
            },
        )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "实验或受试者不存在",
                "experiment_id": experiment_id,
                "subject_id": payload.subject_id,
            },
        )
    return result


@app.get(
    "/experiments/{experiment_id}/subjects/{subject_id}/progress",
    response_model=ProgressOut,
    summary="查询受试者揭盲进度（已揭盲 / 已完成记录、next_period 与 next_available_at）",
)
def get_progress(experiment_id: str, subject_id: str) -> dict:
    progress = db.get_progress(experiment_id, subject_id)
    if progress is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "实验或受试者不存在",
                "experiment_id": experiment_id,
                "subject_id": subject_id,
            },
        )
    return progress


@app.post(
    "/experiments/{experiment_id}/pause",
    response_model=StateEventOut,
    status_code=200,
    summary=(
        "暂停实验：携带唯一 operation_id 与原因，原子置为 paused 并递增状态版本；"
        "暂停期间单条与批量揭盲均返回 409"
    ),
)
def pause_experiment(experiment_id: str, payload: PauseRequest) -> dict:
    try:
        event = db.pause_experiment(
            experiment_id, payload.operation_id, payload.reason
        )
    except db.OperationIdConflictError:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "operation_id 已被其他状态变更操作占用",
                "reason": "operation_id_conflict",
                "operation_id": payload.operation_id,
            },
        )
    except db.InvalidStateTransitionError as exc:
        raise HTTPException(
            status_code=409, detail=_state_conflict_detail(exc)
        )
    if event is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return event


@app.post(
    "/experiments/{experiment_id}/resume",
    response_model=StateEventOut,
    status_code=200,
    summary=(
        "恢复实验：携带期望状态版本，在单个写事务内 compare-and-set 原子恢复，"
        "版本过期或状态不符返回 409 并回传当前版本"
    ),
)
def resume_experiment(experiment_id: str, payload: ResumeRequest) -> dict:
    try:
        event = db.resume_experiment(
            experiment_id,
            payload.expected_version,
            payload.operation_id,
            payload.reason,
        )
    except db.OperationIdConflictError:
        if payload.operation_id is None:
            # 理论不可达：无 operation_id 不会触发唯一冲突，防御性处理
            raise HTTPException(
                status_code=409,
                detail={"message": "状态变更冲突", "reason": "operation_id_conflict"},
            )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "operation_id 已被其他状态变更操作占用",
                "reason": "operation_id_conflict",
                "operation_id": payload.operation_id,
            },
        )
    except db.InvalidStateTransitionError as exc:
        raise HTTPException(
            status_code=409, detail=_state_conflict_detail(exc)
        )
    if event is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return event


@app.get(
    "/experiments/{experiment_id}/state-events",
    response_model=StateEventsOut,
    summary="查询实验状态变更审计记录（暂停 / 恢复），按时间倒序分页",
)
def list_state_events(
    experiment_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    events = db.list_state_events(experiment_id, limit=limit, offset=offset)
    if events is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return {"experiment_id": experiment_id, "events": events}
