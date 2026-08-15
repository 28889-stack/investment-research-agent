# 金融投研 Agent

面向中国 A 股个股研究的本地 Web 应用。当前包含 `technical_v1` 技术面流程和 `fundamental_v1` 基本面正式报告流程。两条流程独立运行，不生成综合评分或交易指令。

## 已实现调用链

```text
POST /api/runs
→ SQLite research.db
→ 独立 Worker 按 analysis_type 分流

technical → technical_v1
  resolve_security
  → technical_research（Full Pi Agent + 3 个技术工具）
  → kronos（独立 Python 模块，不使用 Pi、不读取 Agent 文字）
  → technical_assembly（Constrained Pi Agent，0 工具）
  → write_report

fundamental → fundamental_v1
  resolve_security
  → lead_planning
  → business_research
  → industry_research
  → lead_review
  → deep_research
  → assemble_retrieval_package
  → financial_research
  → valuation_research
  → lead_final_review
  → lead_synthesis（Constrained Pi Agent，0 工具）
  → writer_planning（Constrained Pi Agent，0 工具）
  → fundamental_writer（Constrained Pi Agent，0 工具）
  → write_fundamental_report
```

检索节点把来源正文统一沉淀在 `evidence.json`；`retrieval_package.json` 仅提供可审计的限长索引。研究节点返回结论、论证和 Evidence ID，Lead 另行生成报告主线与资料使用范围，Writer Plan 与 Writer 均不调用工具。最终产物为可离线打开的单文件 `fundamental_report.html`，内嵌原生 Canvas 交互图表；资料待补充事项只在末尾“优化建议”板块展示，不会打断正文。

LangGraph Checkpoint 保存在 `data/checkpoints.db`，`thread_id = run_id`。任务事实、事件、AgentExecution 和 ToolExecution 保存在 `data/research.db`。Worker 总是优先恢复未完成任务，再领取新的 `CREATED` 任务。

## 环境要求与安装

- Python 3.11+
- Node.js 22.19.0+
- npm

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd pi_bridge
npm install
npm run build
cd ..

cp .env.example .env
```

主要 Python 依赖包括 FastAPI、SQLAlchemy、LangGraph、pandas、NumPy、matplotlib 和 AKShare。技术指标不依赖 TA-Lib。

## Mock 模式启动

默认配置可完全离线运行行情、Kronos 和 Pi Bridge Mock：

```bash
export PI_RUNTIME_MODE=mock
export MARKET_DATA_MODE=mock
export KRONOS_MODE=mock
export FUNDAMENTAL_DATA_MODE=mock
export RESEARCH_SEARCH_MODE=mock

