# MPA Pipeline — Australian Protected Areas to Trusted GeoJSON

This producer fetches the DCCEEW CAPAD marine polygon layer, reconciles every
authoritative object ID, and builds a bounded immutable schema-v2 GeoJSON bundle.
It does not publish or possess a GitHub write token; publication is a separate
isolated dual-slot publish job.

## Completeness and geometry contract

The producer first obtains the authoritative `GIS_AREA>0` count and exact ID
list, then requests deterministic ID-addressed pages. Missing, tiny, or
simplification-damaged polygons are refetched without simplification. Every ID
must reconcile exactly; null geometry, partial pages, duplicates, coordinates
outside the Australian-territory safety envelope, excessive source bytes, or
excessive coordinate counts fail the run.

`GIS_AREA` is hectares and is explicitly divided by 100 for `area_km2`. The
public GeoJSON is capped at 16 MiB, validated again across the artifact trust
boundary, and published under a generation-named immutable filename.

## Neutral display classification

CAPAD registry labels do not determine whether fishing, anchoring, entry, or any
other activity is lawful at a position. The artifact therefore contains only a
neutral, display-oriented `protection_class`:

- `high`
- `conditional`
- `multiple_use`

Each feature also carries `classification_source: indicative_heuristic`. These
classes control map styling only. They are not permissions, prohibitions, legal
advice, or a substitute for the responsible authority's current zoning rules.

## CI and publication

`.github/workflows/mpa-pipeline.yml` polls weekly. Generation installs the
Python 3.11.15 hash-locked requirements, has read-only repository permission,
and is the only job allowed to contact DCCEEW. The isolated publish job binds
the sealed producer commit and run ID to its workflow context. A publish-only
rerun may reuse an earlier attempt from that same run while the one-day artifact
is retained; after expiry, rerun all jobs. The job revalidates the entire bundle,
uploads the immutable GeoJSON to its UTC ISO-week release shard, verifies its
size and SHA-256, and updates only the inactive dual v2 discovery slot (seeding
both slots on bootstrap). Legacy `mpa.geojson` is not bridged because the old
display schema is incompatible; it returns `410`.

## Running the producer

```bash
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
export GITHUB_SHA=0123456789abcdef0123456789abcdef01234567
export GITHUB_RUN_ID=1
export GITHUB_RUN_ATTEMPT=1
python pipeline.py
```

Local output defaults to `/tmp/mpa-pipeline/bundle`. The producer deliberately
has no release-upload mode.
