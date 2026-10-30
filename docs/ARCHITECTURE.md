# AI Pulse 架构与代码库分析报告

> 分析对象：本仓库全部源码（42 个 Python 文件，约 4900 行，含 `ui/` 与 `tests/`；不含 README 与依赖清单）
> 分析方式：全量源码通读 + 调用链追踪 + 依赖关系核对
> 更新时间：2026-10-30（P0–P3 完成 + 21 项可靠性 / 安全提升落地并自测通过后）
> 文档用途：既是架构分析，也是**交接文档**。其他 Agent / 开发者读「项目现状速览」+「十、实施进度」即可知道项目做了什么、还没做什么。

---

## 项目现状速览（交接用，2026-10-30）

**一句话**：单机可运行的 AI 行业情报系统——RSS 抓取 → SQLite → Chroma 历史记忆 → Collector/Analyst/Critic 三 Agent → 事件线归并 → 预警人工审批 → Webhook 推送，每次 run 额外产出一份 Markdown 日报。

**模块职责**

| 文件 | 职责 | 备注 |
| --- | --- | --- |
| `app.py` | Streamlit 页面：KPI、图表、报告区、Agent 轨迹、HITL 审批、审批历史 | 约 500 行；查询走 `st.cache_data` 缓存，写操作后主动清缓存 |
| `ui/style.css` · `ui/charts.py` · `ui/trace.py` | 页面样式 / Plotly 图表 / 轨迹面板 | 从 app.py 拆出，视觉零改动 |
| `pipeline.py` | 一次 run 的总编排 + 跨进程运行锁 + 热度/正面占比校正 + 日报导出 | 唯一入口，UI 与调度器都调它 |
| `fetcher.py` | RSS 抓取、清洗、关键词匹配、缓存与共享预算 | 共享 `wait(timeout)` 截止时间，慢源不会饿死后续源 |
| `textutils.py` | 文本归一化 / 二元组 / 标题与 URL 规范化 | 原先在 fetcher / events / critic / database / rag 各写一份，现统一 |
| `sentiment.py` | 情感词表与规则打分 | fetcher 与 analyst 共用，避免两份词表漂移 |
| `sources.py` | 数据源 / 关键词 / 检索词 / 别名的唯一注册表 | **加源、加关键词只改这里** |
| `database.py` | SQLite 九张表、`user_version=4` 迁移、去重、增量过滤、情感回写、事件线、分析结果回放、归档、审批原子抢占 | 所有 SQL 集中在此；`--stats` / `--archive-days` CLI |
| `reports.py` | Markdown 日报渲染与落盘 | 写 `reports/`，已 gitignore |
| `events.py` | 事件线与实体追踪：多来源同一事件的标题相似度归并 | 纯规则，零 token 成本 |
| `api.py` | 只读 + 触发类 HTTP API（标准库 `http.server`，12 个端点）+ 飞书卡片回调 | Token 鉴权、CORS 白名单、飞书签名、参数 400、就绪检查；默认 `127.0.0.1:8502` |
| `logging_setup.py` | 统一日志：控制台 + `logs/pulse.log`（2MB × 3 轮转） | `pipeline` 的 `_tee_log` 双写 UI 与文件 |
| `agents/` | `state` / `prompts` / `collector` / `analyst` / `critic` / `graph` | 60 行纯代码状态机，不依赖 LangGraph |
| `llm/client.py` | DeepSeek JSON Mode、token 计量、429/5xx 退避重试 | 每次调用落 `llm_calls` |
| `rag/store.py` · `rag/memory.py` | Chroma 向量记忆（失败 5 分钟冷却重试）+ 多查询召回 + 关键词隔离 + 7 天窗口周同比 | 嵌入是字符 n-gram 哈希，离线可跑 |
| `notify.py` | 钉钉 HMAC 加签 / 飞书卡片推送 | Webhook 对 429/5xx 指数退避重试；只在人工审批后调用 |
| `scheduler_service.py` | APScheduler 每天 08:00，多关键词串行 | 独立进程，复用运行锁 |
| `tests/` | 131 个 pytest 用例 | CI：ruff + coverage + pytest，`.github/workflows/ci.yml` |

**一次 run 的实际顺序（当前代码）**

```
pipeline.run(keyword, sources, trigger)
  → 获取 .pulse.lock 跨进程锁（30 分钟残留可抢占）
  → 过期预警 expire_stale_alerts(24h)
  → fetcher.fetch_all()                  6 线程并发抓源 + 12 秒预算 + 结果缓存
  → database.filter_unseen()             计算真正的新条目
  → database.save_items()                (date,title,keyword) 去重入库
  → [无新条目] 回退最近 1 天存档继续分析
  → rag_store.index_items()              SQLite → Chroma 幂等 upsert
  → rag_memory.build_context()           召回近 7 天相似（按 keyword 过滤）+ 7 天窗口周同比
  → agents.graph.run()                   Collector → Analyst → Critic（≤3 次回边）
  → pipeline.compute_heat()              代码计算热度，覆盖模型值
  → pipeline.compute_positive_ratio()    与模型偏差 ≥20 个百分点时用代码统计覆盖
  → database.update_sentiments()         Agent 判定情感回写数据库
  → events.link_run()                    同事件多来源报道归并为事件线（纯规则）
  → database.save_analysis()             分析结果落库，刷新页面可回放
  → [negative_alerts > 0] save_alert()   只暂存 pending，等待人工审批
  → reports.save()                       写 reports/YYYY-MM-DD-{keyword}.md
  → finish_run()                         回写 run_logs
  → 释放锁
推送不在本流程内：用户点「审核并发送」后走 pipeline.push_alert()
```

**数据层（SQLite，WAL，`user_version=4`）**

| 表 | 用途 | 关键约束 |
| --- | --- | --- |
| `daily_intel` | 抓取到的资讯 | `UNIQUE(date, title, keyword)`；`user_version=1` 由 v0 迁移而来 |
| `run_logs` | 每次 run 的状态、条数、耗时、错误 | `run_id` 主键 |
| `llm_calls` | 每次 LLM 调用的 token / 耗时 / 成败 | 按 `run_id + node` 可审计 |
| `alerts` | 预警与审批状态 | `pending / sending / sent / failed / expired / rejected`；`reviewed_by` / `review_note` 审计字段（v3）；`is_demo` 演示标记（v4） |
| `event_threads` | 事件线：实体、首末日期、报道数 | `thread_key` 唯一（v2 新增） |
| `event_links` | 事件线 ↔ 资讯明细 | `UNIQUE(thread_id, title)` 防重复计数（v2 新增） |
| `analyses` | 每次 run 的完整分析快照（JSON） | `run_id` 主键，刷新页面可回放（v3 新增） |
| `daily_intel_archive` | 超期资讯冷数据 | 与 `daily_intel` 同构（v3 新增） |
| `llm_calls_archive` | 超期调用记录冷数据 | 与 `llm_calls` 同构（v3 新增） |