source .venv/bin/activate
uvicorn app.main:app --reload
```

另开终端：

```bash
source .venv/bin/activate
python -m app.worker
```

打开 <http://127.0.0.1:8000>，输入 `600519`、`600519.SH`、`SH600519` 或 `贵州茅台`，选择技术面或基本面分析。Mock 模式不需要外部数据服务、搜索 API Key 或真实 LLM。

## 运行配置与启动前检查

`.env.example` 是全离线 Mock 配置，`.env.live.example` 是已选定的单机 Live 配置：Pi 原生 DeepSeek provider、AKShare、Kronos-mini 和免密钥的巨潮资讯检索。模板不包含任何密钥；API Key 只从环境变量传入，不得写入 SQLite、Artifact 或日志。

Live 准备：

```bash
source .venv/bin/activate
python scripts/setup_kronos.py
npm --prefix pi_bridge run build
cp .env.live.example .env
# 仅在本机 .env 中填写 DEEPSEEK_API_KEY，不要提交该文件
python -m app.ops doctor
```

Pi 通过 `@earendil-works/pi-agent-core` 驱动 Agent Session，通过 `@earendil-works/pi-ai/providers/deepseek` 调用原生 DeepSeek provider；当前模型是 `deepseek-v4-pro`，SDK 在受限 Bridge 子进程中读取 `DEEPSEEK_API_KEY`。

```bash
python -m app.ops doctor
```

Doctor 检查 Python、Node ≥ 22.19、Bridge 构建、SQLite integrity/表结构、Artifact 可写性、Profile、ToolRegistry、两条工作流及 Live 凭证。Live Pi 启动前还会在受限 Bridge 子进程中解析 provider/model，并把自定义密钥变量安全映射到 Provider SDK 约定变量；此预检不发起模型请求。有阻断问题时退出码为 1，输出不包含密钥。

`APP_HOST` 默认为 `127.0.0.1`。开发时保留上面的本机 `uvicorn app.main:app --reload`；生产启动统一使用 `python -m app.main`。绑定 `0.0.0.0` 或 IPv6 公网地址时必须显式设置 `ALLOW_PUBLIC_BIND=true`；手工给 Uvicorn CLI 传 `--host` 会覆盖应用入口参数，因此不属于受支持的生产启动方式。当前版本没有用户认证，不建议直接暴露到公网；建议仅在本机、VPN 或受控内网运行。

`STORAGE_ROOT` 是本机持久化白名单根目录。研究库、Checkpoint、Artifact、日志和备份必须位于该目录内；Artifact、日志和备份目录不能重叠，数据库也不能放入这些目录。

## 证券解析和行情

当前只支持中国 A 股。代码由确定性规则识别：

- `600/601/603/605/688/689` 前缀归属上海 `SH`；
- `000/001/002/003/300/301` 前缀归属深圳 `SZ`；
- `4/8/920` 前缀归属北京 `BJ`；
- 显式交易所必须与代码规则一致；
- 名称仅接受精确匹配；Mock 使用内置 fixture，Live 使用 AKShare 股票列表；重名不会自动选择。

错误码包括 `SECURITY_INPUT_INVALID`、`SECURITY_NOT_FOUND` 和 `SECURITY_NAME_AMBIGUOUS`。

行情统一为以下 UTF-8 CSV 标准列：

```text
date, open, high, low, close, volume, amount
```

Live 模式通过 AKShare 获取前复权日线：主路径为东方财富 `stock_zh_a_hist`，返回空或失败时回退新浪 `stock_zh_a_daily`；两条路径都失败才报错，不回退 Mock。数据必须非空、列完整、至少 120 根、日期升序且不重复、OHLC 合法、收盘价为正、成交量非负，且不包含 `as_of` 之后的数据。

`data_version` 的格式为：

```text
{symbol}_{as_of:YYYYMMDD}_{market_data.csv SHA256 前 8 位}
```

## 技术指标与形态

Python 确定性计算：

- SMA5、SMA20、SMA60；
- MACD 12/26/9：DIF、DEA、柱值和交叉/强弱状态；
- RSI14；
- KDJ 9/3/3；
- Bollinger 20/2；
- ATR14、20 日年化历史波动率；
- Volume MA5、Volume MA20；
- 20/60 日高低点支撑与阻力。

形态规则由代码判断：20 日突破/跌破、均线多头/空头排列、MACD 金叉/死叉、RSI 超买/超卖、放量上涨/下跌。matplotlib 生成约 180 根行情的一张价格/均线/成交量/支撑阻力图；图表失败不会使整项研究失败。

## Agent、工具和 Kronos 边界

`technical_research` 是 Full Profile，最多 5 次迭代和 5 次工具调用，只允许：

- `get_market_data`
- `calculate_technical_indicators`
- `get_technical_summary`

Bridge 暴露工具前校验 Profile，Python ToolRegistry 执行前再次授权。Technical Research 不得调用 Kronos、计算原始指标、分析基本面或输出买卖指令。

`technical_assembly` 是独立 Constrained Session，1 次迭代、0 工具。它只读取当前 run 的已校验 Research、Kronos、指标摘要、证券信息和 `data_version`，不读取完整行情、对话、Session 或其他任务数据。

Kronos Mock 使用与证券代码无关的确定性行情算法。Live 接入遵循官方 Kronos `KronosPredictor` 接口，模型第一次调用时懒加载并按模型名和设备复用；失败或超时明确报错，不回退 Mock。`scripts/setup_kronos.py` 会按 `config/kronos-source.json` 拉取锁定的官方提交、安装 PyTorch 依赖，并缓存 mini 模型及 2k tokenizer。Apple Silicon 优先使用 MPS，MPS 不可用时自动回退 CPU。

```bash
export KRONOS_MODE=live
export KRONOS_MODEL_NAME=NeoQuasar/Kronos-mini
export KRONOS_DEVICE=mps
export KRONOS_SOURCE_DIR=./vendor/kronos
```

官方实现和模型用法参见 <https://github.com/shiyu-coder/Kronos>。

## 基本面架构与 Fundamental Writer

`fundamental_v1` 有十一个业务节点。Lead Planning 制定研究主线；Business 和 Industry 是首轮 Full Agent；Lead Review 将缺失项和财务问题整理为补充任务；Deep Research 使用一轮有上限的 Full Agent 检索并生成 `deep_research.json`；Financial 和 Valuation 是 1 次迭代、0 工具的 Constrained Agent；Lead Final Review 判断材料能否进入 Writer。`ready_for_writer=false` 时保留研究工作包并进入 `HUMAN_REVIEW_REQUIRED`，不生成伪正式报告。

Fundamental Writer 使用独立 Constrained Pi Session，1 次迭代、0 工具。它只读取 Lead、专项研究、Final Review、Evidence、Assumption，以及公司、财务指标和估值的安全摘要；不读取原始财务表、来源全文、执行过程、对话、环境变量或其他 run。Writer 不得搜索、创建 Evidence/Assumption、修改权威数字或研究主线。Writer 返回 `needs_more_research` 时同样进入人工复核，不自动返工。

注册的七个基本面工具为：

- `get_company_profile`
- `search_research_sources`
- `read_research_source`
- `query_findkg`
- `get_financial_data`
- `calculate_financial_metrics`
- `calculate_valuation`

Lead 和 Business 可使用公司资料、搜索和来源读取；Industry 只可使用搜索和来源读取。`query_findkg` 仅在 Lead Planning 可用，用于扩展研究变量和问题，不生成 Evidence，也不会进入 Evidence Store。财务数据、指标和估值工具由工作流的受信 Python 边界调用，再把结果作为只读上下文交给 Constrained Agent。所有 Agent 都不能使用 shell、任意 HTTP/文件/SQL、Python eval、技术指标工具或 Kronos。

FinDKG 使用官方发布的本地文本数据集。将 `entity2id.txt`、`relation2id.txt`、`train.txt`（可选 `valid.txt`、`test.txt`）所在目录配置给 `FINDKG_DATA_DIR`。数据集未安装或实体未命中时，工具返回空关系提示，Lead 继续使用现有公司资料和搜索，不中断流程。官方数据集目录：<https://github.com/xiaohui-victor-li/FinDKG/tree/main/FinDKG_dataset/FinDKG>。

## 基本面数据、指标与估值

Mock 模式使用固定公司资料、至少五年年度财务数据和固定公开来源。Live 基本面数据使用 AKShare：公司概况来自巨潮资讯，年度财务数据合并资产负债表、利润表和现金流量表。只保留 `as_of` 当日已公告的共同期间；Live 失败显式报错，不降级为 Mock。

标准财务期字段包含：`period`、`report_type`、`published_date`、营业收入、营业利润、净利润、归母净利润、总资产、总负债、有息负债、归母股东权益、流动资产/负债、现金、应收账款、存货、经营现金流、资本开支、基本 EPS 和总股本。有息负债是短期借款、长期借款、应付债券、一年内到期非流动负债和租赁负债的轻量汇总。缺失值保存为 `null`，不伪造为 0；币种、单位和数据源必须明确。

`financial_metric_v1` 由 pandas/Python 确定性计算：

- 收入、归母净利润和经营现金流同比；
- 营业利润率、净利率、ROA 和 ROE；
- 资产负债率、流动比率和现金/资产；
- 经营现金流/净利润、自由现金流及其利润率；
- 应收账款/收入、存货/收入和资产周转率。

`valuation_v1` 由 Python 计算 PE/PB/PS 和简化五年 DCF。DCF 使用 Financial Agent 生成并落盘的 `fcf_growth`、`terminal_growth` 和 `discount_rate` 假设，计入现金减有息负债的净现金/净负债和总股本，生成低/基准/高增长敏感性。分母为零或现金、有息负债、自由现金流、假设等必需输入缺失时标记 `unavailable`，Agent 不得重算或改写权威数字。

## Evidence 与 Assumption

Evidence 是当前 run 内的轻量 JSON 存储，由 `read_research_source` 生成唯一 `ev_NNN` ID。Agent 只能引用已存在 ID，不能伪造 Evidence。检索 Provider 经由封闭 registry 和稳定 Protocol 边界接入，后续可在该边界增加 MCP adapter，配置值不能动态导入任意代码；但 Agent 不直接选择 Provider。`search_research_sources.sources` 只是公告/研报/新闻/Web 的结果排序偏好，未知网站名或偏好会被忽略，所有已配置 Provider 仍会参与聚合。默认返回 8 个跨来源去重结果；工具预算耗尽时 Agent 必须输出已有 Evidence 和未完成项，而不是让检索失败。Live 默认启用巨潮、东方财富新闻/研报/公告；Tavily adapter 仍保留，但默认不启用。

通用来源读取禁止 localhost/内网 URL、DNS 解析失败时拒绝、禁止自动重定向，以流式下载限制响应大小。仅对固定的 `static.cninfo.com.cn/finalpage/` HTTPS 路径启用系统代理兼容通道；HTML 取文本，PDF 使用 pypdf 取可提取文字（不做 OCR）。交给 Agent 的只是有界摘录，并明确标记为不可信数据。

Assumption 由 Financial Agent 提出，工作流校验后分配 `asm_NNN` ID 并写入 `assumptions.json`；Valuation 和 Writer 只能引用已存在假设。正式报告完整展示变量、数值、期间、提出节点、来源和 ID，并明确预测和估值对假设敏感。

Live 配置示例：

```bash
export FUNDAMENTAL_DATA_MODE=live
export FUNDAMENTAL_DATA_PROVIDER=akshare

