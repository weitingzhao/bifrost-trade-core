#!/usr/bin/env python3
"""One-shot engine → bifrost_core file copy with import rewrites (migration helper)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[2] / "bifrost-trader-engine"
CORE_SRC = Path(__file__).resolve().parents[1] / "src" / "bifrost_core"

# (engine_relative_path, core_relative_path under bifrost_core)
COPY_MAP: list[tuple[str, str]] = [
    # config
    ("src/config/settings.py", "config/settings.py"),
    ("src/config/yaml_config.py", "config/yaml_config.py"),
    ("src/ib/connection_policy.py", "config/connection_policy.py"),
    # bifrost shared keys
    ("src/bifrost/redis_health_keys.py", "core/redis_health_keys.py"),
    # core utils
    ("src/core/dict_merge.py", "core/dict_merge.py"),
    ("src/core/redis_url.py", "core/redis_url.py"),
    ("src/core/logging_redis_stream.py", "core/logging_redis_stream.py"),
    ("src/core/sse/queue_utils.py", "sse/queue_utils.py"),
    ("src/core/realtime/redis_keys.py", "core/realtime/redis_keys.py"),
    ("src/core/realtime/redis_quotes.py", "core/realtime/redis_quotes.py"),
    ("src/core/realtime/redis_subscribe.py", "core/realtime/redis_subscribe.py"),
    ("src/vendor/ib_ingestor/redis_keys.py", "core/realtime/ib_ingestor_keys.py"),
    # persistence
    ("src/persistence/status_sink.py", "persistence/status_sink.py"),
    ("src/persistence/postgres/connection.py", "persistence/postgres/connection.py"),
    ("src/persistence/postgres/ddl.py", "persistence/postgres/ddl.py"),
    ("src/persistence/postgres/postgres_sink.py", "persistence/postgres/postgres_sink.py"),
    ("src/persistence/postgres/accounts_sync.py", "persistence/postgres/accounts_sync.py"),
    ("src/persistence/postgres/ticker_reference.py", "persistence/postgres/ticker_reference.py"),
    ("src/persistence/postgres/stock_ohlc_massive.py", "persistence/postgres/stock_ohlc_massive.py"),
    # ib_operator client (core)
    ("src/ib_operator/client.py", "ib_operator/client.py"),
    ("src/ib_operator/protocol.py", "ib_operator/protocol.py"),
    ("src/ib_operator/config.py", "ib_operator/config.py"),
    ("src/monitor/integrations/ib_probe_derived.py", "ib_operator/ib_probe_derived.py"),
]

# Recursive directory copies (engine dir -> core dir)
DIR_MAP: list[tuple[str, str]] = [
    ("src/portfolio", "portfolio"),
    ("src/monitor", "monitor"),
]

IMPORT_REPLACEMENTS: list[tuple[str, str]] = [
    (r"\bfrom src\.config\b", "from bifrost_core.config"),
    (r"\bimport src\.config\b", "import bifrost_core.config"),
    (r"\bfrom src\.core\b", "from bifrost_core.core"),
    (r"\bimport src\.core\b", "import bifrost_core.core"),
    (r"\bfrom src\.persistence\b", "from bifrost_core.persistence"),
    (r"\bfrom src\.portfolio\b", "from bifrost_core.portfolio"),
    (r"\bfrom src\.monitor\b", "from bifrost_core.monitor"),
    (r"\bfrom src\.ib_operator\b", "from bifrost_core.ib_operator"),
    (r"\bfrom src\.ib\.connection_policy\b", "from bifrost_core.config.connection_policy"),
    (r"\bfrom src\.ib\b", "from bifrost_core.config"),  # fallback
    (r"\bfrom src\.bifrost\.redis_health_keys\b", "from bifrost_core.core.redis_health_keys"),
    (r"\bfrom src\.bifrost\b", "from bifrost_core.core"),
    (r"\bfrom src\.vendor\.ib_ingestor\.redis_keys\b", "from bifrost_core.core.realtime.ib_ingestor_keys"),
    (r"\bfrom src\.vendor\.ib_ingestor\b", "from bifrost_core.core.realtime.ib_ingestor_keys"),
    (r"\bfrom src\.monitor\.integrations\.ib_probe_derived\b", "from bifrost_core.ib_operator.ib_probe_derived"),
    (r"\bfrom src\.connector\b", "from bifrost_socket.ib.connector"),  # optional; monitor ib_clients only
    (r"\bfrom src\.app\.config\b", "from bifrost_core.config.startup"),
]


def rewrite(content: str) -> str:
    for pattern, repl in IMPORT_REPLACEMENTS:
        content = re.sub(pattern, repl, content)
    # Fix settings example path: parent.parent.parent / "config" -> repo config
    content = content.replace(
        'Path(__file__).resolve().parent.parent.parent / "config"',
        'Path(__file__).resolve().parents[3] / "config"',
    )
    return content


def copy_file(rel_engine: str, rel_core: str) -> None:
    src = ENGINE_ROOT / rel_engine
    dst = CORE_SRC / rel_core
    if not src.is_file():
        print(f"SKIP missing {src}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8")
    dst.write_text(rewrite(text), encoding="utf-8")
    print(f"OK {rel_core}")


def copy_tree(rel_engine: str, rel_core: str) -> None:
    src_root = ENGINE_ROOT / rel_engine
    if not src_root.is_dir():
        print(f"SKIP missing dir {src_root}")
        return
    for path in src_root.rglob("*.py"):
        rel = path.relative_to(src_root)
        dst = CORE_SRC / rel_core / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8")
        # Skip ib_clients (socket-only IB connector)
        if rel.name == "ib_clients.py":
            print(f"SKIP {rel_core}/{rel}")
            continue
        dst.write_text(rewrite(text), encoding="utf-8")
    print(f"OK tree {rel_core}/")


def main() -> None:
    if not ENGINE_ROOT.is_dir():
        raise SystemExit(f"Engine not found: {ENGINE_ROOT}")
    for eng, core in COPY_MAP:
        copy_file(eng, core)
    for eng, core in DIR_MAP:
        copy_tree(eng, core)
    # Fix ib_ingestor_keys to use core redis_health_keys
    keys_path = CORE_SRC / "core/realtime/ib_ingestor_keys.py"
    if keys_path.is_file():
        t = keys_path.read_text(encoding="utf-8")
        t = t.replace(
            "from bifrost_core.core.redis_health_keys import BIFROST_HEALTH_IB_INGESTOR",
            "from bifrost_core.core.redis_health_keys import BIFROST_HEALTH_IB_INGESTOR",
        )
        keys_path.write_text(t, encoding="utf-8")
    print("Done.")


if __name__ == "__main__":
    main()
