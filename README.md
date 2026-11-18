# 样本污染追踪 API（Contamination Trace API）

实验室生物安全员依据样本转运记录划定污染追踪范围。本服务接收样本与转运批次
（起点、终点、离库/入库 UTC 时间、批次号）以及污染源样本，按时间顺序计算污染
可达样本；返回每个受影响样本的**最早到达时间**、**完整路径**和**隔离截止时间**。

更正批次时必须携带客户端所见版本（`seen_version`），服务以新版本替换旧记录，
并使依赖旧版本的已发布结果失效。查询在受理时固定快照，失败时不会发布半份结果。

本服务同时提供**时点核对**（`POST /inventory-checks`）：沿样本关联的转运批次还原
某站点在指定 UTC 时刻的在库区间，列出在库样本、支撑批次版本，并把行程时间重叠或
相邻站点不衔接的样本单列为冲突批次，供实验室交接员核对实物。

---

## 1. 启动

### Docker Compose（推荐）

```bash
docker compose up --build -d
open http://localhost:8000/docs      # 交互式 API 文档（Swagger UI）
curl http://localhost:8000/healthz
```

数据保存在命名卷 `trace-data`（容器内 `/data/trace.db`），重启后保留。
停止：`docker compose down`；同时删除数据：`docker compose down -v`。

### 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
TRACE_DB_PATH=./data/trace.db .venv/bin/uvicorn app.main:app --port 8000
```

`TRACE_DB_PATH` 默认为 `./data/trace.db`；系统自动创建目录与 SQLite 表。

### 本地示例

```bash
docker compose up --build -d
./examples/run.sh                 # 使用 http://localhost:8000
API=http://127.0.0.1:9000 ./examples/run.sh
```

示例数据在 `examples/intake.json`。脚本每次运行使用唯一的 ID 前缀，因此可在同一
数据库上反复执行。

---

## 2. 追踪语义（必须明确的部分）

一次转运批次是一条**边**：`origin → destination`，在
`[departed_at, arrived_at]` 之间发生（离库/入库均为 UTC）。一个批次可以装载多个
样本（共享批次）。

**污染批次**的定义（二者之一）：

1. 该批次装载了被标记为污染源的样本；
2. 该批次从某站点**离库**的时间 `t_depart` 落在某污染批次在该站**入库**时间
   `t_arrive` 的窗口内：

   ```
   t_arrive <= t_depart <= t_arrive + 24h
   ```

   即“后续转运须在上一站入库后 24 小时内离库”：恰好 24 小时算作可达，早于污染
   入库则不可达。

这是一条**站点级**的传播规则（上一条链的终点必须等于下一条链的起点）。

**受影响样本**：装载于任一污染批次上、且自身不是污染源的样本。

对每个受影响样本返回：

| 字段 | 含义 |
| --- | --- |
| `first_arrival_at` | 与污染材料中该样本的**最早**入库时间 |
| `quarantine_until` | `first_arrival_at + 24h`（隔离截止时间） |
| `path` | 从污染源批次到该样本的**完整批次链**（每条腿含批次号与版本） |

结果按 `first_arrival_at`、再按 `sample_id` 排序。路径的确定性规则：优先最短路径，
其次按 `(arrived_at, batch_no)` 的处理顺序（结果可复现）。

实现为按 `arrived_at` 单遍扫描的闭包（污染入库必然早于后续离库），每个站点用区间
最小值结构查询 24 小时窗口，因此大规模、含热点枢纽的数据集也能在可接受时间内完成。

---

## 3. API 摘要

完整契约与示例见 `http://localhost:8000/docs`（OpenAPI 3）。

| 方法与路径 | 用途 |
| --- | --- |
| `POST /intakes` | 注册样本、转运批次与污染源；单事务原子写入 |
| `GET /dataset` | 查看当前数据集（含每个批次当前版本） |
| `POST /batches/{batch_no}/corrections` | 以新版本替换批次，并使旧结果失效 |
| `POST /traces` | 在固定快照上计算污染可达样本 |
| `GET /traces` | 列出所有已受理查询 |
| `GET /traces/{trace_id}` | 重读已发布的快照结果 |
| `GET /traces/{trace_id}/invalidations` | 查询失效记录 |
| `POST /inventory-checks` | 在固定快照上还原指定 UTC 时刻某站点的在库样本 |
| `GET /inventory-checks` | 列出所有已受理的时点核对 |
| `GET /inventory-checks/{query_id}` | 重读已发布的时点核对结论 |
| `GET /inventory-checks/{query_id}/invalidations` | 查询时点核对的失效记录 |
| `GET /healthz` | 就绪探针 |

