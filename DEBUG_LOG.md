# Debug 日志

每次 Debug 追加一条，只记录现象、根因、修复、验证和关联任务。

---

## 2026-08-06 基本面任务连续失败

### 现象
基本面任务在 lead_planning / business_research 阶段反复失败。

### 根因
1. 巨潮自然语言检索无法匹配公告标题，返回 0 条。
2. Agent 重复搜索，耗尽 5 次工具预算，触发 TOOL_NOT_ALLOWED。
3. Bridge 会重新抛出已经被模型修正的历史工具错误，导致成功结果被丢弃。
4. read_research_source 的 evidence_type 缺少枚举约束，模型首次容易传非法值。
5. 常驻 Worker 仍运行修改前加载的 Python 模块，磁盘代码已更新但进程未加载。

### 修复
- 巨潮检索增加候选词分层与近期公告回退，单次请求失败继续尝试下一个候选。
- 约束 Lead、Business、Industry 三个 profile 的检索次数，保留一次工具预算余量。
- 修正 Bridge 工具错误恢复语义：普通工具错误允许修正或降级，仅权限和协议错误保持致命。
- 将 evidence_type 收紧为 Literal 枚举，工具 JSON Schema 自带合法值。
- 重启 Worker 加载最新代码与 profile。

### 验证
- 离线测试：274 passed, 4 skipped（live 单独 deselect）。
- Live 任务：COMPLETED。
- 报告状态：current，evidence 4 条、assumption 4 条。
- HTML 导出：200，单文件，无 /static/、无 script、无外部 API 依赖。

### 关联任务
- 失败任务：8cdc49d2、fbeefe8b、d7e3cce0
- 成功任务：19f4f1f4

### 复现与排查约定
确认源码修改时间 → 确认 Worker PID 和启动时间 → 修改时间晚于启动时间则重启 Worker → 创建新任务验证 → 不用旧失败任务判断修复结果。

---

## 2026-08-09 多源检索适配器 Tavily 测试越过假 Client

### 现象
全量回归中 `test_live_search_retries_one_transient_network_failure` 失败：日志显示真实 `POST https://api.tavily.com/search` 401，而非注入的假 Client，断言 `calls == 2` 不成立。

### 根因
`TavilySearchProvider.__init__` 把 `client_factory` 默认值写为 `httpx.Client`（函数默认参数在类定义时求值一次）。测试通过 `monkeypatch.setattr("app.retrieval.tavily.httpx.Client", Client)` 注入假类，但 provider 实例已捕获原始 `httpx.Client`，monkeypatch 被旁路，请求直连真实 Tavily API。

### 修复
`TavilySearchProvider.__init__` 改为 `client_factory: Callable[..., object] | None = None`，`search` 内 `client_factory = self._client_factory or httpx.Client`。默认值延迟到调用时按当前模块属性解析，monkeypatch 即时生效。

### 验证
- `test_fundamental_tools.py` 两项 Tavily 测试 + `test_tavily_retrieval.py` 全部通过。
- 全量 `.venv/bin/python -m pytest`：316 passed, 4 skipped。

### 关联任务
- 多源检索适配器重构（aggregator + akshare_news/reports/notices + tavily 独立 adapter）。

---

## 2026-08-09 lead_final_review gate 误判 HUMAN_REVIEW_REQUIRED

### 现象
aggregator + 4 子来源的 live fundamental smoke（600519.SH）卡在 `fundamental_writer` 前：`lead_final_review.ready_for_writer=False`，触发 `_require_human_review` → `HUMAN_REVIEW_REQUIRED`，`live_smoke.py` 抛 `RuntimeError("Live run did not complete: HUMAN_REVIEW_REQUIRED")`。报告未生成。

