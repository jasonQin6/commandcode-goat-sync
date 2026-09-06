#!/usr/bin/env python3
"""Apply the published GOAT plan model list to an AxonHub channel.

Fetches goat-models.json (from this repo's raw URL or a local file), diffs it
against the channel's live supportedModels, and writes through the admin
GraphQL API. Runs on the AxonHub server; all credentials come from the
environment — nothing secret is ever read from this repo.

Auth (either):
  AXONHUB_JWT                       existing token, used directly
  AXONHUB_EMAIL + AXONHUB_PASSWORD  exchanged via POST /admin/auth/signin

Other environment:
  AXONHUB_URL       default https://axon.jasonqin.site
  AXONHUB_CHANNEL_ID  default gid://axonhub/Channel/12
  AXONHUB_JSON_URL    default raw goat-models.json URL of this repo
  AXONHUB_STATE_DIR   default ~/.config/axonhub-sync

Exit codes: 0 ok/no-change, 2 fetch/parse error, 3 credentials missing,
4 auth failed, 5 refused (source JSON has warnings), 6 verify failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

JSON_URL = "https://raw.githubusercontent.com/jasonQin6/commandcode-goat-sync/main/goat-models.json"


def log(state_dir: Path, message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}"
    print(line)
    try:
        with (state_dir / "last-apply.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def http_json(url: str, payload: dict | None, token: str | None) -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": "axonhub-goat-apply/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def get_token(base_url: str) -> tuple[str | None, int, str | None]:
    jwt = os.environ.get("AXONHUB_JWT", "").strip()
    if jwt:
        return jwt, 0, None
    email, password = os.environ.get("AXONHUB_EMAIL", ""), os.environ.get("AXONHUB_PASSWORD", "")
    if not email or not password:
        return None, 3, "CREDENTIALS_MISSING: set AXONHUB_JWT or AXONHUB_EMAIL+AXONHUB_PASSWORD"
    try:
        result = http_json(f"{base_url}/admin/auth/signin", {"email": email, "password": password}, None)
    except Exception as exc:  # noqa: BLE001
        return None, 4, f"auth failed: {exc}"
    token = result.get("token") or (result.get("data") or {}).get("token")
    if not token:
        return None, 4, "auth failed: no token in signin response"
    return token, 0, None


def graphql(base_url: str, token: str, query: str, variables: dict) -> dict:
    result = http_json(f"{base_url}/admin/graphql", {"query": query, "variables": variables}, token)
    if result.get("errors"):
        raise RuntimeError(result["errors"])
    return result["data"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--json-file", help="use a local goat-models.json instead of the URL")
    args = parser.parse_args()

    base_url = os.environ.get("AXONHUB_URL", "https://axon.jasonqin.site").rstrip("/")
    channel_id = os.environ.get("AXONHUB_CHANNEL_ID", "gid://axonhub/Channel/12")
    json_url = os.environ.get("AXONHUB_JSON_URL", JSON_URL)
    state_dir = Path(os.environ.get("AXONHUB_STATE_DIR", Path.home() / ".config/axonhub-sync"))
    state_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.json_file:
            source = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        else:
            source = json.loads(urllib.request.urlopen(json_url, timeout=30).read().decode())
    except Exception as exc:  # noqa: BLE001
        log(state_dir, f"ERROR fetch/parse source: {exc}")
        return 2

    if source.get("unresolved") or source.get("warnings"):
        log(state_dir, f"REFUSED source has unresolved/warnings: {source.get('unresolved')} {source.get('warnings')}")
        return 5
    desired = sorted(source["models"])

    token, code, error = get_token(base_url)
    if error:
        log(state_dir, error)
        return code

    try:
        current = graphql(
            base_url,
            token,
            'query ($id: ID!) { node(id: $id) { ... on Channel { supportedModels autoSyncSupportedModels } } }',
            {"id": channel_id},
        )["node"]
    except Exception as exc:  # noqa: BLE001
        log(state_dir, f"ERROR reading channel: {exc}")
        return 4

    live = sorted(current.get("supportedModels") or [])
    removed = [m for m in live if m not in desired]
    added = [m for m in desired if m not in live]
    auto_sync = bool(current.get("autoSyncSupportedModels"))
    if not removed and not added and not auto_sync:
        log(state_dir, f"no change ({len(live)} models)")
        return 0

    log(state_dir, f"diff: +{len(added)} -{len(removed)} (live {len(live)} -> desired {len(desired)})")
    for m in removed:
        log(state_dir, f"  - {m}")
    for m in added:
        log(state_dir, f"  + {m}")
    if not args.apply:
        log(state_dir, "dry-run: not applied (pass --apply)")
        return 0

    try:
        written = graphql(
            base_url,
            token,
            'mutation ($id: ID!, $models: [String!], $auto: Boolean) { updateChannel(id: $id, '
            "input: { supportedModels: $models, autoSyncSupportedModels: $auto }) "
            "{ supportedModels autoSyncSupportedModels } }",
            {"id": channel_id, "models": desired, "auto": False},
        )["updateChannel"]
    except Exception as exc:  # noqa: BLE001
        log(state_dir, f"ERROR write failed: {exc}")
        return 4

    if sorted(written["supportedModels"]) != desired or written["autoSyncSupportedModels"]:
        log(state_dir, f"ERROR verify failed after write: {len(written['supportedModels'])} models")
        return 6

    (state_dir / "state.json").write_text(
        json.dumps({"applied_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "models": desired, "source_fetched_at": source.get("fetched_at_utc")}, indent=2),
        encoding="utf-8",
    )
    log(state_dir, f"applied and verified: {len(desired)} models, autoSync off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