export RESEARCH_SEARCH_MODE=live
export RESEARCH_SEARCH_PROVIDER=official_crawler

# 可选：以后启用 Tavily 时再配置
# export RESEARCH_SEARCH_PROVIDER=tavily
# export RESEARCH_SEARCH_API_KEY_ENV_NAME=TAVILY_API_KEY
# export TAVILY_API_KEY='...'
```

## 文件产物

每个技术面任务原子写入：

```text
data/artifacts/{run_id}/
├── market_data.csv
├── technical_indicators.json
├── technical_research.json
├── kronos_result.json
├── technical_assembly.json
├── technical_chart.png
└── technical_report.md
```

最终报告的指标精确值、支撑阻力、Kronos 概率和收益区间直接读取权威 JSON；Agent 文字只用于解释、冲突、不确定性、风险和结论组织。报告包含固定免责声明，不构成投资建议或交易指令。

每个基本面任务原子写入 18 个文件：

```text
data/artifacts/{run_id}/
├── company_profile.json
├── evidence.json
├── assumptions.json
├── lead_plan.json
├── business_research.json
├── industry_research.json
├── lead_review.json
├── deep_research.json
├── financial_data.json
├── financial_metrics.json
├── financial_research.json
├── valuation_result.json
├── valuation_research.json
├── lead_final_review.json
├── fundamental_research_package.md
├── fundamental_writer.json
├── fundamental_report.md
└── result_manifest.json
```

`fundamental_research_package.md` 继续作为调试和审计产物，默认页面展示 `fundamental_report.md`。正式报告固定包含摘要、公司、业务、行业、五类财务表现、盈利驱动、假设、PE/PB/PS、简化 DCF、Evidence、冲突、风险、限制、版本和免责声明。精确数字只从 `financial_data.json`、`financial_metrics.json`、`valuation_result.json` 和 `assumptions.json` 读取；Writer 只提供解释文字。

正文用 `[ev_NNN]` 引用 Evidence，只在证据章节展开实际引用项的来源元数据和限长摘要，不展示来源全文。不存在的 Evidence/Assumption ID 会阻止报告生成。

`result_manifest.json` 为每个结果保存当前 `version`、`current/stale/failed`、文件 SHA256、固定输入 SHA 和更新时间。首次成功生成版本为 1，成功重建后加 1，不保存旧文件副本。依赖关系固定写在代码中，不使用动态依赖数据库。

恢复时把既有损坏校验与 STALE 检查合并：输入 SHA 改变会标记直接结果及固定下游为 stale，并从最早受影响节点继续 Checkpoint 流程。例如 Assumption 改变会重建估值、Final Review、Writer 和报告，而不重跑无关的 Business。stale 报告不会由报告 API 作为最新报告返回。节点前检查取消；Agent 重试和重建使用新 attempt，历史执行不覆盖。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | Web 健康检查 |
| `GET` | `/api/readiness` | 数据库、Checkpoint、Artifact、Bridge、Profile 和工作流就绪状态 |
| `GET` | `/api/runtime/health` | Runtime、Profile、Tool 和 Checkpoint 摘要 |
| `POST` | `/api/runs` | 创建 technical 或 fundamental 任务 |
| `GET` | `/api/runs` | 查询最近任务 |
| `GET` | `/api/runs/{run_id}` | 查询状态、节点和事件 |
| `GET` | `/api/runs/{run_id}/executions` | 查询安全的 Agent 执行摘要 |
| `POST` | `/api/runs/{run_id}/cancel` | 请求取消 |
| `GET` | `/api/runs/{run_id}/report` | 获取 Markdown、安全 HTML 和版本元数据 |
| `GET` | `/api/runs/{run_id}/report/export` | 导出单文件 HTML 报告（内联 CSS 与 Base64 技术图，无脚本、静态资源或运行时 API 请求） |
| `GET` | `/api/runs/{run_id}/artifacts/technical_chart.png` | 读取当前任务目录内的技术图表 |

图表接口固定到 `data/artifacts/{run_id}/technical_chart.png`，不接受任意文件路径。

任务详情中的 `usage` 从现有 AgentExecution/ToolExecution 聚合 Agent 调用、工具调用和 Provider 返回的 token。只有 Provider/Bridge 返回费用时才记录 `estimated_cost`，否则保持 `null`，不在本地编造模型价格。

## 日志、备份和恢复检查

Web 和 Worker 分别写入 `logs/web.log` 和 `logs/worker.log`，同时保留 JSON 控制台输出。单文件上限 10 MB，保留 5 个转储文件。`api_key/token/authorization/password/secret/cookie`、Bearer 值和数据库 URL 密码会被脱敏。

```bash
backup_path=$(python -m app.ops backup)
python -m app.ops restore-check --backup "$backup_path"
```

备份使用 SQLite Backup API 复制 `research.db` 和 `checkpoints.db`，并复制 `data/artifacts/`。备份通过跨进程维护锁与 Worker 任务互斥；多个 Worker 即使被误启动，也只能有一个进入任务执行区。不备份 `.env`、日志、虚拟环境、`node_modules` 或模型权重。`restore-check` 只在临时目录校验 Manifest/SHA/大小汇总、SQLite integrity/必需表、Artifact JSON 和报告引用，不覆盖当前数据。

人工恢复时：先停止 Web/Worker，对当前 `data/` 再做一次备份，将已通过 `restore-check` 的 `research.db`、`checkpoints.db` 和 `artifacts/` 复制到配置位置，再运行 doctor 后启动。

## 单机 Docker 部署

Docker Compose 使用同一镜像启动单 Web 和单 Worker，通过 Docker named volumes 共享并持久化数据、日志、备份和 Kronos 缓存，避免宿主 bind mount 与非 root UID 的写权限冲突。不包含 Redis、Celery、反向代理或数据库容器。镜像以非 root 用户运行，Node Bridge 在构建阶段编译。

```bash
cp .env.example .env
python -m app.ops doctor
docker compose config
docker compose build
docker compose up -d
```

Compose 只将宿主机 `127.0.0.1:8000` 映射到容器，并给活动节点 360 秒停机宽限期；SIGTERM 会给当前任务登记取消请求，任务在当前有界节点返回后于下一节点边界取消。非 Docker 启动方式是 `python -m app.main` 和 `python -m app.worker`。

容器化范围说明：当前 Dockerfile 只安装 `requirements.txt`，没有安装 `requirements-kronos.txt`，也没有复制 `vendor/kronos`、`config/kronos-source.json` 或在构建/启动时执行 `setup_kronos.py`。因此容器内只支持 Mock 和不含 Kronos 的 Live 配置；要把 Kronos Live 放入容器，需补充上述依赖、复制并锁定官方源码、设计模型缓存预热，并在 Docker Linux 上使用 CPU 或明确的 GPU runtime（Apple MPS 在容器内不可用）。`docker compose build/up` 尚未在实机完成验证。

## Live Smoke

```bash
python scripts/live_smoke.py --technical 600519
python scripts/live_smoke.py --fundamental 600519
python scripts/live_smoke.py --all 600519
```

脚本会先确认相关 Pi、AKShare、Kronos 和当前检索适配器全部为 Live，任一组件为 Mock 都会失败。它还会取得独占维护锁并要求等待队列为空，因此不会领取已有任务，也不会与正式 Worker 并行；执行前应先停止 Worker。输出仅包含 run_id、workflow、状态、耗时、报告文件名、数据源、模型及 Evidence/Assumption 数量。

## 测试

```bash
source .venv/bin/activate
pytest
pip check

