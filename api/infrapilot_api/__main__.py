"""infrapilot-api: run the Chat API locally.

  infrapilot-api                 # http://127.0.0.1:8000, docs at /docs
  infrapilot-api --port 9000 --reload
"""

from __future__ import annotations

import argparse


def main() -> None:
    p = argparse.ArgumentParser(prog="infrapilot-api", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    args = p.parse_args()

    import uvicorn

    uvicorn.run("infrapilot_api.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