**常用命令**

```powershell
.\.venv\Scripts\streamlit.exe run app.py                       # 页面
.\.venv\Scripts\python.exe api.py                             # HTTP API（127.0.0.1:8502）
.\.venv\Scripts\python.exe pipeline.py --keyword "AI Agent"    # 手动跑一次
.\.venv\Scripts\python.exe scheduler_service.py --now --keywords "大模型,RAG"
.\.venv\Scripts\python.exe events.py --keyword "AI Agent"             # 查看事件线
.\.venv\Scripts\python.exe database.py --stats                        # 各表容量
.\.venv\Scripts\python.exe database.py --archive-days 90 --dry-run    # 归档演练
.\.venv\Scripts\python.exe -m pytest -q                        # 131 个用例
.\.venv\Scripts\ruff.exe check .                               # 静态检查
```

**设计约束（不要违背）**

1. 不改现有前端 UI 布局；
2. Agent 编排用纯代码状态机，不引入 LangGraph；
3. 调度器是独立进程，不与 Streamlit 会话耦合；
4. 预警必须人工审核后才推送，模型不能直接对外广播。

**本地开发注意**

- Windows PowerShell 5 不支持 `&&`，多条命令用 `;` 分隔；
- Streamlit 热重载不会刷新被 import 的模块，改 `ui/` 或非入口文件后需重启进程；
- Python 解释器统一用 `.\.venv\Scripts\python.exe`，直接 `python` 可能不是虚拟环境。

**当前完成度**：P0–P3 全部落地，2026-10-30 的 21 项可靠性 / 安全提升全部完成；`pytest` 131 通过、`ruff` 全绿。详见「十、实施进度」。

---

## 一、先给结论

| 维度 | 评分 | 一句话评价 |
| --- | --- | --- |
| 功能闭环 | 9 / 10 | 从抓 RSS 到人工审批推送，全链路真跑通，不是 Mock |
| 代码分层 | 8 / 10 | 依赖单向、无循环导入，职责边界清楚 |
| 工程质量 | 9 / 10 | 131 个 pytest 用例 + ruff / coverage CI；仍缺类型检查 |
| 生产可用度 | 7 / 10 | 并发锁、Token 鉴权、审批审计、容器化齐备；仍是单机存储 |
| 可扩展性 | 8 / 10 | 数据源 / 关键词已收敛到 `sources.py`；阈值仍硬编码 |

**定位判断**：这是一个「完整度很高的单机情报系统」。P0–P3 与 2026-10-30 的 21 项提升完成后，鉴权、审批审计、事件线、API、容器化都已具备；剩下的短板是 **UI 登录、单机存储上限与语义召回质量**。

**下一步最值得做的 5 件事**：
1. Streamlit 页面登录（OIDC / 反向代理 Basic Auth），把 `reviewed_by` 与真实账号绑定
2. 召回升级：真实 embedding 或 BM25 + 向量混合检索
3. 运营看板：聚合 Critic 一次通过率、误报率、单 run 成本曲线
4. MCP / 事件回调集成（API 已具备，补对外协议）
5. 多实例部署验证（当前 SQLite + 文件锁仍是单机假设）

---

## 二、项目全貌

### 2.1 目录结构与体量

```
AI Pulse/
├── app.py                约 500 行 Streamlit 页面（KPI + 图表 + 轨迹 + HITL + 审批历史）
├── pipeline.py           约 580 行 一次 run 的总编排（运行锁 / 增量 / 热度校正 / 日报）
├── database.py           约 1000 行 SQLite 九张表 + v1–v4 迁移 + 全部 SQL
├── fetcher.py            约 220 行 RSS 抓取、清洗、关键词匹配、共享预算
├── textutils.py          约 50 行  文本归一化 / 二元组 / 标题与 URL 规范化
├── sentiment.py          约 45 行  情感词表与规则打分（fetcher / analyst 共用）
├── sources.py             52 行   数据源 / 关键词 / 别名唯一注册表
├── reports.py             90 行   Markdown 日报导出
├── events.py             170 行   事件线与实体追踪（标题相似度归并）
├── notify.py             137 行   钉钉加签 / 飞书卡片 Webhook
├── scheduler_service.py  110 行   APScheduler 每日 08:00 独立进程
├── config.py              33 行   自己写的 .env 加载器
├── UI layer
│   ├── ui/style.css      355 行   页面样式（从 app.py 原样搬出）
│   ├── ui/charts.py      133 行   Plotly 图表
│   └── ui/trace.py        95 行   Agent 轨迹面板
├── agents/               485 行   state / prompts / collector / analyst / critic / graph
├── llm/client.py         148 行   DeepSeek JSON Mode + token 计量
├── rag/store.py          227 行   Chroma 向量库 + 关键词降级检索
├── rag/memory.py          48 行   历史召回上下文 + 周同比
├── tests/                约 900 行 131 个 pytest 用例
├── .github/workflows/ci.yml      GitHub Actions（ruff + coverage + pytest，无密钥）
├── docs/                         本目录：架构与迭代文档
├── README.md
├── requirements.txt        运行依赖（锁定版本）
├── requirements-dev.txt    测试 / 静态检查依赖
└── pyproject.toml          pytest / ruff / coverage 配置
```

体量分布：`app.py` 从 980 行压到约 500 行（CSS / 图表 / 轨迹已拆到 `ui/`），新增的 `sources.py` / `reports.py` / `textutils.py` / `sentiment.py` 各自职责单一；测试代码已占全部 Python 代码的约 18%。

### 2.2 技术选型与评价

