# 金融投研 Agent 产品方案 V2.1

## 一、产品定位

本产品是基于 Pi Agent 框架改造的个股投研 Web 应用。MVP 仅提供两类独立报告：

1. 技术面分析报告；
2. 基本面分析报告。

用户输入股票名称或证券代码，并选择分析类型。系统完成证券识别、数据获取、工具调用、Agent 协作和报告生成。

MVP 不提供综合分析、综合评分或技术面与基本面的合并报告。

---

## 二、产品目标

### 2.1 核心目标

- 降低个股研究的信息获取和整理成本；
- 将数据计算、资料检索、分析推理和报告写作分离；
- 保证关键数据和结论能够追溯到来源；
- 通过结构化状态、任务和结果控制多 Agent 协作；
- 在保证研究质量的前提下控制上下文、调用成本和运行时间。

### 2.2 非目标

MVP 不负责：

- 自动交易；
- 直接执行买卖指令；
- 提供确定性收益承诺；
- 生成综合投资评级；
- 同时覆盖多个市场的全部数据标准。

---

## 三、用户流程

```text
用户输入股票名称或代码
        ↓
系统解析证券代码和市场
        ↓
用户选择：技术面分析 / 基本面分析
        ↓
创建异步研究任务并返回 run_id
        ↓
前端展示执行进度
        ↓
生成对应研究报告
```

用户不需要输入关键假设。研究假设由基本面研究流程在证据和任务结果基础上生成、审核和维护。

---

## 四、总体系统架构

```text
Web Frontend
    ↓
API Service
    ↓
Task Orchestrator
    ├── Technical Module
    └── Fundamental Module
    ↓
Agent Runtime / Tool Runtime
    ↓
Data & Storage Layer
```

### 4.1 Web Frontend

负责：

- 股票输入；
- 分析类型选择；
- 任务进度展示；
- 报告展示；
- 错误和数据不足提示。

### 4.2 API Service

负责：

- 创建任务；
- 查询任务状态；
- 取消任务；
- 获取报告；
- 用户权限和访问控制。

长任务采用异步执行。前端通过轮询、SSE 或 WebSocket 获取运行进度。

### 4.3 Task Orchestrator

负责：

- 状态机；
- 节点依赖；
- 超时和重试；
- 任务取消；
- 结果版本；
- 上游变更导致的下游失效；
- 成本和调用次数控制。

### 4.4 存储层

建议使用：

- PostgreSQL：任务、状态、证据、假设、结果和版本；
- Redis：队列、缓存、锁和短期状态；
- 对象存储：PDF、网页快照、图表和报告文件；
- 向量索引：作为长文档按需检索能力，不作为唯一事实库。

### 4.5 Pi Runtime 改造方案

本项目不直接 Fork 或修改 Pi Core，而是在 Pi 外部增加统一适配层：

```text
Task Orchestrator
        ↓
PiAgentAdapter
        ↓
统一 Pi Core
    ├── AgentProfile
    ├── ToolRegistry
    ├── ContextLoader
    ├── OutputValidator
    └── RunController
```

Pi 负责单个研究节点内部的自主执行，包括：

- 检索、分析和补充检索循环；
- 在允许范围内自主选择工具；
- 管理单次任务的 Session 和上下文；
- 输出工具调用和模型事件；
- 生成结构化任务结果。

外部系统负责：

- 技术面与基本面的固定流程；
- Agent 节点之间的先后依赖；
- Research State 持久化；
- 任务级超时、重试、取消和恢复；
- Schema 校验和正式结果写入；
- 上游结果变更后的下游失效。

核心原则：Pi 只管理“节点内部如何完成任务”，Orchestrator 管理“整个产品下一步执行什么”。

### 4.6 AgentProfile 运行模式

所有使用 Pi 的 Agent 共用同一个 Pi Core，通过 AgentProfile 形成不同角色和权限，不维护“原版 Pi”和“改造版 Pi”两套代码。

```json
{
  "role": "financial",
  "mode": "constrained",
  "system_prompt": "prompts/financial.md",
  "skills": ["financial-analysis"],
  "tool_policy": "financial",
  "max_iterations": 6,
  "context_policy": "task_scoped",
  "output_schema": "specialist_output"
}
```

运行模式分为：

#### Full Agent

用于：

- Lead Agent；
- Technical Research Agent；
- Business Research；
- Industry Chain Research。

能力包括自主检索、多轮工具调用、信息充足性判断和补充研究，但仍受工具白名单、最大循环次数和输出 Schema 约束。

#### Constrained Agent

