"""Entry point: trading daemon (GsTrading).

Usage:
    python scripts/run_engine.py [config/config.yaml]
"""
import sys
import asyncio


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/config.yaml"
    from bifrost_core.daemon.app.gs_trading import GsTrading
    engine = GsTrading(config_path=config_path)
    asyncio.run(engine.run())


if __name__ == "__main__":
    main()
