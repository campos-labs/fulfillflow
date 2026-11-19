"""Obtain a local cookie jar through real OIDC. Password is prompted, never an argument."""

import argparse
import getpass
import json
from pathlib import Path

from fulfillflow.access.browser import browser_login
from fulfillflow.access.client import save_session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--username", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, help="Prepared synthetic accounts only")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error("Session material must remain outside the repository")
    try:
        password = (
            json.loads(args.credentials_file.read_text(encoding="utf-8"))["users"][args.username]
            if args.credentials_file
            else getpass.getpass()
        )
        result = browser_login(args.base_url.rstrip("/"), args.username, password)
        save_session(output, args.base_url, result)
    except Exception:
        raise SystemExit(
            "Login failed; check provider, account enablement and application URL"
        ) from None
    print(f"Session saved: {output}")


if __name__ == "__main__":
    main()
