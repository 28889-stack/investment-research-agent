# 开发关键节点

只记录具有架构意义或用户功能意义的节点，避免记录小修小补。

---

## 2026-08-07 文档对齐与现状快照
- 按源码核对并修正 README：行情段落补充新浪 `stock_zh_a_daily` 回退路径；API 表补充 `/api/runs/{run_id}/report/export` 单文件 HTML 报告导出；Docker 段落标注当前容器不含 Kronos Live 依赖（`requirements-kronos.txt`、`vendor/kronos`、`config/kronos-source.json`），`docker compose build/up` 未实机验证。
- 回填本日志缺失的 2026-08-06 架构节点（见下），来源为《项目迁移交接文档_2026-08-06》与当前代码。
- 现状快照：源码级 **MVP Code Complete**；组件级 Live 已验证 AKShare 行情/财务、Kronos-mini MPS 真实推理、巨潮公告检索；仍未配置真实 `DEEPSEEK_API_KEY`，故未完成真实 DeepSeek E2E 与 Docker Live（Kronos）验证；当前成果绝大多数仍为 untracked，尚未建立基线 commit/push。

## 2026-08-06 Pi 原生 DeepSeek provider 接入
- 配置 `PI_RUNTIME_MODE=live`、`PI_MODEL_PROVIDER=deepseek`、`PI_MODEL=deepseek-v4-flash`、`PI_API_KEY_ENV_NAME=DEEPSEEK_API_KEY`。
- Node Bridge 显式导入 `@earendil-works/pi-ai/providers/deepseek` 并调用 `deepseekProvider()`；自定义密钥变量安全映射为 SDK 约定的 `DEEPSEEK_API_KEY`，密钥不进入 JSONL 协议、SQLite、Artifact 或日志。
- Doctor 在受限 Bridge 子进程中解析 provider/model 做本地预检，不发起模型请求；已确认 `deepseek/deepseek-v4-flash` 可被本地 SDK 目录解析。
- 限制：因未配置真实 key，未发起 DeepSeek 网络 API，也未完成完整 technical/fundamental Live Run。

## 2026-08-06 Kronos Live 真实推理
- 按 `config/kronos-source.json` 锁定官方提交 `67b630e67f6a18c9e9be918d9b4337c960db1e9a`，源码置于 `vendor/kronos`（被 `.gitignore` 忽略），安装 `requirements-kronos.txt`，缓存 `NeoQuasar/Kronos-mini` 与 `Kronos-Tokenizer-2k`。
- `KRONOS_DEVICE=mps` 优先 Apple MPS，不可用时回退 CPU；predictor 按模型名 + 设备缓存；mini 模型使用 2k tokenizer 与 `max_context=2048`。
- Live 推理使用 daemon 单飞线程（原生调用超时无法安全强杀 CPython 线程），同时只允许一个 Live Kronos 推理；Live 失败不降级 Mock。
- 600519.SH 2 交易日预测成功，方向概率之和为 1.0，`model_version=NeoQuasar/Kronos-mini`。

## 2026-08-06 Result Manifest STALE 传播
- `result_manifest.json` 为每个基本面结果保存当前 `version`、`current/stale/failed`、输出文件 SHA256、固定上游输入 SHA 和更新时间。
- 上游输入 SHA 变化时，标记直接结果及固定下游为 `stale`，并从最早受影响节点在 LangGraph Checkpoint 上继续重建；成功重建后版本 +1，不保留旧文件副本。
- 依赖关系固定写在代码中，不使用动态依赖数据库；`stale` 报告不会被报告 API 当作当前正式结果返回。

## 2026-08-06 official_crawler 成为默认免密钥检索
- `RESEARCH_SEARCH_PROVIDER=official_crawler` 接入巨潮资讯 A 股公告：代码映射 `orgId`、调用公告查询端点、只接受 `finalpage/` 附件、沪深查询参数按证券代码确定、公告毫秒时间戳按 UTC+8 转换为披露日期。
- 无需 API Key；Tavily adapter 保留但默认不启用。
- 检索经封闭 `registry.py` 和 `ResearchSearchProvider` Protocol 接入，配置不能动态 import 任意代码；通用 URL 读取只允许 HTTP/HTTPS 标准端口，拒绝 localhost/内网/凭证，禁止自动重定向，仅对 `static.cninfo.com.cn/finalpage/` 启用系统代理兼容通道。

