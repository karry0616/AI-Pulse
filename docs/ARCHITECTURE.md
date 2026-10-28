# AI Pulse 架构与代码库分析报告

> 分析对象：本仓库全部源码（19 个 Python 文件，约 2772 行，不含 README 与依赖清单）
> 分析方式：全量源码通读 + 调用链追踪 + 依赖关系核对
> 更新时间：2026-10-28

---

## 一、先给结论

| 维度 | 评分 | 一句话评价 |
| --- | --- | --- |
| 功能闭环 | 9 / 10 | 从抓 RSS 到人工审批推送，全链路真跑通，不是 Mock |
| 代码分层 | 8 / 10 | 依赖单向、无循环导入，职责边界清楚 |
| 工程质量 | 5 / 10 | 零测试、零 CI、入口文件过大 |
| 生产可用度 | 4 / 10 | 无鉴权、无并发锁、无监控、无部署形态 |
| 可扩展性 | 6 / 10 | 数据源 / 关键词 / 阈值全是硬编码，加一个源要改源码 |

**定位判断**：这是一个「完整度很高的演示级 MVP + 半个生产项目」。它已经能支撑一场技术面试的完整讲解，但要交给别人真正每天使用，还差三块：**并发安全、事实校验、可运维性**。

**如果只做 5 件事**：
1. 加并发锁（防止重复扣 token、重复写库）
2. 补 pytest + GitHub Actions（把现有纯函数变成可验证资产）
3. 拆分 980 行的 `app.py`（CSS / 图表 / 轨迹面板各自独立）
4. 修「跨关键词新闻归属丢失」这个数据模型坑
5. 把 `heat_score`、`top_news` 从「模型自由发挥」改成「代码算 + 模型解释」

---

## 二、项目全貌

### 2.1 目录结构与体量

```
AI Pulse/
├── app.py                980 行   Streamlit 页面（CSS 380 行 + 图表 + 轨迹 + HITL）
├── pipeline.py           154 行   一次 run 的总编排
├── database.py           365 行   SQLite 四张表 + 全部 SQL
├── fetcher.py            200 行   RSS 抓取、清洗、关键词匹配、规则情感
├── notify.py             105 行   钉钉加签 / 飞书卡片 Webhook
├── scheduler_service.py   65 行   APScheduler 每日 08:00 独立进程
├── config.py              26 行   自己写的 .env 加载器
├── agents/               452 行   state / prompts / collector / analyst / critic / graph
├── llm/client.py         148 行   DeepSeek JSON Mode + token 计量
├── rag/store.py          227 行   Chroma 向量库 + 关键词降级检索
├── rag/memory.py          48 行   历史召回上下文 + 周同比
├── docs/                         本目录：架构与迭代文档
├── README.md              90 行
└── requirements.txt        7 行
```

体量分布很说明问题：**`app.py` 占了全部代码的 35%**，其中 380 行是 CSS 字符串。这既是优点（UI 好看），也是最大的维护负担。

### 2.2 技术选型与评价

| 层 | 选型 | 选得对吗 | 代价是什么 |
| --- | --- | --- | --- |
| 界面 | Streamlit 1.49+ / Plotly 6 | ✅ 对。纯 Python 出效果，零前端成本 | 无鉴权、长任务阻塞页面、`session_state` 刷新即丢 |
| 大模型 | DeepSeek + 裸 `requests` | ✅ 对。JSON Mode 稳定、便宜 | 没有重试 / 退避、没有流式、同步阻塞 |
| 存储 | SQLite（WAL）+ pandas | ✅ 对。零运维、单机最合适 | 单写锁、无迁移机制、无归档 |
| 向量 | ChromaDB + **自研 512 维字符 n-gram 哈希嵌入** | ⚠️ 有取舍。完全离线，不下载模型 | 只有字面相似度，同义词召回弱（"大模型"召不到 "LLM"） |
| 调度 | APScheduler 3.x（BlockingScheduler） | ✅ 对。够轻、够用 | 任务不持久化、只能单关键词、重启即重来 |
| 推送 | requests + HMAC-SHA256 加签 | ✅ 对 | 无重试、无飞书回调按钮 |
| 配置 | 自写 `.env` 解析（不引 python-dotenv） | ✅ 对，展示基本功 | 无字段校验、无类型检查 |

