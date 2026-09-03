# 金融投研 Agent 技术方案（个人开发版）

## 一、项目目标

本项目建设一个面向个股研究的 Web 应用。

用户输入股票名称或证券代码，并选择：

- 技术面分析；
- 基本面分析。

系统自动完成证券识别、数据获取、指标计算、资料检索、Agent 协作和报告生成。

本版本保留原产品方案中的完整研究流程，但不针对高并发、大规模数据存储和企业级部署进行设计。

主要目标包括：

- 跑通技术面和基本面完整链路；
- 保证计算、分析和报告写作相互分离；
- 保证重要结论能够追溯到数据、Evidence 或 Assumption；
- 支持任务进度、失败重试、取消和中断恢复；
- 尽量降低个人开发和后期维护成本。

## 二、总体技术思路

系统采用单体应用结构，不拆分微服务。

推荐技术组合：

```text
FastAPI
+ LangGraph
+ Pi Agent Runtime
+ SQLite
+ 本地文件存储
+ HTML / CSS / 原生 JavaScript
```

各组件职责如下：

| 组件 | 主要职责 |
| --- | --- |
| FastAPI | 提供网页、API 和任务管理接口 |
| LangGraph | 管理研究流程、节点顺序、分支、并行和恢复 |
| Pi Agent Runtime | 执行单个 Agent 节点内部的检索和分析循环 |
| SQLite | 保存任务、结果、证据、假设和报告 |
| 本地文件目录 | 保存行情文件、网页快照、PDF、图表和报告 |
| HTML/CSS/JavaScript | 提供用户操作界面和任务进度展示 |
| Python 脚本 | 负责技术指标、财务指标和估值计算 |

系统只需运行两个进程：

```text
Web/API 进程
+
Research Worker 进程
```

Web 进程负责接收用户请求。

Worker 进程负责执行 LangGraph 研究工作流。

## 三、系统架构

```text
用户浏览器
    ↓
HTML / CSS / JavaScript
    ↓
FastAPI
    ├── 创建研究任务
    ├── 查询任务进度
    ├── 取消任务
    └── 获取研究报告
    ↓
Research Worker
    ↓
LangGraph Orchestrator
    ├── 技术面流程
    └── 基本面流程
    ↓
Pi Agent / Kronos / Python Tools
    ↓
SQLite + 本地文件目录
```

系统保持三层职责边界：

```text
LangGraph：
管理整个研究流程下一步执行什么

Pi Agent：
管理当前 Agent 节点内部如何完成任务

SQLite：
保存正式研究状态和研究结果
```

## 四、前端方案

前端直接使用：

- HTML；
- CSS；
- 原生 JavaScript。

不使用 React、Vue、HTMX 和复杂前端构建工具。

### 4.1 首页

首页包含：

- 股票名称或代码输入框；
- 技术面或基本面选择；
- 投资风格选择；
- 开始分析按钮；
- 历史任务列表。

### 4.2 任务进度页

任务创建后，前端每隔约 2 秒查询一次任务状态。

展示内容包括：

- 当前任务状态；
- 当前执行阶段；
- 已完成节点；
- 正在执行节点；
- 待执行节点；
- 错误或数据不足提示；
- 取消任务按钮。

### 4.3 报告页

报告以 Markdown 转换后的 HTML 形式展示。

支持查看：

- 报告正文；
- 数据截止时间；
- 模型和工具版本；
- Evidence；
- Assumption；
- 原始来源；
- 技术面图表。

## 五、后端方案

FastAPI 负责网页和 API。

核心接口包括：

### 创建研究任务

```text
POST /api/runs
```

主要参数：

- 股票名称或代码；
- 分析类型；
- 投资风格；
- 数据截止日期。

返回 run_id。

### 查询任务状态

```text
GET /api/runs/{run_id}
```

返回：

- 总任务状态；
- 当前阶段；
- 节点状态；
- 进度；
- 错误信息；
- 报告是否完成。

### 取消任务

```text
POST /api/runs/{run_id}/cancel
```

系统停止启动新的研究节点，并保留已经完成的结果。

### 获取报告

```text
GET /api/runs/{run_id}/report
```

返回技术面或基本面报告。

## 六、Orchestrator 方案

研究流程使用 LangGraph 管理。

LangGraph 主要负责：

- 节点执行顺序；
- 技术面和基本面路由；
- Business 和 Industry 并行执行；
- 条件分支；
- Lead Review 后的返工；
- 节点失败重试；
- 节点执行状态保存；
- Worker 中断后的流程恢复。

LangGraph 只保存轻量流程状态，例如：

- run_id；
- 当前阶段；
- 当前任务 ID；
- Result 引用；
- Review 决策；
- 返工任务类型；
- 报告引用。

完整研究数据仍然保存在业务数据库中。

推荐使用两个 SQLite 文件：

```text
data/research.db
data/checkpoints.db
```

其中：

- research.db 保存正式业务数据；
- checkpoints.db 保存 LangGraph 流程执行状态。

## 七、Pi Agent Runtime

所有 Agent 共用同一个 Pi Core。

系统通过 AgentProfile 配置不同角色。