| 层 | 选型 | 选得对吗 | 代价是什么 |
| --- | --- | --- | --- |
| 界面 | Streamlit 1.6x / Plotly 7 | ✅ 对。纯 Python 出效果，零前端成本 | UI 无登录（API 已 Token 鉴权）、长任务阻塞页面、`session_state` 刷新即丢 |
| 大模型 | DeepSeek + 裸 `requests` | ✅ 对。JSON Mode 稳定、便宜 | 已有 429/5xx 两次退避重试；仍无流式、同步阻塞 |
| 存储 | SQLite（WAL）+ pandas | ✅ 对。零运维、单机最合适 | 单写锁、有 `user_version` 迁移、无归档留存 |
| 向量 | ChromaDB + **自研 512 维字符 n-gram 哈希嵌入** | ⚠️ 有取舍。完全离线，不下载模型 | 只有字面相似度，同义词召回弱（"大模型"召不到 "LLM"） |
| 调度 | APScheduler 3.x（BlockingScheduler） | ✅ 对。够轻、够用 | 任务不持久化、重启需重跑；已支持多关键词串行 |
| 推送 | requests + HMAC-SHA256 加签 | ✅ 对 | 已有 429/5xx 指数退避重试与飞书回调按钮 |
| 配置 | 自写 `.env` 解析（不引 python-dotenv） | ✅ 对，展示基本功 | 无字段校验、无类型检查 |

> 纠正一处 README 与口头描述的偏差：**RAG 并没有用 ONNX MiniLM 模型**，`rag/store.py` 的 `_embed_text()` 用的是字符 1/2-gram 哈希 + 512 维桶向量。这是完全离线方案，好处是永远不会因下载模型失败，坏处是「语义」很弱。

### 2.3 依赖关系图（已核对：无循环导入）

```
app.py ─┐                    ┌─→ fetcher ──→ sources / database
        ├─→ pipeline ────────┤
scheduler_service.py ─┘      ├─→ rag.store ──→ chroma / database
                             ├─→ rag.memory ──→ rag.store / database
                             ├─→ reports ──→ reports/*.md（文件系统）
                             └─→ agents.graph
                                    ├─ collector ─┐
                                    ├─ analyst ───┼─→ llm.client ──→ DeepSeek / database
                                    ├─ critic ────┘
                                    └─ prompts
                             pipeline.push_alert ──→ notify ──→ config
app.py ──→ ui.charts / ui.trace
database / sources / config 被所有人依赖，但自己谁也不依赖（叶子节点）
```

这个结构是**合格的分层**：入口（UI / 调度）→ 编排（pipeline）→ 能力（agents / rag / fetcher）→ 基础设施（llm / database / config）。没有一处 `database` 反过来 import `pipeline`，这是很多人写小项目时最容易犯的错，这里没犯。

### 2.4 一次完整 run 的调用链

```
pipeline.run(keyword, sources, trigger)                         # pipeline.py
  ├─ run_lock()                                 跨进程 .pulse.lock，残留 30 分钟可抢占
  ├─ expire_stale_alerts(24h)                   清理超时未审预警
  ├─ database.start_run()                       写 run_logs（status=running）
  ├─ fetcher.fetch_all()                        6 线程并发抓源，12 秒共享截止时间
  ├─ database.filter_unseen()                   只保留尚未入库的新条目
  ├─ database.save_items()                      INSERT OR IGNORE 去重
  ├─ [无新数据] database.fetch_recent(1天)        兜底用存档继续分析
  ├─ rag_store.backfill(7天) + index_items()    SQLite → Chroma 幂等 upsert
  ├─ rag_memory.build_context()
  │    ├─ store.query_similar()                 7 天 + keyword 过滤的多查询召回，失败降级
  │    └─ _week_over_week()                     近 7 天 vs 上一个 7 天（禁止模型自算）
  ├─ agents.graph.run()                         ★ 核心
  │    ├─ collector.run()                       LLM 抽事件 / 规则降级
  │    ├─ for attempt in 1..3:
  │    │    ├─ analyst.run()                    LLM 出 7 字段 JSON
  │    │    └─ critic.check_contract()          9 类契约校验，失败把 issues 回灌
  │    └─ critic.finalize()                     数值归一 + 默认值兜底
  ├─ compute_heat()                             条数 + 源数算热度，覆盖模型自报值
  ├─ compute_positive_ratio()                   偏差 ≥20pp 时覆盖模型正面占比
  ├─ database.update_sentiments()               Agent 判定情感回写 daily_intel
  ├─ [negative_alerts > 0] database.save_alert()    只暂存 pending，不推送
  ├─ database.usage_summary()                   本 run 的 token / 耗时
  ├─ reports.save()                             写 reports/YYYY-MM-DD-{keyword}.md
  └─ database.finish_run()                      回写 success / failed
```

注意最后一步的分岔：**推送不在这个流程里**。真正发送要等用户在页面上点「审核并发送」，走 `pipeline.push_alert()` → `database.claim_alert()`（`pending/failed → sending` 原子抢占）→ `notify.send_webhook()`（429/5xx 指数退避）→ 回写 `alerts.status`。并发审批时只有一个请求能抢占成功，避免重复推送。

---

## 三、已实现的核心能力盘点

