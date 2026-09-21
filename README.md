# 交叉实验顺序平衡 API

基于 **Python + FastAPI + SQLite** 的交叉（crossover）实验顺序分配与**按周期受控揭盲**服务。
创建实验时提交互异条件、受试者 ID 与随机种子，服务生成 **Williams 平衡拉丁方**序列
并映射给受试者，保证：

- 每个条件在每个位置上出现次数相等；
- 每个**有向相邻条件对**出现次数相等（平衡一阶残留效应）；
- 完整序列组：偶数条件含 `n` 条序列，奇数条件含 `2n` 条序列；
- 受试者数量必须是完整组大小的整数倍，否则返回 `422` 及最近可接受数量；
- 相同输入与相同种子产生完全相同的分配结果（确定性分配）。

## 受控揭盲（period-controlled unblinding）

完整条件序列**只保存在服务端**，避免受试者提前获知后续安排：

- 创建接口 `POST /experiments` 与详情接口 `GET /experiments/{id}` 只返回受试者名册
  （`assignments[].subject_id`）与条件清单，不包含任何人的条件序列或序列下标；
- 通过推进接口逐周期揭盲：提交 `subject_id`、`expected_period` 与 `request_id`，
  **仅当 `expected_period` 正好等于该受试者的下一周期**时返回当前周期条件并原子推进；
- **周期完成 + 洗脱期门禁**：每个周期揭盲后必须调用完成接口
  `POST /experiments/{id}/complete` 登记完成时间；推进下一周期（周期 ≥ 2）前，
  服务端校验上一周期已存在完成记录，且服务端当前时间已达到
  `completed_at + washout_seconds`，缺一不可；
- 相同 `request_id` 重试（接口超时、网络重试等）返回**首次结果**且不重复写入；
  同一 `request_id` 绑定不同受试者或周期返回 `409`（推进与完成使用各自独立的幂等空间）；
- **批量受控揭盲** `POST /experiments/{id}/advance-batch`：一次提交 `batch_id` 与
  1～100 个互异的 `subject_id / expected_period` 成员，服务在**单个 SQLite 写事务**内
  先校验全部成员均满足完成登记与洗脱期门禁，**全部通过才按输入顺序**揭盲并推进；
  任一项失败即返回 `409`，列出每个失败项的 `subject_id` 与原因（不含任何条件），
  且**整批零写入**。相同 `batch_id` 与相同成员列表（含顺序）重试返回首次结果，
  绑定不同成员或周期则返回 `409`；
- 并发请求或过期周期（落后 / 超前 / 实验已结束）返回 `409`，冲突路径不写入、不多揭露任何条件；
- 进度查询只返回**已揭盲记录（含各自的 `completed_at`）**、`next_period` 与
  `next_available_at`（下一周期最早可推进时间），不暴露后续周期安排。

## 快速启动（Docker Compose，一条命令）

```bash
docker compose up --build
```

该命令会构建镜像并启动容器：构建时安装依赖，容器启动命令中再次校验依赖安装，
应用启动钩子（lifespan）自动完成 SQLite 建表与迁移。**旧版本数据库无需手工迁移**：
启动时新增的进度、揭盲、周期完成、单条幂等、批量揭盲表通过 `CREATE TABLE IF NOT EXISTS`
自动补齐，`experiments` 表新增的 `washout_seconds` 列通过幂等的
`ALTER TABLE ... DEFAULT 0` 补齐——**旧实验一律按 0 秒洗脱期处理**，旧数据保留且可继续揭盲与登记完成。

- **访问地址**：http://localhost:8000
- **交互式 API 文档（Swagger UI）**：http://localhost:8000/docs
- **端口**：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 的 `ports` 中修改）

## 必需配置

无强制配置，开箱即用。可选环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 容器内 `/app/data/experiments.db`（compose 已设置）；本地运行默认 `./experiments.db` | SQLite 数据库文件路径，目录不存在时自动创建 |

compose 中通过命名卷 `crossover-data` 持久化数据，删除容器数据不丢失。

