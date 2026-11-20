# 分析工坊 · 数据分析与数据应用平台

面向指标口径管理、多维度聚合查询、用户分群圈选、AB实验评估、时序趋势分析与数据服务发布的数据分析与数据应用平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 业务指标 | 指标编号、指标名称、计算公式 |
| 维度注册 | `dim_registry` | 分析维度 | 维度编号、维度名称、业务键 |
| 聚合任务 | `agg_task` | 聚合计算 | 任务编号、目标指标、聚合维度 |
| 多维立方体 | `cube_builder` | 数据立方体 | 立方体编号、事实来源、维度列表 |
| 分群规则 | `cohort_rule` | 用户分群 | 分群编号、分群名称、筛选条件 |
| 实验管理 | `experiment` | AB实验 | 实验编号、实验假设、分组方案 |
| 时序分析 | `trend_series` | 时序序列 | 序列编号、观测指标、时间粒度 |
| 归因模型 | `attribution` | 归因链路 | 链路编号、归因窗口、触点类型 |
| 数据分层 | `segmentation` | 数据切片 | 切片编号、源数据集、切分维度 |
| 报表生成 | `report_gen` | 分析报表 | 报表编号、报表类型、数据范围 |
| 预测分析 | `forecast` | 预测方案 | 方案编号、预测指标、预测算法 |
| 数据集市 | `data_mart` | 分析主题 | 主题编号、主题名称、业务域 |
| 统计建模 | `stat_model` | 统计模型 | 模型编号、模型类型、因变量 |
| 数据提取 | `data_extract` | 提取任务 | 任务编号、来源数据集、筛选条件 |
| 质量监控 | `quality_mon` | 监控规则 | 规则编号、监控对象、校验类型 |
| 对比分析 | `compare_job` | 对比任务 | 任务编号、基准数据、对比数据 |
| 数据服务 | `api_service` | 服务接口 | 接口编号、查询数据集、请求参数 |
| 调度编排 | `schedule_plan` | 调度计划 | 计划编号、关联任务、Cron表达式 |

## 归因对比与本班销账

对比分析页在任务列表之外提供两块联动区域，数据来自同一条管道
`/api/pipeline/attribution-compare`（归因 → 指标口径 → 对比任务）：

- **差异清单**（`GET /api/compare_job/diff-rows?link_code=...`）
  按触点发布首次/末次归因金额与差异。金额先按原始值参与计算，发布时统一
  四舍五入到分（`ROUND_HALF_UP`），差异只由两侧已发布金额相减得出，
  因此任何一行都能用表里的两个金额复算出差异。某一侧没有观测到的触点
  发布为 `null`（前端显示「未取得」），不会被当成观测到的 0 元。
- **本班可销账清单**（`GET /api/compare_job/claimable`）
  只有两侧可比的触点进入销账范围；差异为 0 的已核平触点可立即关掉。
  单侧待补的触点会单独计数并给出空态原因：整批没有交易 / 一侧补报未到 /
  金额已核平，三者可区分。
- **补报与重提**（`POST /api/pipeline/attribution-compare?late_batch=true&batch_id=...`）
  同一 `batch_id` 重复提交只覆盖同一批差异行，不追加重复行。补报改变了
  某一侧金额时，该触点的旧销账结论作废，进入「需复核」并重新回到可领范围；
  金额未变时重复销账会被拦下。
- **下游报表**（`GET /api/compare_job/report`）
  分别说明比较覆盖的触点、已确认差异的触点与已销账/未销账/需复核触点，
  差异合计等于已发布差异行之和。

可用的示例链路：`ATTR-PRECISION`（分值舍入与补报）、`ATTR-001`（补报）、
`ATTR-NOTRADE`（整批无交易）、`ATTR-PENDING`（一侧补报未到）。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
