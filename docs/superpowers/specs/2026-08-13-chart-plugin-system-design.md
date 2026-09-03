# 投研报告图表插件系统设计

日期：2026-08-13

## 1. 目标与边界

为基本面和技术面报告增加可扩展的原生 HTML 图表能力，同时保持现有数据可信边界、离线可用性和工作流容错能力。

本设计遵循以下原则：

- 图表用于支撑研究论点，不以图表数量作为完成指标。
- 基本面由 Writer Planning 规划图表，插件负责取数、校验和生成图表规格。
- 技术面只为本次实际识别出的形态生成图表。
- 同一指标域的多个技术形态可以共用一张图，但每个形态必须有独立标记和说明。
- 图表使用原生 HTML Canvas，不依赖 PNG、SVG、CDN 或运行时接口。
- Evidence 数据提取、图表生成或渲染失败不得中断报告。
- Agent 不直接填写、补齐或计算图表数值。

## 2. 总体架构

系统新增内部 `Chart Plugin Registry`。它不是外部 Codex 插件，而是应用内可注册、可测试的图表能力层。

基本面链路调整为：

```text
Lead Synthesis
→ Writer Planning（章节计划 + visual_plan）
→ Fundamental Chart Materialization
→ Business / Industry / Financial Section Writers
→ Final Synthesis
→ Composer
→ 原生 HTML
```

技术面链路调整为：

```text
行情与指标计算
→ 代码识别 PatternSignal
→ Technical Chart Materialization
→ Technical Assembly
→ 原生 HTML
```

Writer Planning 和技术形态识别只表达“需要画什么”。插件负责确定性地回答“能否画、使用什么数据、如何形成统一图表规格”。HTML Renderer 只负责展示，不参与研究判断。

## 3. 统一图表协议

所有基本面和技术面插件统一输出 `ChartSpec`：

```text
chart_id                 图表唯一标识
section_id               所属报告专题
plugin_id                生成该图表的插件
chart_type               line/bar/stacked_bar/area/candlestick/combo/
                         band/waterfall/timeline
title                    图表标题
analytical_purpose       图表服务的研究问题
labels                   横轴标签
series                   数据序列
unit                     主单位
secondary_unit           可选副轴单位
annotations              事件、阈值、交叉点、突破点等标记
explanation              与图表绑定的解释
observation_points       后续确认条件或失效观察点
source_notes             来源、口径和日期说明
evidence_ids             使用的 Evidence
assumption_ids           使用的 Assumption
data_lineage             每个序列或数据点的来源路径/引用
status                   generated/skipped
skip_reason              跳过原因，仅进入审计产物
```

`series` 和 `annotations` 使用受限枚举结构，不允许插件注入任意 JavaScript、HTML 或 CSS。图表标题、说明和来源文本继续经过现有 HTML 安全处理。

## 4. 基本面 Writer Planning

在 `WriterPlanOutput` 中新增 `visual_plan`，每项为 `PlannedVisual`：

```text
visual_id
section_id
plugin_id
analytical_question
source_mode              structured/evidence/mixed
metric_keys
allowed_evidence_ids
allowed_assumption_ids
preferred_chart_type
time_range
unit_hint
placement                before_section/after_claim/after_body
caption_focus
priority
```

Writer Planning 可以规划图表类型、研究用途、引用范围和摆放位置，但禁止输出数值序列。`visual_components` 仅为读取旧 Artifact 保留；新运行的图表执行只认 `visual_plan`，不从旧字段推断或补造计划。

规划规则：

- 图表必须服务于某个报告论点，不能只为装饰。
- 优先覆盖趋势、结构、对比、敏感性和时间顺序等适合视觉表达的信息。
- 不设置最低或最高成图数量硬指标。
- 同一数据和同一论点不规划机械重复图表。
- 无可用数据时允许不规划图表。
- Evidence 图表只能使用分配给该专题的 Evidence ID。

## 5. 基本面图表物化

新增 `build_fundamental_visuals` 节点，位于 Writer Planning 和三个 Section Writer 之间。

### 5.1 结构化数据插件

结构化插件只读取受信产物，例如：

- `financial_data.json`
- `financial_metrics.json`
- `valuation_result.json`
- `assumptions.json`

首批插件：

- `financial_performance_trend`：收入、利润及增长趋势。
- `profitability_quality`：毛利率、净利率、ROE 等。
- `cashflow_capex`：经营现金流、资本开支和自由现金流。
- `balance_sheet_health`：现金、债务、杠杆和偿债能力。
- `valuation_snapshot`：PE/PB/PS 与 DCF 区间、敏感性。

所有计算继续由受信 Python 完成。插件只复用现有指标或执行明确注册的简单转换，不允许 Agent 重算财务和估值。

### 5.2 Evidence 数据提取插件