## API 说明

### 创建实验 `POST /experiments`

```bash
curl -X POST http://localhost:8000/experiments \
  -H 'Content-Type: application/json' \
  -d '{
        "conditions": ["A", "B", "C"],
        "subject_ids": ["s1", "s2", "s3", "s4", "s5", "s6"],
        "seed": 42,
        "washout_seconds": 86400
      }'
```

- `conditions`：互异条件，至少 2 个；
- `subject_ids`：互异受试者 ID；数量必须是完整组大小（3 个条件时为 6）的整数倍；
- `seed`：随机种子（整数）。相同输入与种子 → 相同分配；
- `washout_seconds`（可选）：相邻周期间的**洗脱期秒数**，非负整数，默认 `0`。
  上一周期完成（`completed_at`）后必须再经过该时长，服务端才允许推进下一周期。

成功返回 `201`。响应中 `assignments` **仅为受试者名册**，完整条件序列由服务端持有：

```json
{
  "experiment_id": "…",
  "conditions": ["A", "B", "C"],
  "seed": 42,
  "group_size": 6,
  "num_subjects": 6,
  "num_periods": 3,
  "washout_seconds": 86400,
  "created_at": "…",
  "assignments": [
    {"subject_id": "s1"},
    {"subject_id": "s2"}
  ]
}
```

受试者数量不成整组时返回 `422`，并给出最近可接受数量：

```json
{
  "detail": {
    "message": "受试者数量 7 无法整分为完整的序列组（每组 6 条序列）",
    "subject_count": 7,
    "group_size": 6,
    "nearest_acceptable": [6, 12]
  }
}
```

### 推进揭盲 `POST /experiments/{experiment_id}/advance`

进入下一周期前调用，请求体：

| 字段 | 说明 |
| --- | --- |
| `subject_id` | 受试者 ID |
| `expected_period` | 调用方认为该受试者当前所处的下一周期（**从 1 开始**） |
| `request_id` | 调用方生成的幂等键（如 UUID），同一请求重试必须复用 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/advance \
  -H 'Content-Type: application/json' \
  -d '{"subject_id": "s1", "expected_period": 1, "request_id": "9f1c…"}'
```

仅当 `expected_period` 等于该受试者下一周期、**上一周期已登记完成且洗脱期已届满**
时返回 `200`，并把进度原子推进到下一周期：

```json
{
  "experiment_id": "…",
  "subject_id": "s1",
  "period": 1,
  "condition": "A",
  "request_id": "9f1c…",
  "revealed_at": "2026-09-21T08:00:00.000000+00:00"
}
```

冲突一律返回 `409`，响应体 `detail.reason` 区分原因，且**不揭露任何条件 / 后续安排**：

- `period_mismatch`：`expected_period` 落后或超前（并发抢先、重复提交、跳周期），
  同时回传服务端当前的 `next_period`；
- `experiment_complete`：该受试者已完成全部周期；
- `request_id_conflict`：相同 `request_id` 曾用于不同的 `subject_id` / `expected_period`；
- `prior_period_not_completed`：上一周期尚无完成记录（`washout_seconds=0` 时也必须先登记完成）；
- `washout_not_elapsed`：洗脱期尚未届满，回传
  `retry_after_seconds`（建议等待的整秒数）；响应不携带 `next_period` 或任何条件信息。

门禁判断以**服务端时间**为准并在写事务内完成，因此并发完成 / 推进串行化：
要么完成先落盘后放行，要么推进看到“未完成 / 未到期”而 `409`，不会出现绕过门禁的揭盲。
门禁失败（409）不写入幂等记录、不消耗 `request_id`，到期后用同一 `request_id` 重试即可首次成功。

幂等语义：相同 `(实验, request_id)` 且相同请求体重试，返回**首次的条件与 `revealed_at`**，
即使此时受试者已经推进到更后面的周期，也不会重复推进或改写历史。

### 批量受控揭盲 `POST /experiments/{experiment_id}/advance-batch`

需要为多名受试者同时揭盲当前周期时调用，请求体：

| 字段 | 说明 |
| --- | --- |
| `batch_id` | 批量幂等键（如 UUID），同一批请求重试必须复用 |
| `members` | 1～100 个成员，每个含 `subject_id` 与 `expected_period`；成员对必须互异，揭盲严格按该列表顺序执行 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/advance-batch \
  -H 'Content-Type: application/json' \
  -d '{
        "batch_id": "b7c1…",
        "members": [
          {"subject_id": "s1", "expected_period": 1},
          {"subject_id": "s2", "expected_period": 1}
        ]
      }'
```