### 3.1 `POST /intakes`

```jsonc
{
  "source_sample_ids": ["S-001"],
  "samples": [
    {"sample_id": "S-001", "batch_nos": ["B-1001"]},
    {"sample_id": "S-002", "batch_nos": ["B-1001", "B-1002"]},
    {"sample_id": "S-003", "batch_nos": ["B-1003"]}
  ],
  "batches": [
    {"batch_no": "B-1001", "origin": "Collection-Lab", "destination": "Hub-North",
     "departed_at": "2026-01-01T08:00:00Z", "arrived_at": "2026-01-01T10:00:00Z"},
    {"batch_no": "B-1002", "origin": "Hub-North", "destination": "Reference-Lab",
     "departed_at": "2026-01-01T20:00:00Z", "arrived_at": "2026-01-01T22:00:00Z"},
    {"batch_no": "B-1003", "origin": "Reference-Lab", "destination": "Archive",
     "departed_at": "2026-01-02T10:00:00Z", "arrived_at": "2026-01-02T12:00:00Z"}
  ]
}
```

- `batches` 也可内联在样本的 `batches` 字段中；两种写法可混用。
- 污染源可用 `source_sample_ids` 或样本上的 `"contaminated": true` 标记。
- 批次号全局唯一，标识一次实体转运；多个样本通过 `batch_nos` 共享同一批次。
- 时间必须带时区（`Z` 或偏移）；朴素时间按 UTC 解释；响应统一输出 `Z`。
- 提交是**幂等且增量**的：完全相同的重复提交不会产生重复记录；同批次号但取值
  不同则返回 `409`，必须走更正接口。

### 3.2 `POST /traces`

```jsonc
{}                                                  // 使用入库时标记的全部污染源
{"source_sample_ids": ["S-001"]}                    // 或本次查询显式指定污染源
```

受理时在**单个写事务**内读取数据集并固定快照（`intake_id` + 每个批次的版本），
之后的计算只使用该快照。返回：

```jsonc
{
  "trace_id": 1,
  "status": "complete",
  "has_sources": true,
  "source_sample_ids": ["S-001"],
  "affected_samples": [
    {
      "sample_id": "S-003",
      "first_arrival_at": "2026-01-02T12:00:00Z",
      "quarantine_until": "2026-01-03T12:00:00Z",
      "path": [
        {"batch_no": "B-1001", "origin": "Collection-Lab", "destination": "Hub-North",
         "departed_at": "...", "arrived_at": "...", "version": 1},
        {"batch_no": "B-1002", "...": "...", "version": 1},
        {"batch_no": "B-1003", "...": "...", "version": 1}
      ]
    }
  ],
  "snapshot": {"intake_id": 1, "accepted_at": "...", "batch_versions": {...}, "batches": [...]}
}
```

**失败不发布半份结果**：所有结果行与 `status=complete` 在同一事务中提交；任何异常
都会回滚事务、删除可能的部分行，并把该查询标记为 `failed`。读取失败查询返回 `500`
且不含任何结果行。进程在发布前崩溃时，遗留超过 10 分钟的 `pending` 查询会在下次
启动时标记为 `failed`（宽限期避免多进程部署时误杀其他 worker 正在计算的任务）。

### 3.3 `POST /batches/{batch_no}/corrections`

```jsonc
{
  "seen_version": 1,
  "origin": "Hub-North",
  "destination": "Cold-Room",
  "departed_at": "2026-01-01T20:00:00Z",
  "arrived_at": "2026-01-01T21:00:00Z"
}
```

- `seen_version` 必须等于当前版本，否则 `409`（乐观锁，防止覆盖他人更正）。
- 更正会插入 `version + 1` 的新记录并将旧记录标记为已取代；新值原样返回并附带
  `invalidated_trace_ids`。
- 固定了旧版本的已发布查询会被记录失效，随后 `GET /traces/{id}` 返回 `409`
  `snapshot_invalidated`（附失效明细），绝不返回过期数据。
- 没有实际变化的更正返回 `409`。

### 3.4 `POST /inventory-checks`（时点核对）

给定站点与一个 UTC 时刻，按样本关联的转运批次还原站内区间，返回应在库的样本
以及支撑判断的批次版本，供交接员核对实物。

```jsonc
{"site": "Reference-Lab", "as_of": "2026-01-02T11:00:00Z"}
```

区间语义（左闭右开）：

