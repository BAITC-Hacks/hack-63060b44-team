from __future__ import annotations

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="WindPulse AI — локальный просмотр и расчёт из погодного кэша")
    parser.add_argument("--host", choices=["127.0.0.1", "localhost", "::1"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(config_path=args.config), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
