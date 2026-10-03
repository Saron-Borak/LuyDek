"""python -m luydek [--host 0.0.0.0] [--port 8000] [--config configs/synthetic.yaml] [--public-url https://...]"""

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
    parser.add_argument("--public-url", help="the https:// address phones use (e.g. from `tailscale serve`); "
                                             "the Setup page's QR code points there")
    args = parser.parse_args()
    if args.config:
        os.environ["LUYDEK_CONFIG"] = os.path.abspath(args.config)
    # The Setup page's "open on your phone" QR code needs to know who can reach this server.
    os.environ["LUYDEK_HOST"], os.environ["LUYDEK_PORT"] = args.host, str(args.port)
    if args.public_url:
        os.environ["LUYDEK_PUBLIC_URL"] = args.public_url.rstrip("/") + "/"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per Telegram poll otherwise
    uvicorn.run("luydek.web.server:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