cd pi_bridge
npm test
npm run build
```

可选 Live 验证：

```bash
RUN_MARKET_LIVE=1 pytest -m market_live
pytest -m kronos_live
pytest -m live
RUN_FUNDAMENTAL_DATA_LIVE=1 pytest -m fundamental_data_live
RUN_RESEARCH_SEARCH_LIVE=1 \
  RESEARCH_SEARCH_API_KEY_ENV_NAME=TAVILY_API_KEY \
  pytest -m research_search_live
```

缺少显式的 Live 开关、网络、模型源码、模型名或 API Key 时，对应 Live 测试自动跳过。普通测试不会访问 Live 基本面服务或下载 Kronos 模型。Mock 测试结果不等于 Live 验收。

## 当前限制与下一阶段入口

- 仅支持中国 A 股日线；无交易日历、多数据源切换、回测或自动交易；
- 只支持单 Worker；未实现分布式租约、Redis 或 Celery；
- 最多允许 `MAX_PENDING_RUNS` 个未结束任务，超限返回 HTTP 429；不支持多 Worker 并行调度；
- Kronos Live 依赖本机缓存的官方源码和 Hugging Face 权重；安装位置不提交 Git，版本由 `config/kronos-source.json` 锁定；
- 基本面 Live 当前只实现 AKShare A 股公司概况、年度三表和市场快照；不包含完整三表联动预测、复杂 WACC、蒙特卡洛、大型可比公司库或自动投资评级；
- Live 来源检索当前支持巨潮资讯公告和可选 Tavily；尚未接入互联网全站搜索或生产 MCP Server，PDF 不做 OCR；
- Evidence/Assumption 是当前 run 内的轻量存储，没有复杂审批、多人协作或跨 run 传播；
- Result Manifest 只保留当前文件的轻量版本和固定依赖，不是完整历史版本仓库或动态依赖图；
- Lead Review 后支持一轮有上限的 Deep Research 补充检索；Writer 不执行自动返工，仍需更多材料时进入人工复核；
- 没有综合评分、综合报告、回测或自动交易。
- 当前没有身份认证、多用户权限、分布式锁、云存储或复杂告警，只定位为个人/受控内网单机 MVP。

下一阶段可在不改变权威 JSON 和安全边界的前提下完善数据覆盖、人工补充材料入口和 Live 模型验收；在这些基础完成前，不合并技术面与基本面综合评分。
