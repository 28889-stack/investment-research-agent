# 检索结果会话与 Evidence 效率设计

## 目标

修复 Business、Industry 和 Deep Research 在多轮搜索后无法读取旧 `src_xxx`、不同轮次 ID 重用可能错配来源的问题，并在不限制 Provider 丰富度的前提下降低重复搜索、重复下载和工具上下文体积。

## 结果 ID 与生命周期

- Provider 内部仍可使用 `src_001` 等编号；工具边界为每次搜索生成公开唯一 ID。
- 普通研究使用 `src_r01_001`、`src_r02_001`；Deep 使用带任务卡的轮次 ID。
- 公开 ID 仅在当前 Agent attempt 内有效，但当前 attempt 的全部成功搜索轮次均可读取。
- Worker 重启或 Agent retry 不复用临时搜索 ID；已生成的 `ev_xxx` 继续由 Evidence Store 持久化。
- 达到轮次、时间或工具预算上限时只返回收束通知，不清除已成功搜索的结果。

## 搜索效率

- 同一 attempt 内按规范化 URL 过滤后续轮次已经返回的来源；第一轮结果仍保留可读。
- 后续搜索没有新增 URL 时返回正常通知，提示基于已有结果收束，不抛检索失败。
- 返回给模型的搜索结果只包含限长摘要，不传递预取正文；内部缓存保留完整 `ResearchSource` 供读取工具使用。
- Provider 继续全部聚合，`sources` 仍只作为排序偏好，不新增来源硬限制。

## Evidence 复用

- `read_research_source` 下载前先按规范化 URL 查询当前 run 的 Evidence Store。
- 已存在同一来源时直接返回现有 Evidence ID，不重复访问 Reader 或公网。
- Evidence 仍由读取工具创建；搜索元数据不直接成为 Evidence。
- 同一来源可以被多个 Agent 和论点复用，但不复制正文或创建重复 Evidence。

## 安全与正确性

- 读取时按公开唯一 ID 精确解析原始来源，禁止将第一轮 `src_001` 误配到第二轮来源。
- 每个搜索轮次继续保留单来源一次、单轮最多四次读取限制。
- Attempt 隔离保持不变，跨 attempt 使用临时 ID 会返回明确错误。
- 搜索上限、来源不可用和无新增结果均应收束输出，不中断报告。

## 验收

- 两轮搜索并触发第三次上限通知后，前两轮结果仍可分别读取。
- 两轮返回的公开 ID 不重复，且读取结果与原 URL 一一对应。
- 第二轮重复 URL 不再次暴露，但第一轮 ID 仍可读取。
- 相同 URL 已形成 Evidence 后，再次读取不触发下载并返回同一 Evidence ID。
- Business、Industry、Deep 和 Lead 的既有工具权限、Provider 聚合与 Evidence 引用校验保持通过。
