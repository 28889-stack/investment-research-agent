# Railway 单服务部署

本项目在 Railway 运行为一个 Service：主进程提供 FastAPI，子进程运行唯一的研究 Worker。它不支持水平扩容；保持单实例，避免两个 Worker 同时访问 SQLite 和同一 Artifact 目录。

## 首次配置

1. 从 GitHub 导入分支 `agent/fundamental-report-pipeline-html`，Railway 会使用根目录的 `Dockerfile` 与 `railway.toml`。
2. 为 Service 添加一个 Volume，挂载路径固定为 `/app/data`。它保存 SQLite、Checkpoint、报告、Evidence Artifact 和 Hugging Face 模型缓存；容器其他目录不保证跨部署保留。
3. 生成公网 Domain，并把 Healthcheck Path 设置为 `/api/health`。
4. 在 Railway Variables 中设置下列变量。所有 API Key 与邀请码只放 Variables，绝不提交到仓库。

```text
APP_ENV=production
APP_HOST=0.0.0.0
ALLOW_PUBLIC_BIND=true
STORAGE_ROOT=/app
DATABASE_URL=sqlite:////app/data/research.db
ARTIFACTS_DIR=/app/data/artifacts
CHECKPOINT_DATABASE_PATH=/app/data/checkpoints.db
LOGS_DIR=/app/data/logs
BACKUPS_DIR=/app/data/backups
HF_HOME=/app/data/huggingface

ACCESS_AUTH_ENABLED=true
ACCESS_INVITE_CODE=<长期邀请码>
ACCESS_COOKIE_SECRET=<至少32字符随机值>
ACCESS_COOKIE_SECURE=true

PI_RUNTIME_MODE=live
PI_MODEL_PROVIDER=deepseek
PI_MODEL=deepseek-v4-pro
PI_API_KEY_ENV_NAME=DEEPSEEK_API_KEY
DEEPSEEK_API_KEY=<secret>

MARKET_DATA_MODE=live
MARKET_DATA_PROVIDER=akshare
FUNDAMENTAL_DATA_MODE=live
FUNDAMENTAL_DATA_PROVIDER=akshare
KRONOS_MODE=live
KRONOS_MODEL_NAME=NeoQuasar/Kronos-mini
KRONOS_DEVICE=cpu
KRONOS_SOURCE_DIR=/app/vendor/kronos

RESEARCH_SEARCH_MODE=live
RESEARCH_SEARCH_PROVIDER=aggregator
RESEARCH_SEARCH_PROVIDERS=official_crawler,akshare_news,akshare_reports,akshare_notices,keenable,firecrawl
RESEARCH_SEARCH_API_KEY_ENV_NAME=KEENABLE_API_KEY
RESEARCH_READER=firecrawl
RESEARCH_READER_API_KEY_ENV_NAME=FIRECRAWL_API_KEY
FIRECRAWL_API_KEY=<secret>
KEENABLE_API_KEY=<secret>
```

Keenable 通过 `RESEARCH_SEARCH_API_KEY_ENV_NAME=KEENABLE_API_KEY` 读取其密钥；Firecrawl 使用自己的 `FIRECRAWL_API_KEY`。如暂未启用 Firecrawl，从来源列表移除 `firecrawl` 并将 `RESEARCH_READER` 设为 `jina`。不要因为某个可选 Provider 缺少 Key 而关闭聚合检索。

## 运维边界

- 第一次技术面任务会将公开的 Kronos-mini 与 tokenizer 下载到 Volume；下载完成前该任务会等待或失败，之后的部署复用缓存。
- Railway 的 `PORT` 会自动优先于本地 `APP_PORT`，无需手动指定端口。
- 变更 `ACCESS_INVITE_CODE` 后重新部署，已有浏览器登录立即失效。变更 `ACCESS_COOKIE_SECRET` 同样会强制重新登录。
- Volume 绑定的服务部署会短暂中断；部署前应等待活动研究结束或在页面中取消。SQLite 不可用于多副本部署。
