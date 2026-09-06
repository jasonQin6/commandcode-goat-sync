# commandcode-goat-sync

Keeps an AxonHub channel's `supportedModels` in sync with the models actually
available on the Command Code **GOAT plan**.

Command Code's provider API (`/provider/v1/models`) lists every model the
vendor offers (67+), but a subscription plan only serves a subset (GOAT = 48).
The plan restriction is enforced at request time with a 403
(`MODEL_NOT_IN_PLAN`), and the API has no plan filter — the only public source
of the plan → models mapping is the docs page. This repo turns that docs page
into a machine-readable list, daily, with no credentials anywhere.

```
docs page + public /models API          vol-server (private)
        │                                       │
        ▼                                       ▼
sync_goat_models.py  ──goat-models.json──►  apply_goat_models.py
(GitHub Action, daily,                     (systemd timer, daily)
 commit = change history)                   diff → updateChannel GraphQL
```

## Repo layout

| File | Runs where | Purpose |
|------|------------|---------|
| `sync_goat_models.py` | GitHub Actions (daily) | Fetch docs page + public `/models`, resolve slugs → model IDs, write `goat-models.json` |
| `goat-models.json` | committed artifact | The resolved list; `git log` on this file is the plan's change history |
| `.github/workflows/sync-goat-models.yml` | GitHub Actions | Daily schedule + commit step |

## JSON schema

```json
{
  "schema": 1,
  "plan": "individual-goat",
  "fetched_at_utc": "…",
  "slug_count": 48,
  "models": ["…exact upstream model IDs…"],
  "unresolved": [],
  "warnings": []
}
```

`models` is the authoritative list; the server-side applier refuses to apply
when `unresolved` or `warnings` are non-empty (a docs-page layout change must
be investigated, not blindly propagated).

## Server-side applier

`apply_goat_models.py` (deployed on the AxonHub server, **not** part of this
repo) fetches this repo's raw `goat-models.json`, diffs it against the
channel's live `supportedModels`, and writes through the admin GraphQL API.
Credentials live only on the server.

## Local usage

```bash
python3 sync_goat_models.py --output goat-models.json
```

Exit codes: `0` clean, `1` fetch/parse error, `2` validation warning (output
still written).
