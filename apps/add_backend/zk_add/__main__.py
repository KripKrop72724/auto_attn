from __future__ import annotations

import argparse

import uvicorn

from zk_add.settings import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Attendance Device Dashboard API.")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("zk_add.web:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