服务在**单个 SQLite 写事务（`BEGIN IMMEDIATE`）**内先对**全部**成员逐一执行与单条
推进完全相同的门禁校验（受试者存在、周期匹配 / 实验未结束、上一周期完成登记、
洗脱期届满）。任一项失败即返回 `409` 并**整批回滚、零写入**——即使排在前面的成员
本可通过，也不会被揭盲：

```json
{
  "detail": {
    "message": "批量中存在未通过门禁的成员，整批未揭盲",
    "reason": "batch_member_rejected",
    "batch_id": "b7c1…",
    "failures": [
      {"subject_id": "s1", "reason": "prior_period_not_completed"},
      {"subject_id": "s8", "reason": "period_mismatch", "next_period": 2},
      {"subject_id": "s9", "reason": "experiment_complete", "next_period": 4},
      {"subject_id": "s10", "reason": "washout_not_elapsed", "retry_after_seconds": 3600}
    ]
  }
}
```

- `failures[]` 只含失败成员的 `subject_id` 与 `reason`（周期类原因附 `next_period`，
  洗脱期原因附 `retry_after_seconds`），**不含任何条件或后续安排**；受试者不存在为
  `subject_not_found`（实验本身不存在仍返回 `404`）；
- 门禁失败不写入 `advance_batches`、不消耗 `batch_id`，到期 / 修正后用**同一个
  `batch_id`** 重试即首次成功；
- 全部通过时按**输入顺序**揭盲并推进，返回 `200`：

```json
{
  "experiment_id": "…",
  "batch_id": "b7c1…",
  "results": [
    {"experiment_id": "…", "subject_id": "s1", "period": 1, "condition": "A",
     "request_id": "batch:b7c1…:s1:1", "revealed_at": "…"},
    {"experiment_id": "…", "subject_id": "s2", "period": 1, "condition": "B",
     "request_id": "batch:b7c1…:s2:1", "revealed_at": "…"}
  ]
}
```

批量幂等语义：相同 `(实验, batch_id)` 与**完全相同的成员列表（含顺序）**重试，
返回首次的 `results`（含首次的条件与 `revealed_at`），不重复推进；同一 `batch_id`
绑定不同成员、不同周期或不同顺序均返回 `409`（`batch_id_conflict`）。
`batch_id` 与单条推进的 `request_id` 使用各自独立的幂等空间，互不影响。
成员数量不在 1～100、成员对重复或字段非法返回 `422`。

### 登记周期完成 `POST /experiments/{experiment_id}/complete`

每个周期揭盲结束后调用，服务端以自身当前时间登记完成时刻：

| 字段 | 说明 |
| --- | --- |
| `subject_id` | 受试者 ID |
| `period` | 要完成的周期，必须是**已揭盲且尚未完成的当前周期**（从 1 开始） |
| `request_id` | 幂等键（如 UUID），同一请求重试必须复用 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/complete \
  -H 'Content-Type: application/json' \
  -d '{"subject_id": "s1", "period": 1, "request_id": "7d2a…"}'
