# MVP 发布检查清单

> 检查日期：2026-08-06。当前结论：MVP Code Complete，不是 MVP Live Ready。

## 配置

- [x] `.env` 被 Git 忽略，镜像不复制 `.env`
- [x] development / staging / production 配置边界明确
- [ ] Pi/LLM Live provider、model 和 API Key 未配置
- [ ] Tavily API Key 未配置
- [ ] Kronos Live 模型名与权重缓存未配置
- [x] SQLite、Checkpoint、Artifact 目录通过 doctor

## 安全

- [x] 默认绑定 `127.0.0.1`，公网绑定需显式授权
- [x] 未配置 CORS 中间件，默认仅同源
- [x] API Key、Authorization、Cookie 和数据库密码脱敏测试通过
- [x] Storage 白名单、目录隔离和 Artifact 路径限制测试通过
- [x] Markdown 继续使用 Bleach 安全过滤
- [x] 既有 URL SSRF 防护回归测试保留

## 数据与 Live

- [x] AKShare Live：600519.SH 与 000001.SZ 行情、公司资料和财务数据抽查通过
- [ ] 最终复验的 AKShare 行情请求：本机 127.0.0.1:1082 代理不可用，需在发布机重跑
- [ ] Tavily Live：凭证未配置
- [ ] Kronos Live：模型未配置
- [ ] Pi/LLM Live：凭证未配置
- [x] AKShare 抽查的截止日期为 2026-08-06

## 流程

- [x] technical Mock 正式报告
- [x] fundamental Mock 正式报告
- [x] 取消、Checkpoint 恢复、STALE 重建、HUMAN_REVIEW_REQUIRED 回归测试
- [x] 队列上限 HTTP 429、跨进程单任务锁和 Worker 顺序领取
- [ ] technical Live 端到端：Pi/Kronos 未配置
- [ ] fundamental Live 端到端：Pi/Tavily 未配置

## 运维与部署

- [x] `python -m app.ops doctor` 退出码 0
- [x] 备份与 Worker 互斥，SQLite Backup API 备份成功
- [x] `restore-check` 在临时目录校验必需文件、表结构、SHA 与大小通过
- [x] Python 回归、依赖检查、compileall 和 diff check
- [x] Node Bridge 测试与 TypeScript 构建
- [ ] Docker Compose 实际 config/build/up：当前主机未安装 Docker
- [x] 非 Docker 单机启动方式已文档化

## 发布决策

代码和 Mock/本地运维验收可交付。在 Pi、Tavily、Kronos 凭证/模型补齐并完成两条 Live Run 前，不得将状态改为 MVP Live Ready。