- 首次转运**离库之前**不推断位置（`as_of < 第一条 departed_at` ⇒ 不在任何站点）；
- `[departed_at, arrived_at)` 为在途，**不计入任何站点在库**；
- `[arrived_at, 下一次 departed_at)` 计入该批次的目的站点；
- 末次 `arrived_at` 之后持续计入末站（`in_stock_until` 为 `null`）。

时间轴按 `(departed_at, arrived_at, batch_no)` 排序。出现以下任一情况时，样本无法
被安全定位，**绝不出现在在库名单中**，而是列入 `conflicting_samples`：

| `code` | 含义 |
| --- | --- |
| `overlapping_transits` | 后一转运在“排序后紧邻的前一转运”入库前离库（行程时间重叠） |
| `disconnected_route` | 后一转运的起点不是前一转运的终点（相邻站点不衔接） |

冲突是**数据集级**的：即使样本的冲突时段与所询站点无关，也会列出，避免交接员误信
一份“看起来干净”的名单。冲突条目内含 `batch_nos` 与两个批次的完整版本记录。

响应：

```jsonc
{
  "query_id": 1,
  "site": "Reference-Lab",
  "as_of": "2026-01-02T11:00:00Z",
  "status": "complete",
  "in_stock_samples": [
    {
      "sample_id": "S-002",
      "in_stock_since": "2026-01-01T22:00:00Z",
      "in_stock_until": "2026-01-02T10:00:00Z",
      "supporting_batches": [
        {"batch_no": "B-1002", "version": 1, "role": "arrival", "...": "..."},
        {"batch_no": "B-1003", "version": 1, "role": "next_departure", "...": "..."}
      ]
    }
  ],
  "conflicting_samples": [
    {"sample_id": "S-008", "conflicts": [
      {"code": "overlapping_transits", "message": "...",
       "batch_nos": ["B-1009", "B-1010"], "batches": [{"...": "..."}, {"...": "..."}]}
    ]}
  ],
  "in_stock_count": 1,
  "conflict_count": 1,
  "snapshot": {
    "intake_id": 1, "accepted_at": "...",
    "associations": {"S-002": ["B-1001", "B-1002"]},
    "batch_versions": {"B-1001": 1, "B-1002": 1, "B-1003": 1},
    "batches": [{"batch_no": "...", "version": 1, "...": "..."}]
  }
}
```

`in_stock_samples` 按 `(in_stock_since, sample_id)` 排序，`conflicting_samples` 按
`sample_id` 排序。未知站点不是错误：返回空的在库名单（`201`）。

**快照与失效**（与 `POST /traces` 同一套约束）：

- 受理时在单个写事务内固定**样本关联快照**与所引用批次的版本；
- 发布前再次校验：任一被固定批次版本变化，或出现**新增关联**，则拒绝发布
  （`409 snapshot_changed`），该次核对标记为 `failed` 且零结果行；
- 已发布的核对被以下情况记录失效，随后 `GET /inventory-checks/{id}` 返回 `409
  snapshot_invalidated`（含失效明细），绝不返回过期名单：
  - 被其固定的批次被更正（`invalidated_inventory_query_ids` 出现在更正响应中）；
  - 之后再入库**任何新的 `(样本, 批次)` 关联**（`kind=association`）。名单是“该站点
    全体样本”的列表，新关联即使来自新样本也可能改变名单，因此数据集内所有已发布
    核对都会失效。完全相同的重复提交不新增关联，因此**不会**让结论过期。
- 失败不会发布半份结果；崩溃遗留超过 10 分钟的 `pending` 核对会在下次启动时标记
  为 `failed`。

### 3.5 错误格式

所有错误使用统一结构，不使用 FastAPI 默认的 `detail` 字符串：

```json
{"code": "validation_error", "message": "request validation failed",
 "details": {"errors": [{"loc": ["body", "batches", 0], "msg": "..."}]}}
```

状态码约定：`404` 未知样本/批次/查询或无数据集；`409` 版本冲突、快照失效、批次取值
冲突、查询尚未发布（`trace_pending` / `inventory_pending`，不会以空结果冒充“无影响”
或“无在库”）；`422` 载荷校验失败或引用不存在的批次/样本；`500` 追踪/核对失败
（含 `trace_id` 或 `query_id`，且无结果行）。

`POST /traces` 的 `500`/`409` 响应体是统一错误结构（`code` 为 `trace_failed` 或
`snapshot_changed`），`details.trace_id` 可用于 `GET /traces` 中定位该次失败记录。
`POST /inventory-checks` 同理，`code` 为 `inventory_failed` / `snapshot_changed`，
`details.query_id` 可在 `GET /inventory-checks` 中定位。