Evidence 图表用于产量、产能、业务结构、行业供需、商品价格、项目进度等非标准化信息。

流程为：

```text
visual_plan
→ 按 allowed_evidence_ids 读取限长 Evidence 摘录
→ 受约束 Chart Data Extractor 输出候选数据点
→ Python 校验
→ 对应图表插件生成 ChartSpec
```

Chart Data Extractor：

- 独立 Session、零工具、一次迭代。
- 只读取当前图表计划及被允许的 Evidence 摘录。
- 不创建 Evidence 或 Assumption。
- 不补齐缺失期间，不做无来源预测。
- 每个数据点必须包含期间、值、单位和 Evidence ID。

确定性校验要求：

- Evidence ID 必须存在且属于该图表白名单。
- 原始数值、期间和单位必须能在对应摘录中定位。
- 相同序列的单位和口径必须一致。
- 派生比率或增速只能由注册的 Python 转换计算，并记录公式与输入来源。
- 与 `financial_data/metrics/valuation` 冲突时，以结构化权威数据为准。
- 校验失败将该图表标记为 `skipped`，不影响其他图表和报告正文。

首批 Evidence 插件：

- `business_mix`：分部收入、利润或销量结构。
- `production_capacity`：产量、产能及投产节奏。
- `industry_supply_demand`：行业供需和库存趋势。
- `commodity_price_cycle`：商品价格周期。
- `project_timeline`：重点项目、并购或投产时间线。

Evidence 图表数据属于有来源归因的研究事实，不得覆盖正式财务指标，也不得被误标为脚本计算的权威财务口径。

## 6. Writer 与 Final Synthesis 的图表信息

每个 Section Writer 只接收分配给自己专题的：

- 已成功生成的 ChartSpec 安全摘要；
- 图表标题、核心序列和来源说明；
- 图表解释目标和摆放位置；
- 被跳过图表的简短状态，不包含原始失败输出。

Writer 可以围绕图表已经呈现的趋势或结构进行论证，但不能修改图表数值。

Final Synthesis 只接收图表目录与布局摘要，用于避免编辑后出现“正文谈 A、图表放在 B”的错位。它不能修改图表数据、来源或插件结果。

Composer 按最终章节顺序和 `placement` 插入图表。若目标章节不存在、图表状态不是 `generated` 或渲染不支持该规格，则跳过该图表并继续输出报告。

## 7. 技术形态图表

技术面由确定性代码把当前字符串形态升级为结构化 `PatternSignal`：

```text
pattern_id
name
detected_at
chart_family
trigger_values
trigger_rule
explanation_inputs
confirmation_rule
invalidation_rule
```

保留现有 `patterns: list[str]` 作为兼容字段，但新图表和说明以 `PatternSignal` 为准。

图表域映射：

| 图表域 | 实际识别形态 | 图表内容 |
|---|---|---|
| `price_trend` | 20日突破/跌破、均线多头/空头排列 | K线、SMA5/10/20/60、突破区间和触发点 |
| `macd` | MACD金叉/死叉 | DIF、DEA、柱状图、交叉点 |
| `rsi` | RSI超买/超卖 | RSI 曲线、30/70 阈值区间和触发点 |
| `volume_price` | 放量上涨/下跌 | 价格变化、成交量、20日均量和放量倍数 |

生成规则：

- 只为本次实际识别出的形态选择图表域。
- 同一图表域只生成一张图，允许标注多个形态。
- 每个形态在图中有独立 annotation。
- 每个形态在图下有独立说明，包括触发条件、触发值、含义、确认条件和失效观察点。
- 说明由注册模板和权威指标值生成，Agent 只能在 Technical Assembly 中做克制解释，不能改变触发事实。
- 单个图表失败只省略该图，不删除形态文字说明，也不使技术报告失败。

技术报告不再依赖 `technical_chart.png`。新增 `technical_visuals.json`，页面和单文件导出均使用相同 Canvas 数据。

## 8. Chart Plugin Registry

每个插件实现统一接口：

```python
class ChartPlugin(Protocol):
    plugin_id: str
    supported_chart_types: set[str]
    source_modes: set[str]

    def supports(self, plan, available_inputs) -> bool: ...
    def validate(self, plan, inputs) -> ValidationResult: ...
    def build_chart_spec(self, plan, inputs) -> ChartSpec: ...
    def build_explanation(self, plan, spec) -> ChartExplanation: ...
```

Registry 负责：

- 拒绝未知 `plugin_id`。
- 检查图表类型和数据来源是否匹配。
- 为每个图表隔离异常。
- 统一记录 generated/skipped 状态。
- 保证输出顺序稳定，支持恢复和 Manifest SHA 校验。

禁止插件读取任意本地路径、发起网络请求或调用 Agent。Evidence 提取是独立受控节点，不隐藏在插件内部。

