"""Generate synthetic Keycloak identities outside Git, without enabling Core access.

PowerShell: uv run python scripts/prepare_identity.py --output $env:TEMP/fulfillflow-identity
Set IDENTITY_REALM_FILE and KEYCLOAK_DB_PASSWORD from the generated files.
Existing files are preserved; rerunning prints only their paths.
"""

import argparse
import json
import secrets
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from fulfillflow.access.client import write_private_json

USERS = {"operator": "operator", "viewer": "viewer", "disabled": "viewer"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    repository = Path(__file__).resolve().parents[1]
    if root.is_relative_to(repository):
        parser.error("Identity material must remain outside the repository")
    secrets_file = root / "credentials.json"
    if not secrets_file.exists():
        write_private_json(
            secrets_file,
            {
                "database_password": secrets.token_urlsafe(32),
                "users": {name: secrets.token_urlsafe(24) for name in USERS},
            },
        )
    credentials = json.loads(secrets_file.read_text(encoding="utf-8"))
    realm_file = root / "realm.json"
    if not realm_file.exists():
        realm = {
            "realm": "fulfillflow",
            "enabled": True,
            "sslRequired": "none",
            "registrationAllowed": False,
            "clients": [
                {
                    "clientId": "fulfillflow",
                    "publicClient": True,
                    "standardFlowEnabled": True,
                    "directAccessGrantsEnabled": False,
                    "redirectUris": [
                        "http://localhost:8000/auth/callback",
                        "http://localhost:19800/auth/callback",
                    ],
                    "attributes": {"pkce.code.challenge.method": "S256"},
                }
            ],
            "users": [
                {
                    "id": str(uuid5(NAMESPACE_URL, "fulfillflow:v2:" + name)),
                    "username": name,
                    "enabled": True,
                    "firstName": "Synthetic",
                    "lastName": name,
                    "email": name + "@example.test",
                    "emailVerified": True,
                    "credentials": [{"type": "password", "value": password, "temporary": False}],
                }
                for name, password in credentials["users"].items()
            ],
        }
        write_private_json(realm_file, realm)
    print(f"Realm: {realm_file}")
    print(f"Local credentials: {secrets_file}")


if __name__ == "__main__":
    main()
