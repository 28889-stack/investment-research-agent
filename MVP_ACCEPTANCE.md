# MVP 发布验收记录

## 结论

**当前状态：MVP Code Complete。**

验收日期为 2026-08-06。当前环境没有 Pi/LLM、Tavily 或 Kronos Live 配置，因此未执行完整 Live 技术面/基本面 Run，也不将 Mock 或 AKShare 局部抽查冒充为 Live 端到端验收。

## 环境与运维命令

| 项目 | 结果 |
| --- | --- |
| Python | 3.13.9 |
| Node.js | v24.12.0 |
| doctor | 通过；10 Profiles，11 Tools，两条工作流可构建 |
| backup | `backups/mvp_backup_20260806_015828` |
| restore-check | valid；3 个文件、SHA/大小、两个 SQLite integrity/必需表通过；当前库没有已完成报告，报告引用数为 0，自动化测试另覆盖 1 条报告引用 |
| Docker | 未验证：当前主机没有 `docker` 命令 |

## 两个标的验收

选择 600519.SH 作为业务结构相对直观的标的，000001.SZ 作为受监管且财务结构更复杂的银行标的。选择仅用于系统验收，不包含投资结论。

| 标的 | AKShare Live 数据抽查 | Technical 正式报告 | Fundamental 正式报告 | Evidence / Assumption / 免责声明 |
| --- | --- | --- | --- | --- |
| 600519.SH | 公司资料、5 期财务数据、750 条行情，截止 2026-08-06 | Mock 通过，0.756s | Mock 通过，0.243s | 3 Evidence、3 Assumption，检查通过 |
| 000001.SZ | 公司资料、5 期财务数据、750 条行情，截止 2026-08-06 | Mock 通过，0.199s | Mock 通过，0.112s | 3 Evidence、3 Assumption，检查通过 |

财务数字和估值输入的结构/引用边界由现有确定性 Artifact 回归测试验证。真实 Pi Writer 的解释质量与 Tavily Evidence 来源仍等待 Live 验收。

## 队列、恢复和性能记录

- 同时创建 4 个任务后，单 Worker 按创建顺序逐个完成，没有重复领取。
- 独立 Uvicorn 进程 + 独立 Worker 进程的 HTTP Mock 验收通过：两类任务均为 COMPLETED，页面 API 可读取正式报告。
- 处理期间 `/api/runs/{run_id}`、历史列表和 `/api/readiness` 均可访问，未观察到 SQLite 持续锁死。
- 连续 3 个 Mock 任务耗时：Technical 0.826s、Fundamental 0.207s、Technical 0.201s。
- 单 Run 峰值 Artifact 大小：218,664 bytes。
- 3 个连续任务后 SQLite 数据库占用：163,840 bytes。
- 取消后不启动新节点、Worker 重启恢复、Checkpoint 按 run_id 隔离、STALE 返回 409 均有自动回归测试。

## Live 状态

| 能力 | 结果 |
| --- | --- |
| AKShare | 通过两个标的的真实行情、公司和财务数据抽查 |
| Tavily | 未验证，API Key 未配置 |
| Pi/LLM | 未验证，provider/model/API Key 未配置 |
| Kronos | 未验证，Live 模型未配置 |
| Technical Live Run | Live smoke 预检失败并退出 1，未执行 Mock 回退 |
| Fundamental Live Run | Live smoke 预检失败并退出 1，未执行 Mock 回退 |

本轮最终复验中，AKShare 基本面 Live 测试通过；行情 Live 重试被本机 `127.0.0.1:1082` 代理连接失败阻断。表中两个标的的 AKShare 数据是本阶段较早成功抽查记录，不将本次网络失败隐藏为通过。

## 发布前剩余阻断项

1. 配置 Pi/LLM、Tavily 和 Kronos Live。
2. 重新执行两个标的的 technical/fundamental Live smoke，完成 Evidence 来源与财务/估值人工抽样核对。
3. 在安装 Docker 的环境执行 `docker compose config/build/up` 及 Web/Worker 重启验收，或正式选择 README 中的非 Docker 单机部署。
