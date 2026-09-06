#!/usr/bin/env python3
"""Parse the Command Code GOAT plan docs page into an exact model-ID list.

Fetches two PUBLIC sources — no credentials involved:
  1. https://commandcode.ai/docs/plans/goat  (plan -> model page links)
  2. https://api.commandcode.ai/provider/v1/models (full upstream catalog)

Page slugs are resolved to upstream model IDs by normalization
(case/punctuation/vendor-prefix insensitive) plus a small public alias
table for names the provider API spells differently. Output is a JSON
file consumed by the server-side applier (see apply_goat_models.py).

Exit codes: 0 = success, 1 = network/parse error, 2 = validation failed
(list still written, CI should mark the run red).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DOCS_URL = "https://commandcode.ai/docs/plans/goat"
MODELS_URL = "https://api.commandcode.ai/provider/v1/models"
SLUG_RE = re.compile(r'href="/models/([a-z0-9][a-z0-9._-]*)"', re.IGNORECASE)
MIN_MODELS, MAX_MODELS = 20, 100
USER_AGENT = "axonhub-goat-sync/1.0 (+https://github.com/jasonqin/commandcode-goat-sync)"

# Slugs the provider API spells differently; everything else resolves by
# normalization alone. Keys/values are page slug -> upstream model ID.
ALIASES = {
    "tencent-hy3": "tencent/hy3-paid",
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def norm(text: str) -> str:
    """Lowercase and keep [a-z0-9] only, so '-'/'.'/_/case drift cannot break matching."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def candidate_forms(model_id: str) -> list[str]:
    """A model ID may be matched with or without its vendor prefix."""
    without_vendor = model_id.split("/")[-1] if "/" in model_id else model_id
    return [norm(model_id), norm(without_vendor)]


def resolve_slug(slug: str, upstream: list[dict]) -> tuple[str | None, str]:
    """Map a docs-page slug to one upstream model ID.

    Returns (model_id | None, how) where how explains the match strategy.
    """
    if slug in ALIASES:
        return ALIASES[slug], "alias"

    target = norm(slug)
    exact: list[str] = []
    prefix: list[str] = []
    for model in upstream:
        for cand in candidate_forms(model["id"]):
            if cand == target:
                exact.append(model["id"])
                break
            if cand.startswith(target) or target.startswith(cand):
                prefix.append(model["id"])
                break
    # Deduplicate while preserving order.
    exact, prefix = list(dict.fromkeys(exact)), list(dict.fromkeys(prefix))
    if len(exact) == 1:
        return exact[0], "exact"
    if not exact and len(prefix) == 1:
        return prefix[0], "prefix"
    return None, "ambiguous" if len(exact) > 1 or len(prefix) > 1 else "no-match"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="goat-models.json", help="output JSON path")
    parser.add_argument("--docs-url", default=DOCS_URL)
    parser.add_argument("--models-url", default=MODELS_URL)
    args = parser.parse_args()

    try:
        html = fetch(args.docs_url)
        upstream = json.loads(fetch(args.models_url))["data"]
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        print(f"ERROR: fetch failed: {exc}", file=sys.stderr)
        return 1

    slugs = list(dict.fromkeys(m.group(1).lower() for m in SLUG_RE.finditer(html)))
    if not slugs:
        print("ERROR: no model links found — docs page layout changed?", file=sys.stderr)
        return 1

    models: list[str] = []
    unresolved: list[dict] = []
    for slug in slugs:
        model_id, how = resolve_slug(slug, upstream)
        if model_id:
            models.append(model_id)
        else:
            unresolved.append({"slug": slug, "reason": how})

    warnings: list[str] = []
    if unresolved:
        warnings.append(f"{len(unresolved)} slug(s) could not be resolved to model IDs")
    if not (MIN_MODELS <= len(models) <= MAX_MODELS):
        warnings.append(f"model count {len(models)} outside expected range [{MIN_MODELS}, {MAX_MODELS}]")

    models.sort()
    payload = {
        "schema": 1,
        "plan": "individual-goat",
        "source": args.docs_url,
        "upstream_models_api": args.models_url,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "slug_count": len(slugs),
        "models": models,
        "unresolved": unresolved,
        "warnings": warnings,
    }
    Path(args.output).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"slugs: {len(slugs)}, resolved: {len(models)}, unresolved: {len(unresolved)}")
    for item in unresolved:
        print(f"  UNRESOLVED: {item['slug']} ({item['reason']})")
    for warning in warnings:
        print(f"  WARNING: {warning}")
    return 2 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())