每个 Profile 主要定义：

- Agent 角色；
- Prompt；
- Full 或 Constrained 模式；
- 允许使用的工具；
- 最大循环次数；
- 上下文加载范围；
- 输出格式。

### Full Agent

用于：

- Technical Research；
- Lead Agent；
- Business Research；
- Industry Research。

允许自主检索和多轮工具调用，但受到工具白名单和最大循环次数限制。

### Constrained Agent

用于：

- Financial Research；
- Valuation Research；
- Technical Assembly；
- Fundamental Writer。

只读取任务相关内容，工具权限和循环次数更少，并且必须输出固定格式。

### PiAgentAdapter

系统通过统一适配层调用 Pi Core。

适配层负责：

- 创建独立 Session；
- 加载 AgentProfile；
- 注册允许使用的工具；
- 加载任务上下文；
- 获取 Agent 输出；
- 执行输出校验；
- 保存正式结果；
- 结束或归档 Session。

每个专业任务使用独立 Session，不长期共享完整会话。

## 八、技术面流程

技术面保持三个节点：

```text
Technical Research
→ Kronos
→ Technical Assembly
→ 技术面报告
```

### 8.1 Technical Research

负责：

- 获取行情和成交量数据；
- 检查数据完整性；
- 调用技术指标脚本；
- 分析趋势、量价、动量、支撑阻力和形态；
- 输出短期、中期和长期判断。

以下内容由 Python 脚本计算：

- 均线；
- MACD；
- RSI；
- KDJ；
- 成交量指标；
- 波动率；
- 支撑位和阻力位；
- 多周期指标；
- 技术形态候选。

Agent 只负责解释，不直接计算这些指标。

### 8.2 Kronos

Kronos 独立读取标准化行情数据。

输出：

- 上涨概率；
- 震荡概率；
- 下跌概率；
- 预期收益区间；
- 模型置信度；
- 模型版本；
- 数据版本。

Kronos 不读取 Technical Research 的分析结论。

### 8.3 Technical Assembly

Technical Assembly 只读取：

- Technical Research 结果；
- Kronos 结果。

负责：

- 对比两类信号；
- 说明一致结论；
- 保留冲突；
- 说明不确定性；
- 生成最终技术面报告。

它不能重新检索或重新计算指标。

## 九、基本面流程

基本面流程如下：

```text
Lead Planning
→ Business 与 Industry
→ Lead Review
→ Financial
→ Lead Review
→ Valuation
→ Lead Final Approval
→ Fundamental Writer
→ 基本面报告
```

### 9.1 Lead Agent

Lead Agent 负责：

- 初步阅读和检索；
- 识别商业模式；
- 提炼研究主线；
- 提出关键问题；
- 创建专业任务；
- 审核专业结果；
- 处理结果冲突；
- 决定是否返工；
- 批准 Evidence 和 Assumption；
- 生成报告大纲。

Lead Agent 不直接执行财务和估值计算。

### 9.2 Business Research

负责研究：

- 商业模式；
- 收入结构；
- 客户和渠道；
- 产品结构；
- 竞争优势；
- 管理层；
- 主要业务风险。

### 9.3 Industry Research

负责研究：

- 产业链；
- 行业规模；
- 供需关系；
- 竞争格局；
- 周期位置；
- 政策和技术变化；
- 行业主要风险。

Business 和 Industry 可以通过 LangGraph 并行执行。

### 9.4 Financial Research

负责：

- 财务趋势分析；
- 盈利能力分析；
- 现金流分析；
- 资产负债分析；
- 盈利驱动分析；
- 财务预测假设。

财务指标和基础勾稽由 Python 脚本完成。

### 9.5 Valuation Research

必须等待 Financial 完成。

负责：

- PE、PB 等相对估值；
- 历史估值区间；
- 可比公司估值；
- DCF；
- 敏感性分析；
- 估值风险。

具体估值计算由 Python 脚本完成。

### 9.6 Fundamental Writer

Writer 只读取：

- 已批准的研究主线；
- 已批准的报告大纲；
- 已验收的专业结果；
- 已批准的 Evidence；
- 已批准的 Assumption。

Writer 不搜索新资料，不修改财务预测和估值。

## 十、Evidence 和 Assumption

### 10.1 Evidence

Evidence 用于记录支持研究结论的证据。

主要内容包括：

- 支持的 Claim；
- 证据内容；
- 来源名称；
- URL；
- 日期；
- 文档位置；
- 来源类型；
- 原始快照路径；
- 状态和版本。

Evidence 状态包括：

```text
PROPOSED
APPROVED
REJECTED
```

专业 Agent 可以提出 Evidence，只有 Lead 可以批准。

### 10.2 Assumption

Assumption 用于记录预测和估值中的关键假设。

主要内容包括：

- 变量；
- 数值；
- 时间区间；
- 来源；
- 提出任务；
- 状态；
- 版本。

状态包括：

```text
PROPOSED
APPROVED
REJECTED
SUPERSEDED
STALE
```

已批准假设发生变化时，不覆盖旧记录，而是创建新版本。

## 十一、结果版本和失效