> 纠正一处 README 与口头描述的偏差：**RAG 并没有用 ONNX MiniLM 模型**，`rag/store.py` 的 `_embed_text()` 用的是字符 1/2-gram 哈希 + 512 维桶向量。这是完全离线方案，好处是永远不会因下载模型失败，坏处是「语义」很弱。

### 2.3 依赖关系图（已核对：无循环导入）

```
app.py ─┐                    ┌─→ fetcher ──→ database
        ├─→ pipeline ────────┤
scheduler_service.py ─┘      ├─→ rag.store ──→ chroma / database
                             ├─→ rag.memory ──→ rag.store / database
                             └─→ agents.graph
                                    ├─ collector ─┐
                                    ├─ analyst ───┼─→ llm.client ──→ DeepSeek / database
                                    ├─ critic ────┘
                                    └─ prompts
                             pipeline.push_alert ──→ notify ──→ config
database / config 被所有人依赖，但自己谁也不依赖（叶子节点）
```

这个结构是**合格的分层**：入口（UI / 调度）→ 编排（pipeline）→ 能力（agents / rag / fetcher）→ 基础设施（llm / database / config）。没有一处 `database` 反过来 import `pipeline`，这是很多人写小项目时最容易犯的错，这里没犯。

### 2.4 一次完整 run 的调用链

```
pipeline.run(keyword, sources)                                   # pipeline.py:29
  ├─ database.start_run()                      写 run_logs（status=running）
  ├─ fetcher.fetch_all()                       4 个源串行抓，单源失败自动回退
  ├─ database.save_items()                     INSERT OR IGNORE 去重
  ├─ [无新数据] database.fetch_recent(1天)     兜底用存档继续分析
  ├─ rag_store.backfill(7天) + index_items()   SQLite → Chroma 幂等 upsert
  ├─ rag_memory.build_context()
  │    ├─ store.query_similar()                7 天窗口 cosine 召回，失败降级
  │    └─ _week_over_week()                    SQLite 算周同比（禁止模型自算）
  ├─ agents.graph.run()                        ★ 核心
  │    ├─ collector.run()                      LLM 抽事件 / 规则降级
  │    ├─ for attempt in 1..3:
  │    │    ├─ analyst.run()                   LLM 出 7 字段 JSON
  │    │    └─ critic.check_contract()         契约校验，失败把 issues 回灌
  │    └─ critic.finalize()                    数值归一 + 默认值兜底
  ├─ [negative_alerts > 0] database.save_alert()   只暂存 pending，不推送
  ├─ database.usage_summary()                  本 run 的 token / 耗时
  └─ database.finish_run()                     回写 success / failed
```

注意最后一步的分岔：**推送不在这个流程里**。真正发送要等用户在页面上点「审核并发送」，走 `pipeline.push_alert()` → `notify.send_webhook()` → 回写 `alerts.status`。这条设计是整个项目最值得讲的部分之一。

---

## 三、已实现的核心能力盘点

| # | 能力 | 实现位置 | 成熟度 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 多源 RSS 采集 | `fetcher.py` `SOURCES` | ●●●●○ | 4 个逻辑源、单源重试 1 次、机器之心失效自动回退量子位 |
| 2 | 关键词中英双语匹配 | `fetcher.py` `KEYWORD_ALIASES` | ●●●●○ | "AI Agent" 能同时命中 `agentic` / `智能体` |
| 3 | 入库与去重 | `database.py` `save_items()` | ●●●●○ | `(date, title)` 唯一 + WAL + 5 个索引 |
| 4 | 向量记忆 + 降级检索 | `rag/store.py` `query_similar()` | ●●●○○ | 双层降级：Chroma 挂了退 Jaccard 字符二元组 |
| 5 | 周同比 | `rag/memory.py` `_week_over_week()` | ●●○○○ | 思路对（代码算、模型只能引用），但口径脆弱 |
| 6 | 三 Agent 状态机 | `agents/graph.py` `run()` | ●●●●○ | 60 行实现 State / Node / 回边，替代 LangGraph |
| 7 | 契约校验 + Reflexion | `agents/critic.py` `check_contract()` | ●●●●○ | 8 类打回规则，最多 3 次，超限规则降级 |
| 8 | 全链路降级 | analyst / collector / store 三处 | ●●●●● | 无 Key / 网络挂 / JSON 坏 / 向量挂，页面都不白屏 |
| 9 | 成本可观测 | `llm/client.py` `chat()` | ●●●●○ | 每次调用按 `run_id` 落 `llm_calls`，轨迹面板可查 |
| 10 | HITL 审批闸门 | `pipeline.push_alert()` + `app.py` | ●●●●○ | `pending / sent / failed` 三态，失败可重发 |
| 11 | 定时任务 | `scheduler_service.py` | ●●●○○ | 独立进程、`max_instances=1`、`coalesce` + 1h 补跑 |
| 12 | 双渠道推送 | `notify.py` | ●●●○○ | 钉钉 HMAC 加签 + 飞书交互卡片，自动识别渠道 |

