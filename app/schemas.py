"""请求 / 响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# 实验状态：active 可正常揭盲；paused 暂停一切新的揭盲（幂等成功重放除外）
ExperimentStatus = Literal["active", "paused"]
# 状态变更操作类型
StateOperation = Literal["pause", "resume"]


class ExperimentCreate(BaseModel):
    """创建交叉实验的请求体。"""

    conditions: list[str] = Field(
        ..., min_length=2, description="互异的实验条件，至少 2 个"
    )
    subject_ids: list[str] = Field(
        ..., min_length=1, description="互异受试者 ID"
    )
    seed: int = Field(..., description="随机种子，相同输入与种子产生相同分配")
    washout_seconds: int = Field(
        default=0,
        ge=0,
        description=(
            "相邻周期间的洗脱期秒数（非负）。上一周期完成后必须再经过该时长"
            "才能推进下一周期；默认 0 表示只需登记完成即可推进"
        ),
    )


class AssignmentOut(BaseModel):
    """受试者名册条目；不含条件序列，避免提前揭盲。"""

    subject_id: str


class ExperimentOut(BaseModel):
    experiment_id: str
    conditions: list[str]
    seed: int
    group_size: int
    num_subjects: int
    num_periods: int
    washout_seconds: int
    created_at: str
    assignments: list[AssignmentOut]


class ExperimentSummary(BaseModel):
    experiment_id: str
    conditions: list[str]
    seed: int
    group_size: int
    num_subjects: int
    num_periods: int
    washout_seconds: int
    created_at: str


class AdvanceRequest(BaseModel):
    """按周期受控揭盲的推进请求。"""

    subject_id: str = Field(..., min_length=1, description="受试者 ID")
    expected_period: int = Field(
        ...,
        ge=1,
        description="调用方认为该受试者所处的下一周期（从 1 开始）",
    )
    request_id: str = Field(
        ..., min_length=1, description="幂等键，相同 request_id 重试不重复推进"
    )


class AdvanceOut(BaseModel):
    experiment_id: str
    subject_id: str
    period: int
    condition: str
    request_id: str
    revealed_at: str


class AdvanceBatchMember(BaseModel):
    """批量揭盲中的一个成员：受试者与其预期下一周期。"""

    subject_id: str = Field(..., min_length=1, description="受试者 ID")
    expected_period: int = Field(
        ...,
        ge=1,
        description="调用方认为该受试者所处的下一周期（从 1 开始）",
    )


class AdvanceBatchRequest(BaseModel):
    """批量受控揭盲请求：1～100 个互异的 subject_id / expected_period 对。"""

    batch_id: str = Field(
        ...,
        min_length=1,
        description="批量幂等键，相同 batch_id 与相同成员列表重试返回首次结果",
    )
    members: list[AdvanceBatchMember] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="批量成员（1～100 个），按该列表顺序揭盲与推进",
    )

    @model_validator(mode="after")
    def _members_must_be_distinct(self) -> "AdvanceBatchRequest":
        seen: set[tuple[str, int]] = set()
        for member in self.members:
            key = (member.subject_id, member.expected_period)
            if key in seen:
                raise ValueError(
                    "members 必须互异：存在重复的 subject_id / expected_period"
                )
            seen.add(key)
        return self


class AdvanceBatchItemOut(AdvanceOut):
    """批量揭盲中单个成员的揭盲结果。"""


class AdvanceBatchOut(BaseModel):
    experiment_id: str
    batch_id: str
    # 结果严格按请求 members 的输入顺序返回
    results: list[AdvanceBatchItemOut]


class CompleteRequest(BaseModel):
    """周期完成登记请求。"""

    subject_id: str = Field(..., min_length=1, description="受试者 ID")
    period: int = Field(
        ...,
        ge=1,
        description="要登记完成的周期（必须是已揭盲且尚未完成的当前周期）",
    )
    request_id: str = Field(
        ..., min_length=1, description="幂等键，相同 request_id 重试返回原结果"
    )


class CompleteOut(BaseModel):
    experiment_id: str
    subject_id: str
    period: int
    request_id: str
    completed_at: str


class RevealOut(BaseModel):
    """一条已揭盲记录；completed_at 为该周期完成登记时间（未登记为 null）。"""

    period: int
    condition: str
    revealed_at: str
    completed_at: str | None = None


class ProgressOut(BaseModel):
    """受试者进度：只含已揭盲记录、下一周期与下一周期可推进时间。"""

    experiment_id: str
    subject_id: str
    next_period: int
    num_periods: int
    washout_seconds: int
    completed: bool
    next_available_at: str | None = None
    reveals: list[RevealOut]


class PauseRequest(BaseModel):
    """实验暂停请求。"""

    operation_id: str = Field(
        ...,
        min_length=1,
        description="调用方生成的唯一操作键（如 UUID），相同 operation_id 重放返回首次结果",
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="暂停原因（必填，去首尾空白后非空，最长 500 字），写入审计记录",
    )

    @model_validator(mode="after")
    def _reason_must_not_be_blank(self) -> "PauseRequest":
        if not self.reason.strip():
            raise ValueError("reason 不能为空白字符串")
        return self


class ResumeRequest(BaseModel):
    """实验恢复请求：携带调用方持有的状态版本做原子乐观校验。"""

    operation_id: str = Field(
        ...,
        min_length=1,
        description="调用方生成的唯一操作键（如 UUID），相同 operation_id 重放返回首次结果",
    )
    expected_version: int = Field(
        ...,
        ge=1,
        description="调用方认为当前（暂停）状态版本；与服务端不一致时返回 409 且不改变状态",
    )


class ExperimentStateOut(BaseModel):
    """暂停 / 恢复的状态变更结果（也是幂等重放的首次结果）。"""

    experiment_id: str
    status: ExperimentStatus
    # 每次成功的暂停 / 恢复状态转换递增 1；初始活动状态版本为 1
    version: int
    operation_id: str
    reason: str | None = None
    changed_at: str


class StateEventOut(BaseModel):
    """一条状态变更审计记录（暂停携带原因，恢复原因为 null）。"""

    seq: int
    experiment_id: str
    operation: StateOperation
    status: ExperimentStatus
    version: int
    operation_id: str
    reason: str | None = None
    changed_at: str

