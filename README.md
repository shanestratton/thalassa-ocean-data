# Thalassa ocean data

The ocean layers the [Thalassa](https://www.thalassawx.app) app shows (currents, waves, sea temperature,
chlorophyll, sea ice, mixed-layer depth and Australian marine protected areas) are built and published
from here.

Seven scheduled workflows each download one public dataset, check it thoroughly, and publish it as GitHub
Release assets. The app reads those assets through its own server, which checks every file again before
any phone sees it. This repository holds only that pipeline code and its tests. The app itself lives in a
private repository.

## Data sources and credits

- **E.U. Copernicus Marine Service Information.** Currents, waves, sea temperature, chlorophyll, sea ice
  and mixed-layer depth come from these Copernicus Marine products:
    - `cmems_mod_glo_phy_anfc_merged-uv_PT1H-i` (currents)
    - `cmems_mod_glo_wav_anfc_0.083deg_PT3H-i` (waves)
    - `cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m` (sea temperature)
    - `cmems_mod_glo_bgc-pft_anfc_0.25deg_P1D-m` (chlorophyll)
    - `cmems_mod_glo_phy_anfc_0.083deg_P1D-m` (sea ice, mixed-layer depth)

    Their use is governed by the Copernicus Marine Service licence.

- **Marine protected areas:** CAPAD data © Commonwealth of Australia (DCCEEW), licensed CC BY 4.0. It is
  simplified and display-classified by Thalassa. The colours are indicative only and never say whether an
  activity is legally allowed at a position.

## How publishing works

| Workflow                      | Schedule (UTC)                      | Release tag             |
| ----------------------------- | ----------------------------------- | ----------------------- |
| `cmems-currents-pipeline.yml` | every 3 hours (two staggered crons) | `cmems-currents-latest` |
| `cmems-waves-pipeline.yml`    | 07:00 and 19:00                     | `cmems-waves-latest`    |
| `cmems-sst-pipeline.yml`      | 03:17 and 15:17                     | `cmems-sst-latest`      |
| `cmems-chl-pipeline.yml`      | 04:47 and 16:47                     | `cmems-chl-latest`      |
| `cmems-seaice-pipeline.yml`   | 16:30                               | `cmems-seaice-latest`   |
| `cmems-mld-pipeline.yml`      | 17:00                               | `cmems-mld-latest`      |
| `mpa-pipeline.yml`            | Mondays 17:00                       | `mpa-aus-latest`        |

- A **generate** job has the Copernicus login and read-only access. It runs the offline tests first, behind
  a dead network proxy, then builds and validates a bundle. It cannot publish.
- A separate **publish** job has no Copernicus login and installs nothing. It uploads the immutable files
  into a weekly release (`<tag>-assets-YYYY-Www`), downloads them back to verify them, and only then
  updates the inactive one of the two discovery manifests (`manifest-v2-a.json` / `manifest-v2-b.json`).
  A failed run leaves the last good manifest in place.
- `ci.yml` runs the same offline tests on every push. `keepalive.yml` makes one small commit a month,
  because GitHub switches off schedules in a public repository after 60 days without activity.

## One-time setup

1. **Settings → Secrets and variables → Actions → New repository secret:** add `COPERNICUS_MARINE_USERNAME`
   and `COPERNICUS_MARINE_PASSWORD`, your Copernicus Marine login. If you don't have the password to hand,
   reset it at [marine.copernicus.eu](https://marine.copernicus.eu); the old repository's copy cannot be
   read back.
2. Leave **immutable releases** switched off (**Settings → General → Releases**): the discovery manifests
   are replaced on every run.
3. **Actions →** run each of the seven pipelines once (**Run workflow**, branch `master`), so every dataset
   has both discovery manifests before the app is switched over. The protected-areas pipeline otherwise
   only runs on Mondays.

## Checking it

```sh
curl -sSI https://www.thalassawx.app/api/currents/manifest-v2.json | grep -i -E '^(HTTP|x-thalassa)'
```

A healthy answer is `HTTP/2 200` with an `x-thalassa-generation` from the last few hours.

---

Copyright © 2026 Shane Stratton. All rights reserved. The code is public so the data can be downloaded
and checked, but it is not licensed for reuse.
