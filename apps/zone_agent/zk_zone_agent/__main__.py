from __future__ import annotations

import argparse

import uvicorn

from zk_zone_agent.settings import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ZKTeco Zone Agent local UI/service.")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("zk_zone_agent.web:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