用于：

- Financial Research；
- Valuation Research；
- Fundamental Writer Agent；
- Technical Assembly Agent。

限制包括：

- 只加载任务相关上下文；
- 仅能使用少量指定工具；
- 最大循环次数较低；
- 必须输出固定 Schema；
- 不允许直接修改 Research State。

Kronos 作为独立模型服务运行，不使用 Pi Agent Loop。

---

# 五、技术面分析模块

## 5.1 协作结构

技术面使用固定的三个 Agent：

```text
Technical Research Agent
        ↓
Kronos Agent
        ↓
Technical Assembly Agent
```

技术面不使用基本面的 Lead Agent 和 Specialist Runtime。

## 5.2 Technical Research Agent

Technical Research Agent 使用 Full Agent 模式的 Pi Runtime。其内部允许执行“数据获取—指标计算—信息判断—补充检索—重新分析”的自主循环。

负责：

- 解析证券代码；
- 获取行情和成交量数据；
- 检查数据完整性；
- 调用技术指标计算脚本；
- 分析趋势、量价、支撑阻力、动量和形态；
- 区分短期、中期和长期信号；
- 输出结构化技术分析结果。

以下内容必须由确定性脚本计算，不能由大模型自行计算：

- 均线和趋势指标；
- 成交量和量价指标；
- 支撑位和阻力位；
- MACD、RSI、KDJ；
- 波动率；
- 多周期指标；
- 技术形态候选结果。

Agent 只负责工具调度和结果解释。

## 5.3 Kronos Agent

负责：

- 读取标准化行情数据；
- 调用 Kronos 模型；
- 输出预测周期；
- 输出上涨、震荡和下跌概率；
- 输出预期收益区间；
- 输出模型置信度；
- 保存模型版本和数据截止时间。

Kronos Agent 不读取 Technical Research Agent 的分析结论，避免相互影响。

## 5.4 Technical Assembly Agent

Technical Assembly Agent 使用 Constrained Agent 模式。它只读取技术研究结果和 Kronos 结果，不承担检索与计算职责。

负责：

- 读取 Technical Research Agent 的结果；
- 读取 Kronos Agent 的结果；
- 对比两类信号；
- 保留冲突和不确定性；
- 生成最终技术面报告。

该 Agent 不允许：

- 重新检索数据；
- 重新计算指标；
- 修改 Kronos 输出；
- 对冲突信号进行简单投票；
- 强行形成一致结论。

## 5.5 技术面状态机

```text
TECH_RESEARCHING
→ KRONOS_ANALYZING
→ TECH_ASSEMBLING
→ DONE
```

如果 Technical Research Agent 已生成标准化行情数据，Kronos 可以直接复用同一数据版本。

## 5.6 Technical Research Output Schema

```json
{
  "symbol": "600519.SH",
  "as_of": "2026-08-05",
  "data_config": {
    "frequency": ["daily", "weekly"],
    "adjustment": "forward_adjusted",
    "lookback_days": 750,
    "data_version": "market_data_20260805"
  },
  "analysis": {
    "trend": {},
    "volume_price": {},
    "support_resistance": {},
    "momentum": {},
    "patterns": {}
  },
  "time_horizon": {
    "short_term": "...",
    "medium_term": "...",
    "long_term": "..."
  },
  "conflicts": [],
  "risks": [],
  "confidence": "medium",
  "script_version": "tech_indicator_v1"
}
```

## 5.7 Kronos Output Schema

```json
{
  "symbol": "600519.SH",
  "as_of": "2026-08-05",
  "horizon": "20_trading_days",
  "direction_probability": {
    "up": 0.57,
    "flat": 0.26,
    "down": 0.17
  },
  "expected_return_range": [-0.04, 0.09],
  "model_confidence": 0.61,
  "model_version": "kronos_xxx",
  "data_version": "market_data_20260805"
}
```

---

# 六、基本面分析模块

## 6.1 协作结构

```text
Lead Agent
    ↓
Specialist Runtime
    ├── Business Research
    ├── Industry Chain Research
    ├── Financial Research
    └── Valuation Research
    ↓
Lead Review / Approval
    ↓
Fundamental Writer Agent
```

## 6.2 Lead Agent

Lead Agent 独立运行，是基本面研究的负责人。

负责：

- 初步检索和快速阅读；
- 识别商业模式；
- 提炼核心矛盾和市场分歧；
- 形成研究主线；
- 生成关键问题；
- 创建专业研究任务；
- 处理任务结果冲突；
- 修改正式假设；
- 决定返工和补充研究；
- 验收专业研究结果；
- 生成正式报告大纲。