## 2026-08-06 基本面研究工作流可用
- 完成 Lead、Business、Industry、Financial、Valuation、Writer 工作流闭环。
- 建立 Evidence / Assumption 追踪与 Schema 严格校验。
- 真实 Live 任务完成端到端验证并生成正式报告。

## 2026-08-06 行情新浪回退
- AKShare 主路径 `stock_zh_a_hist` 返回空或失败时，回退新浪 `stock_zh_a_daily`；两条路径都失败才报错，不回退 Mock。
- 标准列与 `validate_market_data` 校验（≥120 根、升序不重复、OHLC 合法、不越过 `as_of`）对两条路径一致。

## 2026-08-06 巨潮公告检索降级
- 支持自然语言问题到公告标题关键词的分层回退。
- 支持单次请求失败后继续尝试其他候选词。
- 支持空关键词近期公告兜底。

## 2026-08-06 Agent Runtime 错误恢复
- 普通工具错误支持模型修正和降级。
- 权限、预算和协议错误保持硬失败。
- 工具参数通过 JSON Schema 枚举约束（evidence_type）。

## 2026-08-06 单文件 HTML 报告导出
- 支持技术面和基本面报告导出。
- 内联 CSS 与技术图表 Base64。
- 不依赖脚本、静态资源或运行时 API 请求。

---

## 2026-08-09 多源检索聚合层（aggregator）

### 架构意义
检索层从单一 provider 升级为统一多源聚合模型，这是 2026-08-06 待办里"检索层从单一巨潮扩展为统一多源 Provider"的落地。

### 来源边界
- 巨潮公告（`official_crawler` / cninfo）：A 股正式披露公告，`source_kind=announcement`。
- AKShare 东方财富新闻（`akshare_news`）：`stock_news_em`，**预取正文**写入 `content`，读取路径直接使用、不触发 SSRF/HTTP。
- AKShare 东方财富研报（`akshare_reports`）：`stock_research_report_em`，仅元数据 + pdf.dfcfw.com PDF 链接，读取时下载 PDF。
- AKShare 东方财富公告（`akshare_notices`）：`stock_individual_notice_report`，仅元数据 + data.eastmoney.com HTML 链接，读取时下载 HTML。
- Tavily（`tavily`）：通用 Web 检索，需 API Key，独立 adapter（从 evidence.py 内联迁移而来）。
- Firecrawl（`firecrawl`）：通用 Web 检索，需 API Key。

### 聚合层行为（aggregator.py）
- **Fan-out**：单 `search_research_sources` 工具 fan-out 到 `RESEARCH_SEARCH_PROVIDERS` 列表的全部来源。
- **失败隔离**：每个来源独立 try/except，`ResearchSourceError` 与任意 `Exception` 都只记录、跳过该来源；**仅当全部来源失败且无任何结果时才报错**（`全部检索来源失败`）。
- **跨源去重**：`_dedup_key(url)` = `小写host | 去尾斜杠path`，丢弃 query/fragment，首见保留。
- **重排**：两次稳定排序——先按 `date` 降序（空日期排尾），再按 `_SOURCE_KIND_PRIORITY` 升序（announcement 0 > research_report 1 > news 2 > financial 3 > technical 4 > web 5，未知 9）。优先级**硬编码**，不读配置，防止被篡改配置降权权威披露。
- **截断 + 重编号**：截断到 `max_results` 后稳定重编号 `src_001..src_NNN`，保证下游 `read_research_source` 可按 `result_id` 回查（无论来自哪个来源）。

### 封闭注册表（registry.py）
`get_search_provider` 维持封闭 if-chain，不能动态 import 任意代码；`aggregator` 自身从 `RESEARCH_SEARCH_PROVIDERS` 环境变量解析子来源，**拒绝 `aggregator` 自身出现在子列表**（防递归）。

### SSRF 简化（evidence.py）
删除原 IP-pinning / peer-validation / 域名 allow-list 过度工程，改为显式本机段黑名单 `_BLOCKED_NETWORKS`：127/8、10/8、172.16/12、192.168/16、169.254/16、0/8、::1/128、fe80::/10、fc00::/7。**刻意不含 198.18.0.0/15**——这是本机 Fake-IP 代理段，实际经系统代理路由到公网；用显式段而非 `is_private`/`is_global`，因为 198.18.x.x 同时是 `is_private=True` 且 `is_global=False`，泛 `is_private` 过滤会误杀代理流量。`read_research_source` 优先用 source 已有 `content`，否则 `is_safe_public_url` 单次校验后单次下载（`trust_env=True`、`follow_redirects=False`、流式 + 大小上限、`_retry_http` 单次重试）。

