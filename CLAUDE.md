# CLAUDE.md — bifrost-trade-core

> 本项目是 bifrost-trader-engine 重构的一部分。迁移进度见 `bifrost-trade-infra/docs/MIGRATION_TRACKING.md`。

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
- `ib_operator/` 仅是 RPC client 侧封装，实际执行在 `bifrost-trade-ib-edge` 的 Operator 进程

## 版本发布规范

- 修改 `src/bifrost_core/` 中的共享库后，必须 bump `pyproject.toml` 中的 version（当前 **0.15.0**）
- 其他 repo 通过 git tag 安装：`pip install git+https://github.com/ORG/bifrost-trade-core.git@v0.x.x`
- 破坏性变更需要同步更新所有依赖 repo 的 pyproject.toml
- **0.15.0**: Wave 6 DB hygiene — `ops_audit_log` retired; actuation audit routed to platform-api
- **0.14.0**: Wave 5 Celery removal — dropped `celery_redis_url_from_config()` + monitor self_check Celery segment from `derive_health_roll_up`
- **0.13.0**: Wave 4 DB hygiene — `ops_audit_log` partitioned by month (`timestamptz`), 90-day partition drop on ensure_tables; Flex token columns retained as env-fallback only
- **0.12.0**: Wave 3 DB hygiene — drop `strategy_history` (DDL/reader/writer); extend `raw_broker.transactions` UNIQUE to `(account_id, ts, amount, type, report_date)`
- **0.11.0**: Wave 2 DB hygiene — strategy_template_param/characteristic + strategy_structure_meta folded into parent jsonb; ops_audit_log DDL owned by core; preference_* retained (live FE/API consumers)
- **0.10.11**: Wave 1 DB hygiene — gate_safety_state/intent/guard DROP already idempotent via `_upgrade_gate_safety_strategy`
- **0.10.10**: `preference_data_gap_ack` retired — dropped on startup; Golden Source `ops_jobs.data_source_void` is authoritative

## 数据库规范

- 开发库：`bifrost_dev`，生产库：`bifrost_prod`
- 表命名前缀：`daemon_`、`account_`、`contract_`、`strategy_`、`gate_safety_`、`job_`、`preference_`
- FK 列名与被引用 PK 名完全一致
- `gate_safety_*` 表：标量列，不使用 jsonb
- DDL 变更必须在 `docs/DATABASE.md` 的 §6 变更日志中记录

## 测试标记

- `@pytest.mark.ib` — 需要 IB 实时连接
- `@pytest.mark.db` — 需要 PostgreSQL 连接
- 默认 CI 跑：`pytest -m 'not ib and not db'`
