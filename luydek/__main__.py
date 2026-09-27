"""python -m luydek [--host 0.0.0.0] [--port 8000] [--config configs/synthetic.yaml]"""

import argparse
import logging
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LuyDek dashboard, counter page and Telegram bot.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="0.0.0.0 to open the counter page from another device on the same Wi-Fi")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", help="config YAML (default: configs/config.yaml)")
    args = parser.parse_args()
    if args.config:
        os.environ["LUYDEK_CONFIG"] = os.path.abspath(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per Telegram poll otherwise
    uvicorn.run("luydek.web.server:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