Lead Agent 是唯一可以正式修改研究主线的 Agent。

## 6.3 Specialist Runtime

Business、Industry、Financial 和 Valuation 在业务上是独立专业角色，在工程上共用统一 Pi Runtime 和任务执行框架。

每个任务类型通过 AgentProfile 配置独立的：

- Prompt；
- Skill；
- 输入 Schema；
- 输出 Schema；
- 工具权限策略；
- 最大循环次数；
- 上下文加载策略；
- 验收规则。

推荐运行模式：

- Business：Full Agent；
- Industry：Full Agent；
- Financial：Constrained Agent，检索和解释使用 Pi，财务计算和三表勾稽使用脚本；
- Valuation：Constrained Agent，估值计算使用脚本，需要寻找可比公司时才开放检索工具。

它们共享结构化 Research State，但不共享完整对话、草稿或内部推理过程。每个任务创建独立 Session，任务完成后将结构化结果写入数据库并销毁或归档 Session。

## 6.4 专业任务依赖

```text
Business ─┐
Industry ─┼→ Financial → Valuation
          └→ Assumption Store
```

规则：

- Business 和 Industry 可以并行；
- Financial 可以读取已批准的业务和产业链假设；
- Valuation 必须等待 Financial 完成；
- 上游结果被修改后，下游结果标记为 `STALE`；
- 是否重新执行下游任务由 Lead Agent 决定。

## 6.5 Fundamental Writer Agent

Writer 使用 Constrained Agent 模式独立运行，只读取：

- 正式研究主线；
- 正式报告大纲；
- 已验收研究结果；
- 已批准 Evidence；
- 已批准 Assumption。

Writer 不允许：

- 搜索新资料；
- 创建新假设；
- 修改财务预测；
- 修改估值；
- 读取未验收草稿；
- 直接指挥专业节点。

当材料不足时，Writer 向 Lead 返回缺失项，由 Lead 决定是否创建补充任务。

## 6.6 渐进式上下文披露

所有专业节点和 Writer 均按引用加载上下文，而不是一次加载完整 Research State。

示例：Financial 任务只读取：

```text
研究主线摘要
+ 当前任务要求
+ 相关行业假设
+ 相关业务结论
+ 财务证据
```

这可以降低上下文长度、成本和无关信息干扰。

---

# 七、投资风格 Policy

## 7.1 默认策略

用户不选择投资风格插件时，使用系统内置的通用策略：

```json
{
  "policy_id": "general_research",
  "lead_prompt_overlay": null
}
```

Lead Agent 可以根据公司情况生成主线和任务，但不能修改系统证据标准、工具权限和硬校验规则。

## 7.2 巴菲特风格插件

巴菲特插件通过 Prompt Overlay 和结构化 Policy 改变 Lead Agent 的研究重点和任务派发。

```json
{
  "policy_id": "buffett_style",
  "lead_prompt_overlay": "...",
  "required_questions": [
    "公司是否具有长期竞争优势",
    "管理层资本配置能力如何"
  ],
  "metric_priorities": [
    "ROIC",
    "自由现金流",
    "负债水平"
  ],
  "valuation_preferences": [
    "DCF",
    "所有者收益"
  ],
  "risk_preferences": {
    "leverage_tolerance": "low"
  }
}
```

Policy 可以改变：

- 研究问题；
- 任务优先级；
- 指标关注度；
- 估值方法偏好；
- 风险偏好。

Policy 不能改变：

- 原始数据；
- 证据标准；
- 财务计算规则；
- 工具权限边界；
- 验收规则。

---

# 八、基本面核心 Schema

## 8.1 Research State

```json
{
  "run_id": "run_001",
  "symbol": {
    "ticker": "600519",
    "market": "CN"
  },
  "mode": "fundamental",
  "as_of": "2026-08-05",
  "status": "running",
  "policy": {
    "policy_id": "general_research",
    "lead_prompt_overlay": null
  },
  "runtime": {
    "pi_core_version": "x.y.z",
    "agent_profile_version": "v1"
  },
  "thesis": {
    "version": 1,
    "summary": "...",
    "key_questions": []
  },
  "tasks": [],
  "evidence": [],
  "assumptions": [],
  "results": [],
  "report": {
    "outline": [],
    "approved_refs": [],
    "status": "pending"
  }
}
```

## 8.2 Lead Task Card

Lead 负责定义目标和验收标准，不指定具体工具。