### 根因
`_lead_final_review` 的 `context_refs` 只含 `artifact:financial_research`（叙述），不含 `financial_data`/`financial_metrics`（受信 Python 边界计算的权威数字）。Lead 看不到权威财务来源，将"核心财务数字未在 evidence 摘录中引用"判为缺失信息，置 `ready_for_writer=false`。
- 实际上财务数字由 Python 边界计算（AKShare `get_financial_data` + `calculate_financial_metrics`），`financial_research` 是基于其生成的已校验叙述，`evidence` 仅用于业务/行业/风险等定性披露的来源佐证——财务数字无需在 evidence 中重复引用。
- `result_manifest.DEPENDENCIES["lead_final_review"]` 也未列 `financial_data`/`financial_metrics`，staleness 传播与 context 不一致。

### 修复
1. `app/fundamental/workflow.py` `_lead_final_review`：`context_refs` 增加 `artifact:financial_data` + `artifact:financial_metrics`；task prompt 显式声明二者是受信 Python 边界的权威财务数据、`financial_research` 是其已校验叙述、核心财务数字以这三者为准无需在 evidence 重复引用、各 approved_sections 均有已校验研究支撑时置 `ready_for_writer=true`。
2. `app/fundamental/result_manifest.py` `DEPENDENCIES["lead_final_review"]` 增加 `financial_data`、`financial_metrics`，使 staleness 传播与 context 一致。
- 不改权威边界：full agent 不重写财务数字；`_fundamental_writer` 的 gate 不变。

### 验证
- 离线：全量 `.venv/bin/python -m pytest` 316 passed, 4 skipped（含 `test_fundamental_final_integration.py` 12 项，writer 专属 context 不受影响）。
- Live smoke #1（aggregator，run 3e88c330）：COMPLETED，evidence 6 / assumption 3，`ready_for_writer=True`，`missing_information=0`，report 206 行无禁止交易指令，manifest 全 current。

### 关联任务
- aggregator 多源检索端到端 Live 验收。

---

## 2026-08-09 akshare_notices 参数颠倒导致该子来源恒失败

### 现象
aggregator live smoke 中 `akshare_notices` 子来源对 600519.SH 恒抛 `RESEARCH_SOURCE_FAILED: 东方财富公告检索失败`，被 aggregator 失败隔离跳过，实际只有 `official_crawler`（+部分 `akshare_news/reports`）生效。诊断时单独调用该 provider 直接 `KeyError: '600519'`。

### 根因
`AkshareNoticeProvider.search` 调用 `ak.stock_individual_notice_report(security="A股", symbol=code, ...)`，参数颠倒。akshare 源码里 `symbol` 是公告类别（取自固定映射 `{"全部","财务报告",...}`），`security` 才是股票代码；把代码传进 `symbol` → `report_map["600519"]` KeyError。失败隔离掩盖了它，所以不影响 smoke 完成，但该子来源从未真正贡献。

### 修复
`app/retrieval/akshare_notices.py`：改为 `security=code, symbol="全部"`（按公司过滤、类别全选），再用既有客户端关键词过滤收窄。补注释说明 akshare 签名。

### 验证
- 单 provider live：`akshare_notices` 对 600519 返回 5 条年度/半年报告公告，`source_kind=announcement`，URL 指向 `data.eastmoney.com/notices/detail/600519/...`。
- aggregator live：fan-out 真正触达 4 个 provider，结果含 `东方财富·公告·*` 多类别。
- 离线：`test_akshare_retrieval.py` 15 项全过（假 ak 不校验 security/symbol 取值，修复不破坏 mock）；全量 316 passed, 4 skipped。
- Live smoke #2（run eaa945fc）：COMPLETED，evidence 7 / assumption 5，evidence 全部来自 `akshare_notices` 的 `data.eastmoney.com` 公告（official_crawler 被重排靠后/未被读取），证明该子来源首次真正生效。期间出现两处 `OUTPUT_DIAGNOSTIC_DIR` 诊断文件（lead_planning `JSON_INVALID`、industry_research `FORBIDDEN_FIELD`），均经一次 repair 成功修复后 COMPLETED——诊断钩子按预期工作且未降级 Mock。

### 关联任务
- aggregator 多源检索端到端 Live 验收；OUTPUT_DIAGNOSTIC_DIR 诊断钩子首次在真实 Live 失败下捕获到 raw_output + error_code。