| # | 能力 | 实现位置 | 成熟度 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 多源 RSS 采集 | `sources.py` `SOURCES` · `fetcher.py` | ●●●●○ | 7 个逻辑源（新增 arXiv / InfoQ / Solidot）、单源重试 1 次、机器之心失效自动回退量子位 |
| 2 | 关键词中英双语匹配 | `sources.py` `KEYWORD_ALIASES` | ●●●●○ | "AI Agent" 能同时命中 `agentic` / `智能体` |
| 3 | 入库与去重 | `database.py` `save_items()` | ●●●●○ | `(date, title, keyword)` 唯一 + WAL + 索引，跨关键词归属不再丢失 |
| 4 | 向量记忆 + 降级检索 | `rag/store.py` `query_similar()` | ●●●○○ | 双层降级：Chroma 挂了退 Jaccard 字符二元组 |
| 5 | 周同比 | `rag/memory.py` `_week_over_week()` | ●●●●○ | 改为「近 7 天 vs 上一个 7 天」，避免单日噪声；仍由代码计算，模型只能引用 |
| 6 | 三 Agent 状态机 | `agents/graph.py` `run()` | ●●●●○ | 60 行实现 State / Node / 回边，替代 LangGraph |
| 7 | 契约校验 + Reflexion | `agents/critic.py` `check_contract()` | ●●●●○ | 7 个字段契约、11 条具体打回规则（含 top_news 标题可核验），最多 3 次，超限规则降级 |
| 8 | 全链路降级 | analyst / collector / store 三处 | ●●●●● | 无 Key / 网络挂 / JSON 坏 / 向量挂，页面都不白屏 |
| 9 | 成本可观测 | `llm/client.py` `chat()` | ●●●●○ | 每次调用按 `run_id` 落 `llm_calls`，轨迹面板可查 |
| 10 | HITL 审批闸门 | `pipeline.push_alert()` + `app.py` | ●●●●● | 六态 + `claim_alert()` 原子抢占 + `is_demo` 标记，并发审批不重复推送 |
| 11 | 定时任务 | `scheduler_service.py` | ●●●●○ | 独立进程、`max_instances=1`、`coalesce` + 1h 补跑、多关键词串行 |
| 12 | 双渠道推送 | `notify.py` | ●●●○○ | 钉钉 HMAC 加签 + 飞书交互卡片，自动识别渠道 |
| 13 | 增量抓取 | `database.filter_unseen()` | ●●●●○ | 只把新条目送 LLM/RAG，无新增才回退近 1 天存档 |
| 14 | 热度代码化 | `pipeline.compute_heat()` | ●●●●○ | 由条数 + 源数计算，覆盖模型自报值，杜绝随口报数 |
| 15 | 日报导出 | `reports.py` | ●●●●○ | 每次 run 写 `reports/YYYY-MM-DD-{keyword}.md`（不入库） |
| 16 | 事件线与实体追踪 | `events.py` · `database.py` | ●●●○○ | 标题相似度 + 实体命中归并多来源报道；纯规则、零 token，口径可解释 |

---

## 四、真正的技术亮点

1. **把「结构校验」做成了 LLM 的质检门。** 大多数同类项目直接 `json.loads` 完就往页面上怼，模型少个字段就白屏。这里的 Critic 对 7 个字段做了 11 条校验，其中两条真正触碰「事实」：`negative_alerts` 不能超过输入资讯条数；`top_news` 的短标题必须能在输入标题中找到（包含关系或字符重合 ≥ 0.6，不足 3 条打回）。其余是格式约束。

2. **降级不是一句口号，是三层各自兜底。** 没 Key → 规则引擎；JSON 非法 → 打回重算；Chroma 挂 → 字符重合度检索。任何一层挂了，用户看到的仍然是完整页面 + 明确原因。**这是演示场景最关键的护城河**，因为现场网络永远不可靠。

3. **自研 60 行状态机，而不是硬套框架。** `agents/graph.py` 的 `for ... else` 写法把「重试」和「重试耗尽」表达得非常干净。这在面试里比「我用了 LangGraph」更有说服力——说明你理解编排的本质，而不是只会调包。

4. **成本账本。** `llm_calls` 表按 `run_id + node` 记录 token 与耗时，终端还会打 `[COST]`。数据库层已经有 `usage_summary()` 和 `llm_calls_for_run()` 两个聚合函数——**数据已经存好了，只是还没做汇总页面**，这是最容易摘的果子。

5. **风险动作必须人工闸门。** 预警只落库、不推送，天然符合「AI 系统不能让模型直接对真人广播」的安全设计原则。而且审批状态是真的持久化在 SQLite，跨 Streamlit 会话、跨刷新都还在。

6. **XSS 防护是做了的。** `app.py` 里所有来自 LLM 和 RSS 的文本，进 HTML 前都过了 `html.escape()`。很多 Streamlit 项目直接 f-string 拼 HTML，这里没有。

---

## 五、技术短板（按严重程度分级）

### P0 —— 会直接出事

> 状态（2026-10-30）：A1–A4 全部处理。A2 已补 API Token 鉴权与飞书签名；Streamlit 页面本身仍无登录。

| 编号 | 问题 | 现象与影响 | 证据 | 修复方向 |
| --- | --- | --- | --- | --- |
| A1 | **没有并发锁** | 手动点按钮 + 定时任务 + 两个浏览器会话，可以同时跑同一个 keyword：重复扣 token、SQLite 写竞争、Chroma upsert 打架、`alerts` 重复生成 | `pipeline.run()` 全程无锁；scheduler 的 `max_instances=1` 只约束它自己 | 文件锁 + 「同 keyword 冷却 N 分钟」 |
| A2 | **没有鉴权** | 任何能访问 8501 端口的人都能触发花钱、都能审批推送 | Streamlit 裸跑 | `st.login` / 密码门 + 只监听 127.0.0.1 |
| A3 | **零测试、零 CI** | 契约校验这种纯函数是最该被测的，现在只能靠 `--force-bad` 手工验；改一行代码不知道有没有退化 | 全库无 `tests/` | pytest 覆盖 critic / fetcher / database 三处 |
| A4 | **app.py 是上帝文件** | 980 行里 380 行 CSS、2 个图表函数、轨迹面板、侧边栏、主布局、HITL 按钮全挤在一起；而且 Streamlit 热重载不刷新被依赖模块，改图表必须重启进程 | `app.py` 全文 | 拆成 `ui/style.css` + `ui/charts.py` + `ui/trace.py`（视觉零改动） |

### P1 —— 影响正确性与数据质量

> 状态（2026-10-30）：B1–B10 全部处理。B8 改为 7 天窗口周同比，B9 时间统一按本地时区，B10 分析快照落库回放。

