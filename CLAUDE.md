<!--
parity-ids: core-versioning-v2
对等文件: .cursor/rules/versioning.mdc
改任一侧必须同步另一侧并 bump 两侧版本号；校验: bash ../scripts/check-agent-config-parity.sh
-->

# CLAUDE.md — bifrost-trade-core

> Legacy `bifrost-trader-engine` 已按 spine **D8**（2026-06-29）归档移出工作区。工作区事实基线见 `../AGENT_FACTS.md`。

与本项目用户对话一律使用中文回复（无论用户用何种语言提问）；UI 字符串与代码标识符使用 English。

## 职责范围

本 repo 是 **`bifrost-core` Python 共享库** (`src/bifrost_core/`) — 被所有其他后端 repo pip install 后引用：

- `config/` — YAML 配置加载（Settings、环境合并）
- `core/` — 工具函数（日志、Redis URL 解析）
- `persistence/` — PostgreSQL sink、DDL、账户同步
- `portfolio/` — 持仓模型、Greeks 聚合、多账户
- `ib_operator/` — IB Operator RPC 客户端（client 侧）
- `monitor/` — 状态读取层（供 API 后端查询 DB）

**本 repo 不包含任何业务进程或应用入口**。交易 daemon 归属 `bifrost-trade-worker`。

## 命令

```bash
make install-dev    # pip install -e ".[dev]"
make test           # pytest，跳过 ib/db 依赖测试
make test-all       # 所有测试（需要 IB 和 PostgreSQL）
make lint           # ruff check
make db-init        # 初始化/刷新 PostgreSQL schema
```

## 架构关键点

- `persistence/postgres_sink.py` — StatusSink 的唯一实现，写 daemon 状态快照
- `portfolio/` 的模型被 API 后端 (`bifrost-trade-api`) 直接 import
- `ib_operator/` 仅是 RPC client 侧封装；生产侧执行由 **Platform IB Gateway Plugin → `redis-ib`** 承接（`bifrost-trade-socket` 的 Operator 为半退役参考实现）

## 版本发布规范

- 修改 `src/bifrost_core/` 中的共享库后，必须 bump `pyproject.toml` 中的 version（当前 **0.20.1**）
- 其他 repo 通过 git tag 安装：`pip install git+https://github.com/ORG/bifrost-trade-core.git@v0.x.x`
- 破坏性变更需要同步更新所有依赖 repo 的 pyproject.toml
- **0.20.1**: four defects the 0.19/0.20 review surfaced. Cover is allocated once across a
  symbol's short calls (two legs each claimed the same shares: 165,496 committed against
  91,942 of stock, and both read covered while four contracts were naked); option mids are
  keyed by expiry so a roll's two same-strike legs stop sharing one price; a zero stock cost
  basis is no longer treated as missing; and a stress row with any intrinsic fallback is
  labelled `mixed_intrinsic` on every row rather than only the IV-shocked ones.
- **0.20.0**: returns are measured forward, over committed capital. The payoff is
  scoped to the shares the options actually cover (`covered_shares_modeled`), CAR for a
  covered call is the shares' market value rather than their sunk cost, and
  `annualized_return_on_car` is now the if-called return with `annualized_static_return`
  alongside it. Owner decision 2026-09-05: current-market-value convention. RKLB went
  from 5,857% to static 18% / if-called 107%. Values change; new fields are additive.
- **0.19.0**: portfolio model correctness — option `avg_cost` is loaded per SHARE (IB reports
  per contract), fixing a 100x overstatement of every option payoff / CAR / stress figure;
  stress grid spans ±15% (portfolio-margin range) and carries a 0% baseline row plus
  `pnl_change` measured from it; Greeks use each leg's own expiry instead of the group's
  farthest. Values change — response shape is additive (`pnl_change`).
- **0.18.2**: `update_one_execution` / insert / delete write `raw_broker.*` on Golden Source (not `brokerage.*`)
- **0.18.1**: Flex/GS writes use `raw_broker.*` (`GOLDEN_*`) — `write_account_executions_to_db` / commissions / transactions no longer insert into `brokerage.*` on Golden Source
- **0.18.0**: Wave 11 BREAKING — DROP `settings.ib_flex_*_token`; Flex Plugin Secret-only tokens
- **0.17.2**: Wave 10 — remove Wave 1 `_upgrade_gate_safety_strategy` dead path; `ensure_dim_enum_types()` from catalog; CREATE TABLE uses `dim_*_t` enums directly
- **0.17.1**: fix — add `pydantic>=2.0` dependency for `gate_params.py`
- **0.17.0**: Wave 9 — strategy child tables + gate flat cols → jsonb; `strategy_dim` → enum types + catalog; `migrate_wave9_strategy_collapse()`
- **0.16.0**: Wave 8 — `settings.active_*_id` FK ON DELETE SET NULL; Flex token columns DEPRECATED
- **0.15.2**: Wave 6.3 — `validate_settings_active_refs()` for settings `active_*_id` write guard; Golden Source canonical docs
- **0.15.1**: Wave 6.1 config hygiene — remove dead `ops.celery.*` / `use_for_celery_bars` from `config.yaml.example` (Trade Celery retired Wave 5)
- **0.15.0**: Wave 6 DB hygiene — `ops_audit_log` retired; actuation audit routed to platform-api
- **0.14.0**: Wave 5 Celery removal — dropped `celery_redis_url_from_config()` + monitor self_check Celery segment from `derive_health_roll_up`
- **0.13.0**: Wave 4 DB hygiene — `ops_audit_log` partitioned by month (`timestamptz`), 90-day partition drop on ensure_tables; Flex token columns retained as env-fallback only
- **0.12.0**: Wave 3 DB hygiene — drop `strategy_history` (DDL/reader/writer); extend `raw_broker.transactions` UNIQUE to `(account_id, ts, amount, type, report_date)`
- **0.11.0**: Wave 2 DB hygiene — strategy_template_param/characteristic + strategy_structure_meta folded into parent jsonb; ops_audit_log DDL owned by core; preference_* retained (live FE/API consumers)
- **0.10.11**: Wave 1 DB hygiene — gate_safety_state/intent/guard merged then retired (Wave 9 params_json)
- **0.10.10**: `preference_data_gap_ack` retired — dropped on startup; Golden Source `ops_jobs.data_source_void` is authoritative

## 数据库规范

- 开发库：`bifrost_dev`，生产库：`bifrost_prod`
- 表命名前缀：`daemon_`、`account_`、`contract_`、`strategy_`、`gate_safety_`、`job_`、`preference_`
- FK 列名与被引用 PK 名完全一致
- `gate_safety_strategy`：metadata 标量 + **`params_json`**（Wave 9）；六个 `dim_*` 列为 `dim_*_t` enum（Wave 10）
- DDL 变更必须在 `docs/DATABASE.md` 的 §6 变更日志中记录

## 测试标记

- `@pytest.mark.ib` — 需要 IB 实时连接
- `@pytest.mark.db` — 需要 PostgreSQL 连接
- 默认 CI 跑：`pytest -m 'not ib and not db'`