---

## 4. 数据一致性模型

- SQLite WAL 模式；所有写操作使用 `BEGIN IMMEDIATE` 串行化。
- 批次只追加：更正产生新版本，旧版本保留用于审计。
- 查询快照固定 `intake_id` 与每个批次的版本；时点核对还固定样本关联。发布前会再次
  校验这两者未变化，若已变化则拒绝发布（`409 snapshot_changed`，查询标记为
  `failed`）。
- 补录新的样本—批次关联会使时点核对失效（名单的样本总体改变）；完全相同重复提交
  不新增关联，因此不会使结论过期。
- 快照读取、结果读取、失效标记读取都在单个读事务内完成，不会返回“半新半旧”的组合。

## 5. 自测

```bash
.venv/bin/python -m pytest -q
```

覆盖范围（83 个用例）：

- 24 小时窗口边界：恰好 24 小时可达、超出 1 微秒不可达、污染入库前离库不可达；
- 多跳链、共享批次、多个污染源、污染源重写；
- 最早到达时间与完整路径（含确定性并列规则）、隔离截止时间；
- 更正替换、连续多版本、陈旧 `seen_version`、无变化更正、未知批次；
- 更正使旧查询失效、旧查询返回 `409`、新查询反映新值、未被引用的批次更正；
- 发布失败/计算异常时不产生任何结果行，且不影响其他查询；
- 受理与更正并发时不会返回半份结果；
- 400 跳深链、5 万批次宽图、2 万批次热点枢纽的性能回归；
- 400 组随机图与独立暴力参考实现逐一比对（同一 `first_arrival` 与路径）；
- 时点核对的区间边界：首次离库前不推断、离库/到站时刻左闭右开、在途不计入、
  到站后至下一次离库计入、末次到站后持续计入、未知站点返回空名单；
- 时点核对的冲突：行程重叠、站点不衔接、冲突样本不入名单、冲突跨站点仍列出、
  冲突条目携带批次版本；
- 时点核对快照：固定关联与批次版本、更正使旧结论 `409`、补录关联使旧结论 `409`、
  完全相同重复提交不失效、未被引用的批次更正不失效、发布中插入更正/关联则拒绝发布
  且零结果行；
- 400 组随机时间线与独立区间参考实现比对（在库名单逐例一致，冲突与名单互斥）、
  2 万样本名单性能回归；
- 真实 uvicorn HTTP 端到端（`/docs`、入库、追踪、更正、失效、时点核对、错误结构）；
- 重启持久化、崩溃遗留 `pending` 查询的恢复。

Docker 构建的验证情况见下方“已知限制/未验证项”。

## 6. 已知限制与未验证项

- 本环境**未安装 Docker**，无法执行 `docker build` / `docker compose up`。Dockerfile
  与 compose 文件按标准用法编写，并已用相同的 `uvicorn app.main:app` 命令在本机
  验证；容器内 `pip install` 与健康检查命令未实测。
- 时点核对只按关联批次重建区间，**不校验实物盘点**；`in_stock_samples` 是“按记录
  应在库”的名单，用于与交接员实物核对，不代表实物一定在位。
- 冲突判定只看排序后相邻的两次转运，检测重叠与站点不衔接；同一站点内无转运记录的
  长时间停留不被视为冲突。
- 新关联使数据集内**所有**已发布核对失效（名单是全体样本列表）；这是保守策略，
  代价是可能失效与变更样本无关的站点结论。
- 时点核对未提供“按站点过滤冲突”的选项；冲突总是数据集级的。
- 隔离截止时间按“首次到达 + 24 小时”计算，未接入具体病原体的潜伏期参数。
- 未实现认证/授权、速率限制与多租户隔离；默认部署应置于受信网络之后。
- 更正只针对活跃数据集的批次；数据集的整批替换接口未开放（提交是增量式的）。
- 路径选择：优先最短链，其次 `(arrived_at, batch_no)` 处理顺序；这是可复现的确定性
  规则，但不是唯一的合理规则（例如按“最早离库”选路径会得到不同但同样合法的链）。
  若业务要求另一种并列规则，需要在 `app/tracing.py` 中调整。
- 同一批次号在两次入库提交中被定义成不同取值会返回 `409`；必须先更正，不能静默覆盖。
- SQLite 适合单机部署；多副本/高并发写入需要换成服务端数据库。
