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

## 实验级暂停 / 恢复（pause / resume）

管理员可在不删除实验的情况下整体冻结揭盲流程：

- **暂停** `POST /experiments/{id}/pause`：请求体携带调用方生成的唯一
  `operation_id`（如 UUID）与必填 `reason`。服务在**单个写事务**内把
  `status` 置为 `paused`、`state_version` 加一，并追加一条 SQLite 审计记录；
- **恢复** `POST /experiments/{id}/resume`：请求体携带 `expected_version`
  （调用方看到的暂停版本）。服务在写事务内做 **compare-and-set 原子校验**：
  仅当实验当前为 `paused` 且当前版本恰好等于 `expected_version` 时才恢复；
  版本过期（已被其他操作者变更）返回 `409 state_version_mismatch` 并回传
  服务端当前 `state_version` / `current_status`，不会覆盖他人的状态变更；
- **暂停期间的揭盲门禁**：单条推进与批量推进均返回
  `409 experiment_paused`，响应体不含任何条件、周期或后续安排，且**不消耗**
  `request_id` / `batch_id`；恢复后用原幂等键首次成功即可；
- **已成功操作的重放不受暂停影响**：暂停前已成功揭盲的 `request_id` /
  `batch_id`，暂停期间重试仍返回**首次结果**（重放检查先于暂停门禁）；
- 周期**完成登记不是揭盲**，暂停期间仍可登记，便于暂停前已揭盲周期正常收尾；
- 相同 `operation_id` 重放返回首次结果；同一 `operation_id` 绑定不同动作 /
  原因（或恢复时绑定不同的 `expected_version`）返回 `409 operation_id_conflict`。
  暂停与恢复共享同一个 `(实验, operation_id)` 幂等空间；
- 每次暂停 / 恢复都追加写入 `experiment_state_events` 审计表，可通过
  `GET /experiments/{id}/state-events` 按**时间倒序**（最新在前）分页查询，
  支持 `limit`（1～500，默认 100）与 `offset`。

实验的 `status`（`active` / `paused`）与 `state_version` 同时出现在创建、
详情与列表接口的响应中，调用方可据此做乐观并发控制。

## 快速启动（Docker Compose，一条命令）

```bash
docker compose up --build
```

该命令会构建镜像并启动容器：构建时安装依赖，容器启动命令中再次校验依赖安装，
应用启动钩子（lifespan）自动完成 SQLite 建表与迁移。**旧版本数据库无需手工迁移**：
启动时新增的进度、揭盲、周期完成、单条幂等、批量揭盲、状态审计表通过
`CREATE TABLE IF NOT EXISTS` 自动补齐，`experiments` 表新增的 `washout_seconds`、
`status`、`state_version` 列通过幂等的 `ALTER TABLE ... DEFAULT`
补齐——**旧实验一律按 0 秒洗脱期、运行中（active）、状态版本 0 处理**，
旧数据保留且可继续揭盲、登记完成、暂停与恢复。

- **访问地址**：http://localhost:8000
- **交互式 API 文档（Swagger UI）**：http://localhost:8000/docs
- **端口**：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 的 `ports` 中修改）

### 常用 Docker 操作

```bash
# 后台运行
docker compose up --build -d

# 查看实时日志
docker compose logs -f

# 停止服务（保留数据卷）
docker compose down

# 停止并同时删除数据卷（⚠ 清空全部实验数据）
docker compose down -v

# 不使用 compose：直接构建并运行镜像
docker build -t crossover-api .
docker run --rm -p 8000:8000 \
  -v "$PWD/data:/app/data" \
  -e DATABASE_PATH=/app/data/experiments.db \
  crossover-api

# 在运行中的容器内执行测试（dev 依赖需自行安装）
docker compose exec api sh -c "pip install --no-cache-dir pytest httpx && pytest -q"
```

健康检查：`curl http://localhost:8000/health` 应返回 `{"status":"ok"}`。
若端口被占用，修改 `docker-compose.yml` 中 `ports` 的宿主机端口（如
`"18000:8000"`），随后访问 http://localhost:18000 。

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
  "status": "active",
  "state_version": 0,
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
  `retry_after_seconds`（建议等待的整秒数）；响应不携带 `next_period` 或任何条件信息；