每个正式 Result 保存：

- 自身版本；
- 输入 Result；
- 输入 Evidence；
- 输入 Assumption；
- AgentProfile 版本；
- 数据版本；
- 工具版本。

当上游 Result 或 Assumption 更新时：

```text
创建新版本
→ 将旧版本标记为失效
→ 查找依赖旧版本的下游结果
→ 将下游结果标记为 STALE
→ Lead 决定是否重新执行
```

Writer 只能读取当前有效、并且已经批准的结果。

## 十二、数据存储

个人版本使用 SQLite 和本地文件目录。

### SQLite 主要保存

- Research Run；
- Research Task；
- Result；
- Evidence；
- Assumption；
- Result Reference；
- Report；
- Run Event；
- Agent 和工具执行记录。

### 本地文件主要保存

- 行情原始文件；
- 标准化行情数据；
- 网页快照；
- PDF；
- 图表；
- Kronos 输入输出；
- Markdown 报告。

建议目录：

```text
data/
├── research.db
├── checkpoints.db
├── artifacts/
│   └── {run_id}/
│       ├── documents/
│       ├── market_data/
│       ├── charts/
│       └── reports/
└── cache/
```

长文档检索第一阶段可以使用 SQLite FTS5，不引入独立向量数据库。

## 十三、状态与恢复

### 总任务状态

```text
CREATED
RESOLVING_SECURITY
ROUTING
RUNNING
REPORTING
COMPLETED
```

异常状态：

```text
WAITING_FOR_DATA
RETRYABLE_FAILED
FAILED
CANCELLED
HUMAN_REVIEW_REQUIRED
```

### 节点状态

```text
PENDING
RUNNING
COMPLETED
STALE
FAILED
CANCELLED
```

每完成一个节点：

- 保存正式 Result；
- 更新任务状态；
- 写入运行事件；
- 保存 LangGraph Checkpoint。

Worker 重启后，可以从最后一个已完成节点继续执行。

## 十四、重试与取消

### 重试

建议：

- 数据接口失败重试 2～3 次；
- Agent 节点失败重试 1 次；
- Kronos 失败重试 1 次；
- Schema 修复最多 1 次。

避免工具、Pi 和 LangGraph 同时大量重试。

### 取消

用户取消任务后：

- 不再启动新节点；
- 当前无法中断的模型请求可以等待结束；
- 已完成的 Result 保留；
- Run 标记为 CANCELLED。

## 十五、安全要求

个人版本仍需保留以下基本安全要求：

- 网页和 PDF 内容全部视为不可信数据；
- 文档中的文字不能覆盖系统 Prompt；
- Agent 只能调用白名单工具；
- 外部 URL 禁止访问 localhost 和内网地址；
- API Key 使用环境变量；
- API Key 不写入数据库和日志；
- Markdown 转换为 HTML 后进行安全过滤；
- Agent 输出通过 Schema 校验后才能正式保存；
- Writer 不得读取未批准结果；
- 不在界面展示 Agent 内部推理过程。

## 十六、开发阶段

对于个人开发者，建议分六个阶段完成。

### 第一阶段：项目骨架

完成：

- FastAPI；
- HTML/CSS/JavaScript 页面；
- SQLite；
- Run 创建和查询；
- Worker；
- 基础状态管理。

### 第二阶段：Agent Runtime

完成：

- PiAgentAdapter；
- AgentProfile；
- ToolRegistry；
- ContextLoader；
- OutputValidator；
- LangGraph Checkpoint。

### 第三阶段：技术面流程

完成：

- 证券解析；
- 行情数据；
- 技术指标脚本；
- Technical Research；
- Kronos；
- Technical Assembly；
- 技术面报告。

### 第四阶段：基本面专业研究

完成：

- Lead Planning；
- Business；
- Industry；
- Financial；
- Valuation；
- Lead Review；
- 财务和估值脚本。

### 第五阶段：研究状态和报告

完成：

- Evidence；
- Assumption；
- 结果版本；
- STALE；
- Fundamental Writer；
- 基本面报告；
- 引用追溯。

### 第六阶段：稳定性完善

完成：

- 取消；
- 重试；
- Worker 恢复；
- 错误提示；
- 日志；
- 成本统计；
- Docker 部署；
- README 和测试。

## 十七、最终系统形态

最终系统由以下部分组成：

```text
一个 FastAPI 项目
一个原生 HTML/CSS/JavaScript 前端
一个 Research Worker
一个 LangGraph 工作流
一个 Pi Agent Runtime 适配层
两个 SQLite 数据库
一个本地文件目录
两条完整研究流程
```

该方案保留原产品方案的核心功能与流程，同时避免使用微服务、Redis、消息队列、Kubernetes 和复杂分布式基础设施，适合作为个人开发版本。

后续用户量和数据量增长时，可逐步将：

- SQLite 升级为 PostgreSQL；
- 本地文件升级为对象存储；
- 单 Worker 升级为任务队列；
- 本地 LangGraph Checkpoint 升级为正式持久化存储；
- 原生前端升级为 React 或 Vue。

这些升级不会改变现有 Agent 分工和研究流程。