| 编号 | 问题 | 现象与影响 | 证据 |
| --- | --- | --- | --- |
| B1 | **跨关键词归属丢失** | `UNIQUE(date, title)` 意味着同一条新闻只归属第一个抓到它的关键词；切到「大模型」时这条新闻在图表里直接消失，KPI 失真 | `database.py` `SCHEMA` |
| B2 | **情感标签不可靠** | `lowered.count("risk")` 会把 "risk-free" 记成负面；DB 存的是词表结果，而 KPI 显示的是 LLM 结果，两边口径不一致 | `fetcher.py` `guess_sentiment()` |
| B3 | **Chroma 一次失败 = 永久降级** | `_disabled_reason` 是模块级变量，常驻 scheduler 进程首次失败后再也不会重试，直到重启 | `rag/store.py` `_disable()` |
| B4 | **LLM 无重试无退避** | 429 / 5xx 直接降级；一次 run 最多 1+3 次调用 × 60s 超时，最坏情况页面卡好几分钟 | `llm/client.py` `chat()` |
| B5 | **幻觉通道没堵死** | Critic 不校验 `top_news` 里的标题是否真的来自输入，模型可以编一条假新闻而顺利通过；`heat_score` 800-2000 也是自由发挥 | `agents/critic.py` |
| B6 | **预警无去重无过期** | 每跑一次就插一条 `pending`，连点 5 次生成 5 条；旧预警永远挂在库里，UI 只显示最新一条 | `database.save_alert()` |
| B7 | **配置散落 5 处** | `KEYWORDS`(app)、`DATA_SOURCES`(app)、`SOURCES` / `KEYWORD_ALIASES` / `SEARCH_TERMS`(fetcher)、`FALLBACK_MATURITY`(analyst)、时区与 08:00(scheduler)；`Twitter/X` 永远抓不到却每次刷 warning | 跨 4 个文件 |
| B8 | **周同比口径太脆** | 只比「今天单日 vs 7 天前单日」，任一天没数据就显示「基线不足」 | `rag/memory.py` |
| B9 | **时区错位** | `calendar.timegm(struct_time)` 把 RSS 时间当 UTC 直接取日期，与本地 UTC+8 差 8 小时 → 晚上的新闻可能落到「昨天」 | `fetcher.py` `entry_date()` |
| B10 | **刷新即丢分析** | `analysis` 只存在 `session_state`，刷新页面后报告区和轨迹面板变空，只剩数据库 KPI | `app.py` |

### P2 —— 生产化才有意义

> 状态（2026-10-30）：以下 7 项全部处理，见第十节 10.3 表。

- ✅ **数据迁移体系**：`PRAGMA user_version` 已到 v4，v1–v4 均为幂等迁移；后续改表仍需手写迁移脚本
- ✅ **无留存策略**：`daily_intel_archive` / `llm_calls_archive` 两表 + `database.py --archive-days` 冷热归档
- ✅ **日志只打 stdout**：`logging_setup` 写 `logs/pulse.log`（2MB × 3 轮转），`_tee_log` 双写 UI
- ✅ **Prompt 注入**：外部文本统一 `<<<UNTRUSTED_DATA>>>` 包裹 + 注入句式屏蔽（记忆与今日资讯都覆盖）
- ✅ **密钥明文**：`config.secret_status()` 掩码展示 + `missing_secrets()` 缺失提示
- ✅ **无部署形态**：Dockerfile（非 root、`TZ=Asia/Shanghai`）+ compose（ui/api/scheduler/caddy）+ Caddyfile
- ✅ **推送无重试**：`notify.py` 对 429/5xx 指数退避重试，仍失败才落 `failed`

---

## 六、可开发方向（价值 / 难度 / 收益）

> 状态：标 ✅ 的条目已在 P0 / P1 阶段落地（括号内注明做到哪一步）；其余仍是待办。

难度说明：★ = 半天到一天；★★ = 一个模块级改动；★★★ = 跨模块重构；★★★★ = 需要新架构能力。

### 6.1 功能扩展

| 编号 | 方向 | 做什么 | 价值 | 难度 | 预期收益 |
| --- | --- | --- | --- | --- | --- |
| F1 | **多关键词任务** ✅ | scheduler 支持 `--keywords`，默认 4 个关键词串行；并行 job 未做 | ★★★★ | ★★ | 从「单关键词玩具」变成「真情报台」 |
| F2 | **数据源注册表 + 扩源** ✅ | `sources.py` 唯一注册表；新增 arXiv / InfoQ / Solidot（Reddit 在当前网络不可达，未采用） | ★★★★★ | ★★ | 情报价值 = 覆盖率；已消灭 Twitter/X 幽灵源 |
| F3 | **日报 / 周报导出** ✅（Markdown 部分） | 每次 run 自动写 `reports/YYYY-MM-DD-{keyword}.md`；PDF / 飞书文档、7 天周报未做 | ★★★★ | ★★ | 产出「能发出去的东西」 |
| F4 | **事件线与实体追踪** ✅（基础版） | 标题字符二元组相似度 + Collector 实体命中，把多来源同一事件归并为一条事件线并记录首末日期；事件级因果链（融资/事故/响应）未做 | ★★★★★ | ★★★★ | 从「新闻摘要」升级为「事件跟踪」 |
| F5 | **预警规则中心** | 自定义负面词、阈值、z-score 热度突增检测 | ★★★★ | ★★★ | 降低误报 |
| F6 | **飞书卡片回调审批** ✅ | 卡片带按钮，点「通过/驳回」经 `/api/callback/feishu` 直接改状态；标准库 `http.server` 即可，无需 FastAPI；已加签名校验与原子 claim | ★★★ | ★★★ | 真正的移动端 HITL |

### 6.2 性能与成本

| 编号 | 方向 | 做什么 | 价值 | 难度 | 收益 |
| --- | --- | --- | --- | --- | --- |
| P1 | **增量抓取** ✅ | `filter_unseen` 只把新条目送进 LLM/RAG，无新增才回退近 1 天存档 | ★★★★ | ★★ | 重复点击不再重复扣费 |
| P2 | **并行抓取** ✅ | 6 线程抓 7 个源，12 秒共享截止时间（`wait(timeout)`），慢源不再饿死后续源 | ★★★ | ★ | 抓取时长压到接近最慢单源耗时 |
| P3 | **当日结果缓存** ✅（抓取层） | 成功结果缓存 180 秒、失败源冷却 300 秒；整轮结果复用仍未做 | ★★★ | ★ | 演示时连点不再重复抓取 |
| P4 | **检索质量升级** | 哈希向量 + BM25 混合召回（RRF 融合），或换本地小语义模型 | ★★★★ | ★★–★★★ | 召回从「字面重合」升级到「意思相近」 |
| P5 | **LLM 调用治理** ✅（退避部分） | 429/5xx 两次指数退避已实现；单节点超时与 run 级预算未做 | ★★★ | ★ | 稳定性提升 |

### 6.3 体验提升

| 编号 | 方向 | 价值 | 难度 | 收益 |
| --- | --- | --- | --- | --- |
| U1 | **结果持久化** ✅：分析结果落库（`analyses` 表），刷新不丢 | ★★★★ | ★★ | 修复刷新页面后报告变空 |
| U2 | **节点级进度可视化**：Collector / Analyst / Critic 逐节点点亮 | ★★★ | ★★ | 演示观感提升明显 |
| U3 | **KPI 下钻**：点「今日入库 32 条」弹出原始新闻列表 | ★★★ | ★★ | 让数据可追溯 |
| U4 | **一键复制 / 分享报告** | ★★ | ★ | 低成本高感知 |