- `experiment_paused`：实验已暂停。暂停期间拒绝一切揭盲，响应不含任何条件 /
  周期信息，且不消耗 `request_id`；恢复后用原请求即可首次成功。
  （暂停前已成功的 `request_id` 重试仍返回首次结果，不受暂停影响。）

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

### 暂停实验 `POST /experiments/{experiment_id}/pause`

冻结整个实验的揭盲流程，请求体：

| 字段 | 说明 |
| --- | --- |
| `operation_id` | 调用方生成的唯一操作键（如 UUID），相同操作重试必须复用 |
| `reason` | 暂停原因（必填，非空字符串），随审计记录持久化 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/pause \
  -H 'Content-Type: application/json' \
  -d '{"operation_id": "5c2a…", "reason": "安全性复核，暂停全部揭盲"}'
```

成功返回 `200`，响应即本次状态变更审计记录：

```json
{
  "id": 1,
  "experiment_id": "…",
  "operation_id": "5c2a…",
  "action": "pause",
  "from_status": "active",
  "to_status": "paused",
  "state_version": 1,
  "reason": "安全性复核，暂停全部揭盲",
  "created_at": "2026-09-21T12:00:00.000000+00:00"
}
```

- 相同 `(实验, operation_id)` 与相同原因重试返回**首次结果**（版本不重复递增、
  不重复写审计）；同一 `operation_id` 绑定不同原因 / 动作返回
  `409 operation_id_conflict`；
- 实验已经是暂停状态，换用新 `operation_id` 再暂停返回
  `409 already_paused`（回传当前 `state_version`），且不写审计；
- 实验不存在返回 `404`；`operation_id` / `reason` 缺失或为空返回 `422`。

### 恢复实验 `POST /experiments/{experiment_id}/resume`

携带期望状态版本做原子 compare-and-set 恢复，请求体：

| 字段 | 说明 |
| --- | --- |
| `expected_version` | 调用方看到的暂停版本（暂停接口返回的 `state_version`，或详情接口读取） |
| `operation_id` | 可选的唯一操作键；携带后相同操作重试返回首次结果 |
| `reason` | 可选的恢复原因，随审计记录持久化 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/resume \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 1, "operation_id": "9d7f…", "reason": "复核通过"}'
```

仅当服务端当前状态为 `paused` 且 `state_version == expected_version` 时，
才在单个写事务内恢复为 `active` 并把版本加一（响应结构同暂停，`action` 为
`resume`）。冲突一律 `409`：

- `state_version_mismatch`：期望版本与服务端当前版本不一致（期间可能已被
  暂停 / 恢复多次），回传服务端当前 `state_version` 与 `current_status`，
  调用方应重新读取状态、确认后再操作；
- `already_active`：版本虽然匹配，但实验当前已在运行中（例如以版本 `0`
  恢复一个从未暂停的实验）；
- `operation_id_conflict`：`operation_id` 曾绑定其他动作 / 原因 / 期望版本。

恢复后单条与批量揭盲立即恢复；暂停期间被 `409 experiment_paused` 拒绝的
请求**未消耗幂等键**，用原 `request_id` / `batch_id` 重试即为首次成功。

### 查询状态变更审计 `GET /experiments/{experiment_id}/state-events`

返回暂停 / 恢复的审计记录，**按时间倒序**（最新在前），支持分页：

```bash
curl 'http://localhost:8000/experiments/<experiment_id>/state-events?limit=100&offset=0'
```

```json
{
  "experiment_id": "…",
  "events": [
    {"id": 3, "experiment_id": "…", "operation_id": "9d7f…",
     "action": "resume", "from_status": "paused", "to_status": "active",
     "state_version": 2, "reason": "复核通过", "created_at": "…"},
    {"id": 2, "experiment_id": "…", "operation_id": "5c2a…",
     "action": "pause", "from_status": "active", "to_status": "paused",
     "state_version": 1, "reason": "安全性复核", "created_at": "…"}
  ]
}
```