---

## 四、真正的技术亮点

1. **把「结构校验」做成了 LLM 的质检门。** 大多数同类项目直接 `json.loads` 完就往页面上怼，模型少个字段就白屏。这里的 Critic 校验了 8 件事，其中一条特别聪明：`negative_alerts` 不能超过输入资讯条数——**这是唯一一条真正触碰「事实」的约束**，其余都是格式约束。

2. **降级不是一句口号，是三层各自兜底。** 没 Key → 规则引擎；JSON 非法 → 打回重算；Chroma 挂 → 字符重合度检索。任何一层挂了，用户看到的仍然是完整页面 + 明确原因。**这是演示场景最关键的护城河**，因为现场网络永远不可靠。

3. **自研 60 行状态机，而不是硬套框架。** `agents/graph.py` 的 `for ... else` 写法把「重试」和「重试耗尽」表达得非常干净。这在面试里比「我用了 LangGraph」更有说服力——说明你理解编排的本质，而不是只会调包。

4. **成本账本。** `llm_calls` 表按 `run_id + node` 记录 token 与耗时，终端还会打 `[COST]`。数据库层已经有 `usage_summary()` 和 `llm_calls_for_run()` 两个聚合函数——**数据已经存好了，只是还没做汇总页面**，这是最容易摘的果子。

5. **风险动作必须人工闸门。** 预警只落库、不推送，天然符合「AI 系统不能让模型直接对真人广播」的安全设计原则。而且审批状态是真的持久化在 SQLite，跨 Streamlit 会话、跨刷新都还在。

6. **XSS 防护是做了的。** `app.py` 里所有来自 LLM 和 RSS 的文本，进 HTML 前都过了 `html.escape()`。很多 Streamlit 项目直接 f-string 拼 HTML，这里没有。

---

## 五、技术短板（按严重程度分级）

### P0 —— 会直接出事

| 编号 | 问题 | 现象与影响 | 证据 | 修复方向 |
| --- | --- | --- | --- | --- |
| A1 | **没有并发锁** | 手动点按钮 + 定时任务 + 两个浏览器会话，可以同时跑同一个 keyword：重复扣 token、SQLite 写竞争、Chroma upsert 打架、`alerts` 重复生成 | `pipeline.run()` 全程无锁；scheduler 的 `max_instances=1` 只约束它自己 | 文件锁 + 「同 keyword 冷却 N 分钟」 |
| A2 | **没有鉴权** | 任何能访问 8501 端口的人都能触发花钱、都能审批推送 | Streamlit 裸跑 | `st.login` / 密码门 + 只监听 127.0.0.1 |
| A3 | **零测试、零 CI** | 契约校验这种纯函数是最该被测的，现在只能靠 `--force-bad` 手工验；改一行代码不知道有没有退化 | 全库无 `tests/` | pytest 覆盖 critic / fetcher / database 三处 |
| A4 | **app.py 是上帝文件** | 980 行里 380 行 CSS、2 个图表函数、轨迹面板、侧边栏、主布局、HITL 按钮全挤在一起；而且 Streamlit 热重载不刷新被依赖模块，改图表必须重启进程 | `app.py` 全文 | 拆成 `ui/style.css` + `ui/charts.py` + `ui/trace.py`（视觉零改动） |

### P1 —— 影响正确性与数据质量

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