### 预算放宽
`max_iterations` / `max_tool_calls` schema 上限 `le=5` → `le=12`；`max_tool_calls_per_node` 默认 5 → 8；三个基本面 profile 改 8/8；运行时上限 = `min(profile.max_tool_calls, settings.max_tool_calls_per_node)`。多源 fan-out 不再因预算耗尽而中断。

### Live 启用（readiness.py + .env.live.example）
`.env.live.example` 改 `RESEARCH_SEARCH_PROVIDER=aggregator` + `RESEARCH_SEARCH_PROVIDERS=official_crawler,akshare_news,akshare_reports,akshare_notices`。`readiness.py` 新增 `_LIVE_SEARCH_PROVIDERS` 集合 + `_research_search_ready` 校验：aggregator 要求子来源非空、且每个成员必须在白名单内、且不能是 `aggregator` 自身；tavily/firecrawl 仍要求 API Key 环境变量。Live 失败明确报错，不降级 Mock。

### 测试
- 新增 `test_akshare_retrieval.py`（15 项：新闻预取/去市场后缀/重试/空帧/跳过缺列、研报元数据/备用列名、公告关键词过滤/短查询回退、共享 helper）。
- 新增 `test_tavily_retrieval.py`（6 项：公网来源/重试/401 不重试/5xx 重试/缺 Key/全私网结果）。
- 新增 `test_aggregator_retrieval.py`（18 项：fan-out、失败隔离含非预期异常、全失败、空配置、去重、重排优先级/日期/跨 kind 稳定/空日期排尾、截断、重编号、content 透传、未知 kind 默认优先级）。
- `test_fundamental_core.py` 新增黑名单测试：验证 `_BLOCKED_NETWORKS` 不含 198.18，并 sanity 断言该段确为 `is_private=True`。
- 修复 `test_fundamental_tools.py`：Tavily 测试改 patch `app.retrieval.tavily.httpx.Client`，删除已删函数 `_pinned_request_target` 的 monkeypatch，allowlist 测试改为 unsafe URL 拒绝测试。
- 修复 `test_config_validation.py`：新增 aggregator 配置校验三项。
- `test_fundamental_live.py`：读 `RESEARCH_SEARCH_PROVIDER`/`RESEARCH_SEARCH_PROVIDERS` 环境变量，仅 tavily/firecrawl 要求 Key。
- 全量 `.venv/bin/python -m pytest`：316 passed, 4 skipped。

### 待办 / 后续方向
- **P0 保全工作区**：检查全部 untracked 文件，排除运行数据/密钥/缓存/个人文件后，建立经用户确认的基线 commit/push（当前仅 2 个初始提交，绝大多数源码 untracked，是最高迁移风险）。
- **P1 完整 Live E2E**：由用户在本机 `.env` 自行填写 `DEEPSEEK_API_KEY`（不在聊天/Git 中传输），停 Worker 后运行 `live_smoke.py --technical/--fundamental`，人工核对报告 Evidence/财务/估值/Kronos 解释与禁止交易指令，再更新 `MVP_ACCEPTANCE.md` 与 `RELEASE_CHECKLIST.md`。
- **P2 修复 Docker Live**：决定容器是否安装 PyTorch/Kronos；如支持需在 Dockerfile 补 `requirements-kronos.txt`、复制并锁定官方源码、设计模型缓存预热，并在 Docker Linux 使用 CPU/GPU runtime（MPS 不可用）；再实机完成 `docker compose config/build/up` 与数据卷验收。
- 多源需支持故障隔离、跨源去重排序、来源可信度与结构化 missing_information。（**已于本次落地**，见上。）
- 后续可继续扩展来源：腾讯财经、交易所、公司 IR。

---

## 2026-08-09 aggregator Live 端到端验收 + 受信财务边界修正

### 架构意义
aggregator 多源检索在真实 DeepSeek Live 下首次端到端跑通并生成正式报告（600519.SH），同时暴露并修复了两类隐患：受信 Python 财务边界与 `lead_final_review` 的 context/依赖不一致；aggregator 失败隔离会静默掩盖恒失败子来源。两者都不改变权威边界——full agent 仍不得改写财务数字。