```json
{
  "task_id": "task_001",
  "task_type": "financial_analysis",
  "title": "盈利驱动分析",
  "question": "未来利润增长由哪些变量驱动",
  "scope": [
    "收入",
    "销量",
    "价格",
    "成本"
  ],
  "context_refs": [
    "thesis:v2",
    "result:industry_001"
  ],
  "depends_on": [
    "task_industry"
  ],
  "requirements": [
    "拆分主要盈利驱动变量"
  ],
  "completion_criteria": [
    "回答核心问题",
    "结论有证据",
    "假设可以追溯"
  ]
}
```

## 8.3 Specialist Output

```json
{
  "task_id": "task_001",
  "status": "completed",
  "summary": "利润增长主要由销量和产品结构驱动",
  "findings": [
    {
      "claim": "业务A是主要利润增长来源",
      "evidence_ids": ["ev_001"],
      "assumption_ids": ["asm_001"],
      "confidence": "medium"
    }
  ],
  "new_evidence": [],
  "new_assumptions": [],
  "risks": [],
  "conflicts": [],
  "missing_information": [],
  "suggested_followups": []
}
```

## 8.4 Evidence

Evidence 和 Source 合并为一个简化实体，只保留支持投研所需的信息。

```json
{
  "id": "ev_001",
  "claim": "业务A是主要收入增长来源",
  "content": "业务A收入同比增长28%，贡献67%的收入增量",
  "source_name": "2025年年度报告",
  "url": "...",
  "date": "2026-03-20",
  "location": "第82页",
  "type": "historical_fact"
}
```

`type` 可选：

- `historical_fact`；
- `management_statement`；
- `third_party_forecast`；
- `analyst_estimate`。

## 8.5 Assumption

```json
{
  "id": "asm_001",
  "variable": "业务A销量增长率",
  "value": 0.12,
  "period": "FY2027",
  "source": "industry_analysis",
  "owner": "industry_task",
  "status": "approved",
  "version": 1
}
```

---

# 九、状态机设计

技术面和基本面共用任务级基础设施，但使用独立模块状态机。

## 9.1 总任务状态

```text
CREATED
→ RESOLVING_SECURITY
→ ROUTING
→ RUNNING
→ REPORTING
→ COMPLETED
```

异常状态：

```text
WAITING_FOR_DATA
RETRYABLE_FAILED
FAILED
CANCELLED
HUMAN_REVIEW_REQUIRED
```

## 9.2 技术面状态

```text
TECH_RESEARCHING
→ KRONOS_ANALYZING
→ TECH_ASSEMBLING
→ DONE
```

## 9.3 基本面状态

```text
LEAD_PLANNING
→ TASK_DISPATCHING
→ SPECIALIST_ANALYZING
→ REVIEWING
→ LEAD_APPROVING
→ FUNDAMENTAL_WRITING
→ DONE
```

状态转换由 Orchestrator 控制，不由 Agent 自行修改。

---

# 十、工具调度策略

## 10.1 核心原则

```text
Lead 定义研究目标
→ 专业节点自主选择工具
→ Runtime 控制工具权限
→ Orchestrator 控制执行顺序
```

## 10.2 工具注册表

每个工具注册：

```json
{
  "tool_name": "financial_data",
  "description": "获取标准化财务数据",
  "supported_tasks": [
    "financial_analysis",
    "valuation_analysis"
  ],
  "input_schema": {},
  "output_schema": {},
  "timeout": 60,
  "cost_level": "low",
  "fallback_tools": []
}
```

## 10.3 工具选择顺序

```text
内部结构化数据库
→ 确定性计算脚本
→ 官方公告和财报
→ 外部数据接口
→ 网页检索
→ LLM 分析
```

能够通过代码或结构化数据完成的任务，不交给 LLM 计算。

## 10.4 工具权限

### Technical Research Agent

使用 Full Agent Profile，可以调用：

- 行情数据工具；
- 证券信息工具；
- 技术指标脚本；
- 必要的外部检索工具；
- 图表工具。

它可以在节点内部自主决定是否补充数据，但不能启动 Kronos 或 Technical Assembly 节点。

### Kronos Agent

不使用 Pi Runtime，只能调用：

- 标准化行情数据接口；
- Kronos 推理工具。

### Technical Assembly Agent

使用 Constrained Agent Profile，只能读取前两个节点的结果，不能调用检索、指标计算或 Kronos 工具。

### Lead Agent

使用 Full Agent Profile，可以：

- 初步检索；
- 创建任务；
- 修改研究主线；
- 验收结果；
- 创建返工任务。

