# 基本面研究分层与交互 HTML 报告设计

## 目标

将原始检索材料、研究结论、Lead 的叙事判断和 Writer 的写作规划拆分为独立、可恢复的 Artifact。正式报告升级为现代金融风格的单文件 HTML：离线打开时保留交互图表，缺失信息集中展示而不打断正文。

## 范围

- 在现有单 Worker、SQLite、局部 Artifact 和 Manifest 内扩展基本面工作流。
- 保留 `evidence.json` 作为 Evidence 的唯一权威存储；新增检索包只是完整索引和审计视图，不复制为另一套可编辑证据。
- 增加 Lead 主线节点与 Writer 计划节点；正式 Writer 保持 0 工具、独立 Session。
- 引入确定性的报告视觉配置和本地 Canvas 图表运行时；在线页与导出 HTML 共用同一配置。
- 不允许 Agent 输出任意 HTML、JavaScript、精确财务数字或交易指令/收益承诺。
- 不引入 CDN、前端框架、Redis、Celery、多 Worker 或外部图表服务。

## 目标链路

```text
lead_planning → business_research ┐
                                 ├→ lead_review → deep_research
                 industry_research┘
                                       ↓
                         assemble_retrieval_package
                                       ↓
financial_research → valuation_research → lead_final_review
                                       ↓
                                lead_synthesis
                                       ↓
                               writer_planning
                                       ↓
                              fundamental_writer
                                       ↓
                          write_fundamental_report
```

`retrieval_package.json` 收录本次 Run 的全部 Evidence ID、来源元数据、主题/来源节点和审计摘要。原始正文继续只存在于 `evidence.json`，不能整包注入 Agent Context。

## Artifact 与职责

| Artifact / 节点 | 责任 | 可读取内容 |
| --- | --- | --- |
| `business_research.json`、`industry_research.json`、`deep_research.json` | 研究简报：结论、论证、Evidence ID、风险、缺口 | 自己的工具返回与明确上游简报；不读取全量 Evidence |
| `retrieval_package.json` | 全量检索目录和审计索引 | `evidence.json`；确定性生成 |
| `lead_final_review.json` | 研究完整性审核与报告准入门槛 | 研究简报、受信财务/估值结果、短 Evidence 目录 |
| `lead_synthesis.json` | 报告主线、章节论点、资料采用/排除说明、允许引用集合、独立缺口板块 | 已审核的研究简报、权威数据和目录摘要 |
| `writer_plan.json` | 标题层级、段落目标、允许使用的论点/引用、视觉强调意图 | `lead_synthesis` 与权威安全摘要；0 工具 |
| `fundamental_writer.json` | 依照 Writer Plan 输出正文解释 | `writer_plan`、获准短摘要与权威数字安全摘要；0 工具 |
| `report_visuals.json` | 卡片、图表、数据序列和可用性状态 | 财务/指标/估值权威 Artifact；确定性生成 |
| `fundamental_report.html` | 最终可展示、可导出的离线交互报告 | 报告正文、视觉配置、内嵌样式与图表运行时 |

现有 `fundamental_research_package.md` 保留为审计阅读版，但不再作为 Writer 的工作上下文。

## 上下文与引用边界

- `ContextLoader` 为每种角色提供专用紧凑包，而不是通用 `_load_fundamental_artifacts` 全量装载。
- 检索 worker 的初始 Context 不含 `artifact:evidence`；读取来源只经受控工具完成，输出仅保留 Evidence ID。
- 财务、估值、Lead 审核、Lead 主线与 Writer 计划都使用研究简报和权威摘要；Evidence 仅以被引用 ID 的元数据/限长摘要提供。
- Lead Synthesis 产出章节级 `allowed_evidence_ids` 和 `material_usage`。Writer 与报告渲染只接受这些被批准的引用。
- 所有新增输出继续经过 Schema、symbol/as_of、Evidence/Assumption 引用校验和 Manifest SHA 校验。

## HTML 与图表

- 报告采用本地、无外部请求的 HTML/CSS/Canvas 运行时；导出文件内嵌全部样式、图表脚本和 `report_visuals.json` 数据。
- 视觉为现代机构金融研究：深海军蓝、暖白/灰、克制强调色、摘要 KPI 卡片、信息密度高的表格、清晰层级和移动端布局。
- 图表使用 Canvas 而非 PNG/SVG：悬浮提示、系列开关与响应式重绘均在浏览器本地运行。
- 图表只使用已验证的营收、利润、利润率、现金流、资产负债、估值和敏感性数值。空值对应系列被忽略；不在正文中插入“数据缺失导致无法继续”的说明。
- 缺失信息仅在独立的“信息缺口与后续核验”板块集中展现，不影响其余章节的连续叙事。

## Manifest、恢复与兼容性

- 新 Artifact 进入 `RESULT_ORDER` 与固定依赖图：检索包依赖 Evidence；Lead 主线依赖最终审核和研究产物；Writer Plan 依赖 Lead 主线；正式 Writer 依赖 Writer Plan；视觉和 HTML 报告依赖权威数据与 Writer 输出。
- Artifact 变更从最早失效节点重建；旧 Run 没有新 Artifact 时保持可读，并在恢复时按新节点顺序补建。
- 报告 API 返回最终 HTML；兼容字段可继续提供 Markdown 供旧客户端使用。

## 验收

- 多条长 Evidence 不会令财务、估值、Lead 或 Writer Context 超过 30,000 字符。
- Mock fundamental Run 产生新链路的全部 Artifact 和可用单文件 HTML。
- 导出 HTML 不包含网络 URL、运行时 API 请求或 PNG/SVG 图表依赖，且包含本地 Canvas 图表配置。
- Writer 仍为 0 工具，且最终 Prompt 保留交易指令/收益承诺限制。
- 全量 Python 测试、Pi Bridge 测试/构建、`pip check`、`git diff --check` 与技术/基本面 Mock Web+Worker 验收通过。