### lead_final_review 财务权威边界修正（workflow.py + result_manifest.py）
- **现象**：aggregator Live 卡在 `HUMAN_REVIEW_REQUIRED`——Lead 把"核心财务数字未在 evidence 摘录引用"判为缺失信息。
- **根因**：`_lead_final_review` 的 `context_refs` 只含 `financial_research`（叙述），不含 `financial_data`/`financial_metrics`（受信 Python 边界计算的权威数字）；`DEPENDENCIES["lead_final_review"]` 同样缺这两项，staleness 传播与 context 不一致。Lead 看不到权威财务来源，遂将财务数字的"缺失"误判为阻断。
- **修复**：`context_refs` 增加 `artifact:financial_data` + `artifact:financial_metrics`；task prompt 显式声明二者是受信 Python 边界的权威财务数据、`financial_research` 是其已校验叙述、核心财务数字以这三者为准无需在 evidence 重复引用、各 approved_sections 均有已校验研究支撑时置 `ready_for_writer=true`；`DEPENDENCIES` 同步补齐两项目使 staleness 一致。`_fundamental_writer` 的 gate 与权威边界不动。
- **架构含义**：明确"evidence 仅用于业务/行业/风险等定性披露的来源佐证；财务数字以受信 Python 边界为准"——这是权威边界在最终审核节点的延伸声明，防止 Lead 重复校验已受信数据。

### akshare_notices 参数颠倒与失败隔离的静默掩盖
- **现象**：`akshare_notices` 对 600519.SH 恒抛 `RESEARCH_SOURCE_FAILED`（根因 `KeyError '600519'`），被 aggregator 失败隔离跳过，表面"aggregator 健康"但该子来源从未真正贡献，smoke 仍能 COMPLETED（仅靠 official_crawler 等其余来源）。
- **根因**：`AkshareNoticeProvider.search` 调用 `stock_individual_notice_report(security="A股", symbol=code)`，参数颠倒——akshare 中 `symbol` 是公告类别（固定映射），`security` 才是股票代码；代码进 `symbol` → `report_map["600519"]` KeyError。
- **修复**：改为 `security=code, symbol="全部"`（按公司过滤、类别全选），再用既有客户端关键词过滤收窄。
- **架构含义**：aggregator 的"失败隔离 + 仅全失败才报错"模型健壮但有盲区——一个**恒失败**子来源会被静默吞掉，聚合层层面无法区分"该来源今天没数据"与"该来源调用方式坏了"。本次靠单 provider 直调诊断才定位。后续可考虑在诊断/日志层为"连续 N 次恒失败同一来源"打告警标记，但不改变"失败不阻断聚合"的主语义。

### OUTPUT_DIAGNOSTIC_DIR 诊断钩子首次在真实 Live 失败下验证
- `pi_adapter._write_validation_diagnostic`（env-gated，仅写失败、redact、只含模型生成的研究文本、绝不写密钥）在 smoke #2 真实捕获两处可修复校验失败：`lead_planning` `JSON_INVALID`（raw_output 被 ```json fence 包裹）、`industry_research` `FORBIDDEN_FIELD`（finding 对象含禁止字段名）；两者均经一次 repair 成功修复后 COMPLETED。这闭合了此前"REPAIR_FAILED 在 DB 仅留通用消息、raw_output 丢失"的取证缺口，且证明钩子不触发误降级 Mock。

### 验收结果
- Live smoke #1（run 3e88c330，notices 修复前）：COMPLETED，evidence 6 / assumption 3，evidence 全部巨潮资讯（akshare_notices 静默失败中），`ready_for_writer=True`（gate 修复生效，missing_information 仅含颗粒度缺口而非财务数字"缺失"）。
- Live smoke #2（run eaa945fc，notices 修复后）：COMPLETED，duration 152.574s，evidence 7 / assumption 5，evidence 全部来自 `akshare_notices` 的 data.eastmoney.com 公告，`missing_information=0`，writer.status=completed，report 无禁止交易指令，manifest 全 current。
- 离线：全量 `.venv/bin/python -m pytest` 316 passed, 4 skipped。

### 待办 / 后续方向
- 决定是否将 `OUTPUT_DIAGNOSTIC_DIR` 钩子作为常驻诊断手段保留（当前倾向保留：opt-in、仅失败、redact、已验证）。