- **无数据迁移体系**：改表结构只能手工 `ALTER`，schema 没有版本号
- **无留存策略**：`daily_intel`、`llm_calls` 只增不减
- **日志只打 stdout**：20 条会话日志，刷新就没；没有文件日志与轮转
- **Prompt 注入**：RSS 标题 / 摘要原样进 Prompt，恶意标题可诱导模型偏离契约
- **密钥明文**：`.env` 里存 API Key 与 Webhook
- **无部署形态**：Docker、守护进程、HTTPS 全没有
- **推送无重试**：Webhook 失败只记 `failed`，靠人再点一次

---

## 六、可开发方向（价值 / 难度 / 收益）

难度说明：★ = 半天到一天；★★ = 一个模块级改动；★★★ = 跨模块重构；★★★★ = 需要新架构能力。

### 6.1 功能扩展

| 编号 | 方向 | 做什么 | 价值 | 难度 | 预期收益 |
| --- | --- | --- | --- | --- | --- |
| F1 | **多关键词并行任务** | scheduler 按关键词注册多个 job，一次跑完 4 个关键词 | ★★★★ | ★★ | 从「单关键词玩具」变成「真情报台」 |
| F2 | **数据源插件化 + 扩源** | `config/sources.py` 注册表，新增 arXiv、Reddit、GitHub Trending、Product Hunt、X API | ★★★★★ | ★★ | 情报价值 = 覆盖率；同时消灭 Twitter/X 幽灵源 |
| F3 | **日报 / 周报导出** | 一键导出 Markdown / PDF / 飞书文档；周报做 7 天聚合 | ★★★★ | ★★ | 产出「能发出去的东西」 |
| F4 | **事件线与实体追踪** | 相似新闻合并为同一事件，跟踪「公司-模型-融资/事故」时间线 | ★★★★★ | ★★★★ | 从「新闻摘要」升级为「事件跟踪」 |
| F5 | **预警规则中心** | 自定义负面词、阈值、z-score 热度突增检测 | ★★★★ | ★★★ | 降低误报 |
| F6 | **飞书卡片回调审批** | 卡片带按钮，点「通过/驳回」直接改状态（需 FastAPI 回调端点） | ★★★ | ★★★ | 真正的移动端 HITL |

### 6.2 性能与成本

| 编号 | 方向 | 做什么 | 价值 | 难度 | 收益 |
| --- | --- | --- | --- | --- | --- |
| P1 | **增量抓取游标** | 每个源记 `last_seen`，只把新条目送进 LLM | ★★★★ | ★★ | token 与耗时降 30%–70% |
| P2 | **并行抓取** | `ThreadPoolExecutor` 抓 4 个源 | ★★★ | ★ | 抓取时长压到接近单源耗时 |
| P3 | **当日结果缓存** | 同 keyword 同日重复点击直接回放上次结果 | ★★★ | ★ | 演示时不怕手抖连点 |
| P4 | **检索质量升级** | 哈希向量 + BM25 混合召回（RRF 融合），或换本地小语义模型 | ★★★★ | ★★–★★★ | 召回从「字面重合」升级到「意思相近」 |
| P5 | **LLM 调用治理** | 429 退避重试、单节点 30s 超时、run 级总预算 | ★★★ | ★ | 稳定性 + 费用上限 |

### 6.3 体验提升

| 编号 | 方向 | 价值 | 难度 | 收益 |
| --- | --- | --- | --- | --- |
| U1 | **结果持久化**：分析结果落库（`reports` 表），刷新不丢 | ★★★★ | ★★ | 修复刷新页面后报告变空 |
| U2 | **节点级进度可视化**：Collector / Analyst / Critic 逐节点点亮 | ★★★ | ★★ | 演示观感提升明显 |
| U3 | **KPI 下钻**：点「今日入库 32 条」弹出原始新闻列表 | ★★★ | ★★ | 让数据可追溯 |
| U4 | **一键复制 / 分享报告** | ★★ | ★ | 低成本高感知 |

### 6.4 生态集成