Lead 不指定专业节点的具体工具，也不能绕过 Orchestrator 直接启动下游节点。

### Specialist Runtime

专业节点根据 Task、上下文、数据缺口和工具描述自主选择工具。实际可用工具由 AgentProfile 和 ToolRegistry 共同限制。

### Fundamental Writer Agent

使用 Constrained Agent Profile，不能调用检索、财务计算或估值工具，只能读取已批准结果。

## 10.5 工具失败处理

```text
工具失败
→ 自动重试
→ 调用备用工具
→ 返回 missing_information
→ Lead 决定补充任务、降级输出或人工处理
```

---

# 十一、Review 与验收

## 11.1 硬校验

由程序完成：

- JSON Schema 校验；
- 引用 ID 是否存在；
- 数字和单位是否一致；
- 假设版本是否一致；
- 数据截止时间是否符合要求；
- 财务数据是否基本勾稽；
- Writer 是否使用未批准结果。

## 11.2 软校验

MVP 由 Lead Agent 完成：

- 是否回答研究问题；
- 是否存在过度推断；
- 是否忽略反面证据；
- 是否与正式主线冲突；
- 是否需要返工；
- 是否可以交给 Writer。

后续可拆分独立 Review Agent。

---

# 十二、可靠性和安全设计

系统需要保存：

- 数据截止时间；
- 数据版本；
- Prompt 版本；
- Policy 版本；
- 模型版本；
- 工具版本；
- 报告版本。

安全要求：

- 网页和 PDF 内容视为不可信数据；
- 文档内容不能修改系统指令；
- 工具调用使用白名单；
- URL 访问需要防止 SSRF；
- 所有任务设置超时、最大重试和成本上限；
- 状态写入必须支持幂等和崩溃恢复；
- Pi Session 只作为临时工作上下文，数据库是唯一正式状态源；
- 每个任务使用独立 Session，禁止不同专业任务长期共用会话；
- 任务级重试由 Orchestrator 控制，避免 Pi 内部重试与外部重试形成无限循环；
- Pi 输出必须先通过 Schema 校验，才能写入正式 Research State。

---

# 十三、MVP 范围

MVP 实现：

- 单一股票市场；
- 股票名称和代码解析；
- 统一 Pi Core、PiAgentAdapter 和 AgentProfile；
- 技术面三个 Agent；
- Technical Research Full Agent Profile；
- 技术指标计算脚本；
- Kronos 模型调用；
- 技术面报告；
- 基本面 Lead Full Agent Profile；
- Specialist Runtime；
- Business、Industry Full Agent Profile；
- Financial、Valuation Constrained Agent Profile；
- Business、Industry、Financial、Valuation 任务类型；
- Evidence 和 Assumption 管理；
- 基本面 Writer Agent；
- 基础 Review；
- 技术面和基本面独立状态机；
- 基本面报告。

MVP 不实现：

- 综合分析；
- 综合评分；
- 技术面和基本面合并报告；
- 自动交易；
- 多市场统一适配；
- 独立 Review Agent；
- 多种高级投资风格插件。

---

# 十四、开发优先级

1. 定义 Research State、Task、Output、Evidence、Assumption Schema；
2. 实现证券解析、异步任务框架和 Orchestrator；
3. 封装 PiAgentAdapter、AgentProfile、ToolRegistry 和 OutputValidator；
4. 跑通一个 Full Agent 和一个 Constrained Agent；
5. 实现状态机、重试、版本和失效机制；
6. 接入行情数据和技术指标脚本，完成技术面三个节点；
7. 实现基本面 Lead Agent 和 Specialist Runtime；
8. 实现 Evidence、Assumption 和渐进式上下文加载；
9. 实现 Review、Writer、Kronos 评估、运行监控和成本统计；
10. 最后扩展投资风格插件和更多市场。

---

# 十五、核心设计原则

- 技术面和基本面使用不同协作结构；
- 确定性计算交给脚本，LLM 负责分析和写作；
- Lead 定义目标，专业节点自主选择工具；
- 同一 Pi Core 通过 AgentProfile 提供不同自治程度；
- Pi 管理节点内部循环，Orchestrator 管理节点顺序；
- Runtime 控制权限，状态机控制顺序；
- Agent 共享结构化状态，不共享完整推理上下文；
- Writer 只读取已验收结果；
- 上下文通过引用渐进式加载；
- 所有关键结论必须能够追溯到 Evidence 或 Assumption；
- MVP 只产出技术面报告和基本面报告；
- 不 Fork Pi Core，不维护两套 Pi，通过适配层和配置完成改造。
