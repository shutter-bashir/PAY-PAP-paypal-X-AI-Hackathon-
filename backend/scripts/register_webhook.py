"""Point the PayPal sandbox app's webhook at this API and save its id. Run from backend/:

    .venv\\Scripts\\python scripts\\register_webhook.py https://<public-api-host>
    .venv\\Scripts\\python scripts\\register_webhook.py --list

The URL must be public HTTPS: the Render API URL, or a tunnel to localhost:8000.
If a webhook for /webhooks/paypal already exists it is moved to the new URL (so a
changing tunnel URL does not pile up webhooks). The id is written to
PAYPAL_WEBHOOK_ID in backend/.env; restart the API afterwards.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.paypal_client import PayPalClient  # noqa: E402

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
PATH = "/webhooks/paypal"
PREFIXES = ("PAYMENT.PAYOUTSBATCH.", "PAYMENT.PAYOUTS-ITEM.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", nargs="?", help="public HTTPS base URL of the API")
    parser.add_argument("--list", action="store_true", help="show the app's webhooks and exit")
    parser.add_argument("--no-write-env", action="store_true")
    args = parser.parse_args()

    with PayPalClient.from_settings(settings) as paypal:
        hooks = paypal.list_webhooks()
        if args.list or not args.base_url:
            for h in hooks:
                print(h["id"], h["url"], ", ".join(e["name"] for e in h.get("event_types", [])))
            if not hooks:
                print("no webhooks registered")
            return 0

        base = args.base_url.rstrip("/")
        if not base.startswith("https://"):
            print("error: PayPal only delivers to public https:// URLs", file=sys.stderr)
            return 1
        url = base + PATH
        events = sorted(n for n in paypal.list_webhook_event_types() if n.startswith(PREFIXES))
        existing = next((h for h in hooks if h["url"].endswith(PATH)), None)
        if existing:
            hook = paypal.update_webhook(existing["id"], url, events)
            print(f"moved webhook {hook['id']} to {url}")
        else:
            hook = paypal.create_webhook(url, events)
            print(f"created webhook {hook['id']} for {url}")
        print(f"events: {', '.join(events)}")

    if not args.no_write_env:
        text = ENV_FILE.read_text(encoding="utf-8")
        line = f"PAYPAL_WEBHOOK_ID={hook['id']}"
        text, n = re.subn(r"(?m)^PAYPAL_WEBHOOK_ID=.*$", line, text)
        ENV_FILE.write_text(text if n else text.rstrip("\n") + "\n" + line + "\n", encoding="utf-8")
        print("saved PAYPAL_WEBHOOK_ID to backend/.env; restart the API to use it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