### 6.4 生态集成

| 编号 | 方向 | 价值 | 难度 | 收益 |
| --- | --- | --- | --- | --- |
| E1 | **API / MCP 化** ✅（HTTP 部分） | `api.py` 已暴露 12 个 HTTP 端点（含触发 run 与审批）；MCP Server 未做 | ★★★★★ | ★★★ | 从「一个网页」变成「一个能力节点」 |
| E2 | **飞书 / 钉钉双向**：群内输入「今日 AI 情报」即返回摘要 | ★★★★ | ★★★ | 用户不用打开网页 |
| E3 | **日报自动写入飞书文档 / 多维表格** | ★★★ | ★★ | 沉淀知识资产 |
| E4 | **GitHub Actions 每日跑 + Pages 展示** | ★★★ | ★★ | 零成本托管 demo |

### 6.5 安全加固

| 编号 | 方向 | 价值 | 难度 | 收益 |
| --- | --- | --- | --- | --- |
| S1 | **登录鉴权** ✅（API 部分） | ★★★★ | ★ | 从「谁都能点」到「只有我能点」；API 已 Token 鉴权，Streamlit 页面仍无登录 |
| S2 | **审计日志** ✅ | `reviewed_by` / `review_note` / 操作时间 | ★★★ | ★ | 审批可追责 |
| S3 | **Prompt 注入防护** ✅ | 不可信内容分隔符包裹 + 注入句式清洗 | ★★★ | ★★ | 防「新闻标题操控模型」 |
| S4 | **密钥治理** ✅ | 启动时掩码展示 + 缺失项提示 | ★★ | ★ | 避免把 `sk-` 提交上去 |
| S5 | **按钮冷却 + run 配额** ✅（冷却） | `RUN_COOLDOWN_SECONDS` 默认 90s；配额未做 | ★★★ | ★ | 与并发锁一起做 |

---

## 七、优先级路线图

### P0 · 立即做（低难度、高收益、不改视觉）— ✅ 全部完成

1. ✅ **并发锁 + 按钮冷却**：`pipeline.run_lock` 挡住并发（UI 提示 RunLocked）；按钮级冷却 90s（UI 禁用 + 倒计时，API 返回 429 + `retry_after`）
2. ✅ **pytest 骨架 + GitHub Actions**：131 个用例 + `ruff check .` + coverage，CI 绿（只装测试依赖，不装 streamlit / chromadb）
3. ✅ **拆分 app.py**：`ui/style.css` / `ui/charts.py` / `ui/trace.py`，CSS 与原文件逐字符一致
4. ✅ **Chroma 降级可恢复 + LLM 退避重试**：失败后 5 分钟冷却重试；429/5xx 两次退避
5. ✅ **预警去重 + 过期状态**：同日同关键词复用；24 小时未审自动 `expired`

### P1 · 下一阶段（把「能演示」变「能天天用」）— ✅ 全部完成

6. ✅ 数据源注册表（`sources.py`）+ 新增 arXiv / InfoQ / Solidot
7. ✅ 跨关键词归属修复（`(date, title, keyword)` + `user_version` 迁移）
8. ✅ 情感口径统一（Agent 判定回写；英文词边界匹配）
9. ✅ 防幻觉补丁（`compute_heat` 代码化 + `top_news` 标题核验）
10. ✅ 多关键词调度（`--keywords`，默认全部串行）
11. ✅ 增量抓取（`filter_unseen`）
12. ✅ 日报导出（`reports/*.md`）

### P2 · 战略级（差异化与生态）— ✅ 全部完成（UI 登录仍按要求不做，API 已 Token 鉴权）

13. ✅ 事件线与实体追踪（标题相似度 + 实体命中归并；事件线表 v2）
14. ✅ API 化（`api.py`，标准库 `http.server`，12 个端点；未做 MCP）
15. ✅ 审计 + Docker / HTTPS 部署（`reviewed_by` / `review_note` 落库；Dockerfile + compose + Caddyfile）；API 侧 Token 鉴权已完成（`API_TOKEN` + CORS 白名单），Streamlit 页面仍绑 `127.0.0.1`
16. ✅ 飞书卡片回调审批（卡片按钮 → `/api/callback/feishu` → `push_alert` / `reject_alert`，已加签名校验）
17. ✅ 数据迁移 + 归档留存（`user_version=4`；`analyses` + 两张归档表 + `alerts.is_demo`；`database.py --stats / --archive-days`）

### P3 · 稳定性与体验收口 — ✅ 全部完成

18. ✅ Prompt 注入防护（`<<<UNTRUSTED_DATA>>>` 边界包裹 + 注入句式屏蔽）
19. ✅ 密钥治理（掩码展示、缺失项提示，侧边栏只显示 `sk-1…def`）
20. ✅ 日志文件化（`logging_setup` + `_tee_log` 双写，2MB × 3 轮转）
21. ✅ 按钮冷却（默认 90s，UI 禁用 + 倒计时；API 返回 429 + `retry_after`）
22. ✅ 分析结果持久化（`analyses` 表 + 刷新回放，修复「刷新即丢」）

---

## 八、明确不建议现在做的事

| 想法 | 为什么不做 |
| --- | --- |
| 换成 LangGraph | 当前瓶颈是数据质量和运维，不是编排。三个节点 + 一条回边，60 行已经够了 |
| 前端换 React / Next.js | Streamlit 的短板是鉴权和长任务，不是渲染能力 |
| 上 PostgreSQL / 消息队列 | 单机 MVP 阶段 SQLite + 状态表完全够用 |
| 上 K8s / 云原生 | 过度设计。一台机器 + 一个守护进程 + 一个 cron 就够 |

---

## 九、建议的落地节奏

**第一批（先把地基焊死）** ✅：并发锁 → 测试 → 拆 app.py
这三件做完，项目就从「能跑」变成「敢改」。

**第二批（把数据弄准）** ✅：跨关键词归属 → 情感口径 → 防幻觉补丁
这三件做完，页面上的数字才真正可信。

**第三批（把价值放大）**：源注册表 ✅ → 多关键词 ✅ → 日报导出 ✅ → API ✅（MCP Server 未做）
前四项已完成，MCP 才是真正的对外产品化下一步。