## 9. 原生 Canvas Renderer

前端扩展现有 `report-charts.js`，建立按 `chart_type` 分发的 Renderer Registry。首批支持：

- line、area；
- bar、stacked_bar；
- combo 双轴组合；
- candlestick；
- band 区间图；
- waterfall；
- timeline。

公共能力包括：

- 响应式 Canvas 和高 DPI 缩放；
- 图例开关；
- 悬浮提示；
- 单位和口径显示；
- annotation 标记；
- 空值断点处理；
- 键盘可访问的图表摘要；
- Canvas 不可用时展示文本数据摘要。

导出的 HTML 内嵌 CSS、Renderer JavaScript 和 ChartSpec JSON，不使用外部资源。应用内报告可继续引用静态 `report-charts.js`，但导出文件必须内联同版本代码。

## 10. Artifact 与依赖关系

基本面新增或调整：

```text
writer_plan.json                 包含 visual_plan
fundamental_chart_candidates.json Evidence 提取候选及校验状态
report_visuals.json              已物化的统一 ChartSpec
```

`report_visuals` 的依赖调整为：

```text
writer_plan
financial_data
financial_metrics
valuation_result
assumptions
evidence
```

技术面新增：

```text
technical_visuals.json
```

它依赖标准化行情、`technical_indicators.json` 和结构化 PatternSignal。旧 `technical_chart.png` 从最终报告和导出依赖中移除。

Graph State 仍只保存路径，不保存图表序列或 Evidence 正文。

## 11. 失败处理

失败粒度按单张图表隔离：

- Planner 未规划图表：正常生成无图报告。
- 未知插件：该图表 skipped。
- Evidence 数据不足或口径冲突：该图表 skipped。
- Chart Extractor 失败：所有依赖该次提取的图表 skipped，正文继续。
- 单插件异常：仅该图表 skipped。
- Canvas 不支持或浏览器上下文缺失：显示文本摘要。
- 全部图表失败：报告仍然完成，不转入 FAILED 或 HUMAN_REVIEW_REQUIRED。

`skip_reason` 只进入审计产物和运行诊断，不在成品报告中堆叠技术错误。必要的数据完善方向仍可由报告已有“优化建议”表达。

## 12. 安全与可信边界

- Writer Planning、Chart Data Extractor 和 Writer 都不能创建 Evidence 或 Assumption。
- Agent 输出不得包含任意脚本、HTML、CSS 或本地文件路径。
- ChartSpec 只允许白名单字段、颜色和图表类型。
- Evidence 摘录继续视为不可信数据，其中的指令不得执行。
- API Key、Prompt、原始模型输出和完整来源正文不得进入图表 Artifact 或 HTML。
- 图表中的交易评级、目标价或预测必须保持第三方归因，不能改写成系统自身指令或收益承诺。

## 13. 测试与验收

### 单元测试

- Writer Plan 可规划图表但不能携带数值序列。
- Registry 拒绝未知插件和不支持的图表类型。
- 每个插件对缺失值、单位冲突和空序列正确降级。
- Evidence 数据点必须能回溯到允许的 Evidence。
- 技术 PatternSignal 到图表域的映射完整。
- 同一技术图表域正确合并多个形态且保留独立 annotation。
- Renderer 对所有 ChartSpec 类型生成可用 Canvas 或文本降级。

### 工作流测试

- `build_fundamental_visuals` 位于 Writer Planning 和 Section Writers 之间。
- Section Writer 只能看到所属专题图表。
- Final Synthesis 只能看到图表目录与布局摘要。
- 图表失败不阻断 Writer、Composer 和报告完成。
- Evidence 或权威财务输入变化会正确传播 STALE。
- 恢复时复用 current 的图表 Artifact，不重复提取。

### 端到端验收

- 基本面 Mock 报告包含由 Writer Plan 规划的多类型原生图表。
- 基本面无可用 Evidence 图表数据时仍生成完整报告。
- 技术面只渲染本次识别形态对应的图表域。
- 每个技术形态都有图中标记和独立说明。
- 单文件 HTML 断网打开后图表、图例和悬浮提示仍可使用。
- Web 与独立 Worker 分别完成 technical Mock 和 fundamental Mock。

## 14. 实施顺序

1. 建立 `ChartSpec`、`PlannedVisual`、`PatternSignal` 和 Registry 基础协议。
2. 扩展 Writer Planning，并接入结构化基本面插件。
3. 增加 Evidence Chart Data Extractor 与校验器。
4. 将 scoped 图表摘要交给 Section Writers 和 Final Synthesis。
5. 扩展 Canvas Renderer 和 Composer 布局。
6. 将技术形态迁移为 PatternSignal 并生成四类技术图表。
7. 移除最终报告对 PNG 技术图表的依赖，完成恢复、导出和端到端验证。