- `limit`：1～500，默认 `100`；`offset`：分页偏移，默认 `0`；非法值返回 `422`；
- 审计记录只增不改；从未暂停 / 恢复过的实验返回 `events: []`；
- 实验不存在返回 `404`。

### 其他接口

- `GET /experiments/{experiment_id}`：查询实验（受试者名册 + 条件清单 +
  `status` / `state_version`，**不含条件序列**；不存在返回 `404`）；
- `GET /experiments`：列出全部实验摘要（含 `status` / `state_version`）；
- `GET /health`：健康检查。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest                         # 运行测试（含序列平衡、幂等、门禁、409 并发、暂停/恢复、旧库迁移校验）
uvicorn app.main:app --reload  # 本地启动，默认 http://127.0.0.1:8000
```

## 实现要点

- **Williams 序列**（`app/williams.py`）：第一行按 `0, n-1, 1, n-2, …` 从两端交替取值，
  其余行循环移位（模 n）；奇数阶再拼接镜像方阵，使每个有向相邻对出现次数相等。
- **确定性分配**（`app/main.py`）：将完整序列组复制 `受试者数 / 组大小` 份构成序列池，
  用 `random.Random(seed)` 打乱后依次映射给受试者，因此相同输入与种子结果一致。
  序列只写入服务端存储，不进入创建 / 详情响应。
- **存储**（`app/db.py`，SQLite）共十张表，启动时幂等迁移（旧库自动补齐）：
  - `experiments`：含 `washout_seconds`、`status`（`active` / `paused`）与
    `state_version`（旧库缺列时分别 `ALTER TABLE ... DEFAULT 0 / 'active' / 0` 补齐）；
  - `assignments`：受试者完整序列（服务端持有）；
  - `subject_progress`：每受试者的 `next_period`；
  - `unblindings`：已揭盲记录（周期 → 条件，含幂等键与时间戳）；
  - `period_completions`：周期完成记录（周期 → `completed_at`，主键冲突即重复完成）；
  - `advance_requests` / `completion_requests`：推进与完成各自按
    `(实验, request_id)` 唯一约束保存首次结果，支持重放；
  - `advance_batches`：批量揭盲按 `(实验, batch_id)` 保存首次绑定的有序成员列表
    与首次结果（成员或顺序不同即 `409`）；
  - `experiment_state_events`：暂停 / 恢复的追加式审计记录（自增 `id`、
    `action`、前后状态、`state_version`、原因、时间戳），按
    `(实验, operation_id)` 唯一约束支持操作幂等重放，查询按 `id DESC` 时间倒序。
- **暂停 / 恢复的原子性**：暂停与恢复都在单个 `BEGIN IMMEDIATE` 写事务内完成
  “幂等检查 → 状态 / 版本校验 → 更新 `experiments` 并追加审计 → 提交”。
  恢复以 `state_version` 做 compare-and-set，版本不符整体回滚并返回 `409`，
  并发恢复只有一个赢家；暂停门禁同样在揭盲写事务内判定，与暂停 / 恢复互斥：
  事务提交后的揭盲必看到 `paused` 而拒绝，不会出现暂停后仍漏出条件。
- **原子推进与完成门禁**：推进、完成与批量推进都在单个 `BEGIN IMMEDIATE` 事务内完成
  “幂等检查 → 顺序 / 周期校验 → 写入并提交”，配合 `busy_timeout` 让并发请求串行化：
  每个周期只有一个完成请求、一次推进成功，其余得到 `409`；推进时**服务端时间在锁内读取**，
  与并发的完成登记互斥，`completed_at + washout_seconds` 的判断与揭盲写入原子一致；
  批量推进在同一事务内**先统一校验全部成员、再按输入顺序写入**，任何失败路径整体回滚，
  不会绕过门禁、部分揭盲或多揭露条件。
- **暂停期揭盲门禁与幂等重放的顺序**：单条推进与批量推进都**先做幂等命中检查**，
  已成功操作的重放即使在暂停期间也直接返回首次结果（只读，不重新推进）；
  未命中的新请求再经过暂停门禁，得到不含任何条件信息的 `409 experiment_paused`，
  且不写入幂等记录，恢复后原键仍可首次成功。