**衡量指标建议**（做完 P0 / P1 后应该达到）：

| 指标 | 目标 | 当前 |
| --- | --- | --- |
| 重复 run 率 | 0 | ✅ 运行锁 + 增量过滤，第二次点击直接被拒或跳过 |
| Critic 一次通过率 | > 80% | 未统计（`_attempts` 已落库，可后续聚合） |
| 单 run token | < 4000 | ✅ 实测 3634（2 次调用） |
| 单 run 时长 | < 20s | ✅ 实测 8.4s（4 个源 / 14 条） |
| 预警人工判定误报率 | < 20% | 未统计 |
| 接入源 | ≥ 7 个 | ✅ 7 个 |

---

## 十、实施进度

| 阶段 | 项目 | 状态 |
| --- | --- | --- |
| P0-1 | 并发锁 | ✅ 已完成（`pipeline.run_lock`，30 分钟残留锁可抢占） |
| P0-2 | pytest 骨架 + CI | ✅ 已完成（131 个用例 + ruff + coverage，`.github/workflows/ci.yml`） |
| P0-3 | 拆分 app.py | ✅ 已完成（`ui/style.css` · `ui/charts.py` · `ui/trace.py`，视觉零改动） |
| P0-4 | Chroma 降级可恢复 + LLM 重试 | ✅ 已完成（5 分钟冷却重试；429/5xx 两次退避重试） |
| P0-5 | 预警去重 + 过期状态 | ✅ 已完成（同日同关键词复用；24 小时未审核自动 `expired`） |
| P1-6 | 数据源注册表 + 新增源 | ✅ 已完成（`sources.py` 唯一注册表；新增 arXiv / InfoQ / Solidot，移除抓不到的 Twitter/X） |
| P1-7 | 跨关键词归属修复 | ✅ 已完成（唯一键改为 `(date, title, keyword)`，`PRAGMA user_version` 一次性迁移） |
| P1-8 | 情感口径统一 | ✅ 已完成（Agent 判定回写 `daily_intel`；英文词按词边界匹配） |
| P1-9 | 防幻觉补丁 | ✅ 已完成（`compute_heat` 覆盖模型热度；Critic 增加 top_news 标题可核验） |
| P1-10 | 多关键词调度 | ✅ 已完成（`--keywords` 逗号列表，默认全部关键词串行执行） |
| P1-11 | 增量抓取 | ✅ 已完成（`filter_unseen` 只分析新条目，无新增回退近 1 天存档） |
| P1-12 | 日报导出 | ✅ 已完成（`reports/YYYY-MM-DD-{keyword}.md`，目录已 gitignore） |
| P2-13 | 事件线与实体追踪 | ✅ 已完成（`events.py` 规则归并；`event_threads` / `event_links` 两表，`user_version=2`；轨迹面板与日报新增事件线段落） |
| P2-14 | API 化 | ✅ 已完成（`api.py`：`/api/health` `/api/status` `/api/keywords` `/api/alerts` `/api/threads` `/api/intel` `/api/runs` `/api/cost` `/api/storage` `/api/run` `/api/approve` `/api/callback/feishu`） |
| P2-15 | 审计 + 容器化部署 | ✅ 已完成（`alerts.reviewed_by` / `review_note`；`Dockerfile` + `docker-compose.yml`（ui/api/scheduler/caddy）+ `deploy/Caddyfile`）；API 侧 `API_TOKEN` 鉴权与 CORS 白名单已完成，Streamlit 页面仍只绑 `127.0.0.1` |
| P2-16 | 飞书卡片回调审批 | ✅ 已完成（卡片按钮 + `/api/callback/feishu`，兼容 `action.value` 与卡片 2.0 `event.action.value`） |
| P2-17 | 数据迁移 + 归档 | ✅ 已完成（`user_version=4`；`analyses` / `daily_intel_archive` / `llm_calls_archive` / `alerts.is_demo`；`--stats` / `--archive-days` CLI） |
| P3-18 | Prompt 注入防护 | ✅ 已完成（`agents/prompts.py` 边界包裹 + 注入句式清洗） |
| P3-19 | 密钥治理 | ✅ 已完成（`config.secret_status()` 掩码、`missing_secrets()`、`secret_warnings()`） |
| P3-20 | 日志文件化 | ✅ 已完成（`logging_setup.py`，`logs/pulse.log` 2MB × 3；`_tee_log` 双写且不重复落盘） |
| P3-21 | 按钮冷却 | ✅ 已完成（`RUN_COOLDOWN_SECONDS` 默认 90；UI 禁用 + 倒计时；API 429 + `retry_after`） |
| P3-22 | 分析结果持久化 | ✅ 已完成（`save_analysis` / `load_analysis`，刷新页面回放上次结果） |

### 10.1 验证记录

**2026-10-30（本轮 21 项提升完成后）**

- `pytest -q`：**131 passed**（19.95s），覆盖面新增 API 鉴权 / 参数校验 / 就绪检查 / 异步 409 / 飞书签名 / 审批原子抢占 / demo 过滤 / RAG 关键词隔离
- `ruff check .`：**All checks passed!**（`select = ["E","F","W","I"]`，`ignore = ["E501"]`，中文注释与 Prompt 长行交给编辑器）
- 新增用例（本轮）：`test_api_token_is_enforced_when_configured`、`test_api_token_accepts_bearer_scheme`、`test_invalid_query_param_returns_400`、`test_health_reports_component_status`、`test_async_run_returns_409_when_lock_is_alive`、`test_feishu_signature_is_verified`、`test_claim_alert_is_atomic`、`test_demo_alerts_can_be_filtered_from_history`、`test_health_reports_database_ready`、`test_fallback_query_filters_by_keyword`
- 手工回归点：`/api/health` 在 DB 不可用时返回 503；`POST /api/run` 异步分支在锁活跃时直接 409（不再 202 后静默失败）；飞书回调签名错误 403、verification token 错误 403

**2026-10-28（历史记录）**

- `pytest -q`：121 passed（当轮基线；后续新增用例后为 131）
- 事件线端到端（36Kr + Solidot + InfoQ）：11 条资讯归并为 11 条事件线，实体识别正常，日报含「事件线追踪」段落
- `events.py --keyword "AI Agent"`：可读出事件线实体、报道数与首末日期
- `compileall`：全部源码模块通过
- 真实端到端 run（arXiv + InfoQ + 36Kr + Solidot）：抓取 14 条、入库 14 条，Critic 一次通过，
  `[GUARD] heat_score` 由代码修正为 1580（模型给 1520），预警落 `pending`，日报文件生成