```

成功返回 `200`：

```json
{
  "experiment_id": "…",
  "subject_id": "s1",
  "period": 1,
  "request_id": "7d2a…",
  "completed_at": "2026-09-21T10:00:00.000000+00:00"
}
```

- 相同 `(实验, request_id)` 重试返回**首次的 `completed_at`**；同一 `request_id`
  绑定不同 `subject_id` / `period` 返回 `409`（`request_id_conflict`）；
- 周期尚未揭盲、已经完成、落后或超前（含实验未开始 / 已结束）均返回 `409`：
  `period_out_of_order`（越序，回传 `current_period`）或
  `period_already_completed`（重复登记，原完成时间不变）；
- 最后一个周期也可登记完成，登记后该受试者 `completed=true`；
- 实验或受试者不存在返回 `404`。

### 查询受试者进度 `GET /experiments/{experiment_id}/subjects/{subject_id}/progress`

只返回已揭盲 / 已完成记录、下一周期与下一周期最早可推进时间，不含完整序列或后续安排：

```json
{
  "experiment_id": "…",
  "subject_id": "s1",
  "next_period": 2,
  "num_periods": 3,
  "washout_seconds": 86400,
  "completed": false,
  "next_available_at": "2026-09-22T10:00:00.000000+00:00",
  "reveals": [
    {
      "period": 1,
      "condition": "A",
      "revealed_at": "…",
      "completed_at": "2026-09-21T10:00:00.000000+00:00"
    }
  ]
}
```

- `reveals[].completed_at`：该周期的完成登记时间，未登记为 `null`；
- `next_available_at`：下一周期最早可推进的时刻（上一周期 `completed_at + washout_seconds`）。
  周期 1 之前、实验已结束，或上一周期尚未完成时为 `null`。

实验或受试者不存在返回 `404`。

### 其他接口

- `GET /experiments/{experiment_id}`：查询实验（受试者名册 + 条件清单，**不含条件序列**；不存在返回 `404`）；
- `GET /experiments`：列出全部实验摘要；
- `GET /health`：健康检查。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest                         # 运行测试（含序列平衡、幂等、门禁、409 并发、旧库迁移校验）
uvicorn app.main:app --reload  # 本地启动，默认 http://127.0.0.1:8000
```

## 实现要点

- **Williams 序列**（`app/williams.py`）：第一行按 `0, n-1, 1, n-2, …` 从两端交替取值，
  其余行循环移位（模 n）；奇数阶再拼接镜像方阵，使每个有向相邻对出现次数相等。
- **确定性分配**（`app/main.py`）：将完整序列组复制 `受试者数 / 组大小` 份构成序列池，
  用 `random.Random(seed)` 打乱后依次映射给受试者，因此相同输入与种子结果一致。
  序列只写入服务端存储，不进入创建 / 详情响应。
- **存储**（`app/db.py`，SQLite）共八张表，启动时幂等迁移（旧库自动补齐）：
  - `experiments`：含 `washout_seconds`（旧库缺列时 `ALTER TABLE ... DEFAULT 0` 补齐）；
  - `assignments`：受试者完整序列（服务端持有）；
  - `subject_progress`：每受试者的 `next_period`；
  - `unblindings`：已揭盲记录（周期 → 条件，含幂等键与时间戳）；
  - `period_completions`：周期完成记录（周期 → `completed_at`，主键冲突即重复完成）；
  - `advance_requests` / `completion_requests`：推进与完成各自按
    `(实验, request_id)` 唯一约束保存首次结果，支持重放；
  - `advance_batches`：批量揭盲按 `(实验, batch_id)` 保存首次绑定的有序成员列表
    与首次结果（成员或顺序不同即 `409`）。
- **原子推进与完成门禁**：推进、完成与批量推进都在单个 `BEGIN IMMEDIATE` 事务内完成
  “幂等检查 → 顺序 / 周期校验 → 写入并提交”，配合 `busy_timeout` 让并发请求串行化：
  每个周期只有一个完成请求、一次推进成功，其余得到 `409`；推进时**服务端时间在锁内读取**，
  与并发的完成登记互斥，`completed_at + washout_seconds` 的判断与揭盲写入原子一致；
  批量推进在同一事务内**先统一校验全部成员、再按输入顺序写入**，任何失败路径整体回滚，
  不会绕过门禁、部分揭盲或多揭露条件。