| 编号 | 方向 | 价值 | 难度 | 收益 |
| --- | --- | --- | --- | --- |
| E1 | **API / MCP 化**：把 `pipeline.run` 暴露成 HTTP 接口或 MCP Server | ★★★★★ | ★★★ | 从「一个网页」变成「一个能力节点」 |
| E2 | **飞书 / 钉钉双向**：群内输入「今日 AI 情报」即返回摘要 | ★★★★ | ★★★ | 用户不用打开网页 |
| E3 | **日报自动写入飞书文档 / 多维表格** | ★★★ | ★★ | 沉淀知识资产 |
| E4 | **GitHub Actions 每日跑 + Pages 展示** | ★★★ | ★★ | 零成本托管 demo |

### 6.5 安全加固

| 编号 | 方向 | 价值 | 难度 | 收益 |
| --- | --- | --- | --- | --- |
| S1 | **登录鉴权** | ★★★★ | ★ | 从「谁都能点」到「只有我能点」 |
| S2 | **审计日志**：`reviewed_by` / `review_note` / 操作时间 | ★★★ | ★ | 审批可追责 |
| S3 | **Prompt 注入防护**：不可信内容分隔符包裹 + 输出白名单 | ★★★ | ★★ | 防「新闻标题操控模型」 |
| S4 | **密钥治理**：启动时校验必填项 | ★★ | ★ | 避免把 `sk-` 提交上去 |
| S5 | **按钮冷却 + run 配额** | ★★★ | ★ | 与并发锁一起做 |

---

## 七、优先级路线图

### P0 · 立即做（低难度、高收益、不改视觉）

1. **并发锁 + 按钮冷却** → 验收：两个浏览器同时点，只有一个真正执行，另一个提示「任务进行中」
2. **pytest 骨架 + GitHub Actions** → 验收：覆盖 8 类契约打回，CI 绿
3. **拆分 app.py** → 验收：`ui/style.css` / `ui/charts.py` / `ui/trace.py`，页面与现在逐像素一致
4. **Chroma 降级可恢复 + LLM 退避重试** → 验收：首次失败后第 2 次调用能恢复
5. **预警去重 + `expired` / `ignored` 状态** → 验收：同 keyword 同日最多 1 条待审

### P1 · 下一阶段（把「能演示」变「能天天用」）

6. 数据源注册表 + 新增 3 个源
7. 跨关键词归属修复
8. 情感口径统一
9. 防幻觉补丁（`heat_score` 代码化 + `top_news` 标题核验）
10. 多关键词调度
11. 增量抓取
12. 日报导出

### P2 · 战略级（差异化与生态）

13. 事件线与实体追踪
14. API / MCP 化
15. 鉴权 + 审计 + Docker / HTTPS 部署
16. 飞书卡片回调审批
17. 数据迁移 + 归档留存

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

**第一批（先把地基焊死）**：并发锁 → 测试 → 拆 app.py
这三件做完，项目就从「能跑」变成「敢改」。

**第二批（把数据弄准）**：跨关键词归属 → 情感口径 → 防幻觉补丁
这三件做完，页面上的数字才真正可信。

**第三批（把价值放大）**：源注册表 → 多关键词 → 日报导出 → API / MCP
这批开始才是真正的产品化。

**衡量指标建议**（做完 P0 / P1 后应该达到）：
- 重复 run 率 = 0
- Critic 一次通过率 > 80%（当前未统计）
- 单 run token < 4000、时长 < 20s
- 预警人工判定误报率 < 20%
- 接入源 ≥ 7 个

---

## 十、实施进度

| 阶段 | 项目 | 状态 |
| --- | --- | --- |
| P0-1 | 并发锁 | ✅ 已完成（`pipeline.run_lock`，30 分钟残留锁可抢占） |
| P0-2 | pytest 骨架 + CI | ✅ 已完成（38 个用例，`.github/workflows/ci.yml`） |
| P0-3 | 拆分 app.py | ✅ 已完成（`ui/style.css` · `ui/charts.py` · `ui/trace.py`，视觉零改动） |
| P0-4 | Chroma 降级可恢复 + LLM 重试 | ✅ 已完成（5 分钟冷却重试；429/5xx 两次退避重试） |
| P0-5 | 预警去重 + 过期状态 | ✅ 已完成（同日同关键词复用；24 小时未审核自动 `expired`） |
| P1 | 6–12 项 | ⏳ 待开始 |
| P2 | 13–17 项 | ⏳ 待开始 |