- Streamlit 启动：HTTP 200，`import app` 无异常（UI 布局未改动）
- 真实 `pulse.db`：`PRAGMA user_version = 3`（v1 唯一键 + v2 事件线 + v3 审计与归档）
- **P2/P3 联调（本轮）**：
  - `POST /api/run`（`wait=true`）真实跑通：`items=26`、`inserted=3`、`duration_ms=23309`、`mode=deepseek`，事件线 `linked=3 / new=2 / threads=3`，分析落库后 `/api/stats` 中 `analyses=1`
  - 90 秒内二次 `POST /api/run` 返回 **429** 且带 `retry_after`；`GET /api/status` 返回 `lock: null`、`cooldown_seconds: 90`
  - `GET /api/health` 200；`logs/pulse.log` 单行落盘无重复
  - `database.py --stats`：`daily_intel 84 / llm_calls 25 / analyses 1 / run_logs 22 / alerts 5`；`--archive-days 90 --dry-run` 0 条超期（逻辑正常）
  - Streamlit `AppTest` 无头渲染：`exceptions=0`、`error=0`，侧边栏「审批人」输入框存在，`PUBLIC_BASE_URL` 未配置时给出提示而非报错

### 10.2 代码状态

- P0 提交：`6ad6348`（并发锁、测试与 CI、UI 拆分、韧性、预警生命周期）
- P1 提交：`2761520`（数据源注册表、跨关键词归属、情感口径、防幻觉、多关键词调度、增量抓取、日报）
- P2-13 提交：事件线与实体追踪（`events.py` + 事件线两表 + 轨迹/日报段落）
- P2-14~17 + P3-18~22：上一轮工作区改动，**尚未提交**（等对接确认后再 commit）
- 2026-10-30 的 21 项可靠性 / 安全提升：本轮工作区改动，**尚未提交**
- 本地 `master` 较 `origin/master` 领先若干提交，**GitHub 推送因本机无法连通 github.com:443 未完成**
- `pulse.db`、`chroma_db/`、`reports/`、`logs/`、`data/`、`.env` 均在 `.gitignore` 中

### 10.3 2026-10-30 的 21 项提升（全部完成）

| # | 类别 | 改进项 | 关键位置 |
| --- | --- | --- | --- |
| 1 | P0 安全 | API Token 鉴权（`X-API-Token` / `Authorization: Bearer`，`hmac.compare_digest` 比较）；`/api/health` 与飞书回调豁免 | `api.py` `_authorized()` / `PUBLIC_PATHS` |
| 2 | P0 安全 | 飞书回调签名校验：`sha256(timestamp + nonce + encrypt_key + raw_body)`，另支持 verification token；失败 403 | `api.py` `_verify_feishu()` |
| 3 | P0 安全 | CORS 从 `*` 改为默认不发头，按 `CORS_ALLOW_ORIGIN` 白名单回显 | `api.py` `_cors_origin()` |
| 4 | P0 正确性 | 审批原子抢占：`claim_alert()` 用条件 UPDATE 把 `pending` 抢为 `sending`，10 分钟 stale 可回收，并发审批不重复推送 | `database.py` `claim_alert()` |
| 5 | P0 正确性 | 抓取预算改为共享截止时间（`wait(timeout=FETCH_BUDGET)`），慢源不再饿死后续源 | `fetcher.py` `fetch_all()` |
| 6 | P0 安全 | 记忆与今日资讯统一 `<<<UNTRUSTED_DATA>>>` 包裹并屏蔽注入句式 | `agents/prompts.py` |
| 7 | P1 质量 | RAG 多查询合并（收藏标题 + 今日标题）+ 按 `keyword` 过滤与降级路径同样过滤，避免跨关键词污染 | `rag/store.py` `query_similar()` |
| 8 | P1 质量 | 周同比改为「近 7 天 vs 上一个 7 天」窗口 | `rag/memory.py` `_week_over_week()` |
| 9 | P1 质量 | 热度公式改 800–2000 区间代码计算；正面占比与模型偏差 ≥20 个百分点时用代码统计覆盖 | `pipeline.py` `compute_heat()` / `compute_positive_ratio()` |
| 10 | P1 质量 | Prompt 与 Critic 契约对齐：`warning_text` 长度 30–200 字 | `agents/prompts.py` / `agents/critic.py` |
| 11 | P1 质量 | 情感词表收敛到 `sentiment.py` 单一来源，英文词按边界匹配（`risk-free` 不再误判） | `sentiment.py` |
| 12 | P1 质量 | 文本归一化 / 二元组 / 标题与 URL 规范化收敛到 `textutils.py`；URL 去跟踪参数 | `textutils.py` |
| 13 | P1 质量 | Webhook 对 429/5xx 指数退避重试 | `notify.py` |
| 14 | P2 体验 | Streamlit 查询加 `st.cache_data`，写操作后 `cache_data.clear()` | `app.py` |
| 15 | P2 体验 | 新增「审批历史」面板，展示最近 8 条与六态中英映射，demo 标记区分 | `app.py` `ALERT_STATUS` |
| 16 | P2 正确性 | 异步 `POST /api/run` 先预检运行锁，冲突直接 409（不再 202 后静默失败） | `api.py` `_handle_run()` |
| 17 | P2 正确性 | 查询参数统一整数校验与范围夹紧，非法值 400（不再 500） | `api.py` `_int_param()` / `do_POST` |
| 18 | P2 数据 | 演示预警加 `is_demo` 标记（`user_version=4`），审批历史可与真实预警区分 | `database.py` 迁移 v4 |
| 19 | P3 工程 | 依赖锁定：`requirements.txt` 固定版本 + `requirements-dev.txt` | `requirements*.txt` |
| 20 | P3 工程 | CI 增加 `ruff check .` 与 coverage 报告，配置收敛到 `pyproject.toml` | `.github/workflows/ci.yml` |
| 21 | P3 工程 | 容器：`TZ=Asia/Shanghai`、`PULSE_CHROMA_DIR=/data/chroma`、非 root 用户（uid 10001）、curl 健康检查 | `Dockerfile` / `docker-compose.yml` |

