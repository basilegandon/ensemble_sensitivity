# PEARP data landscape

Exploration snapshot: 3–4 October 2026. This records probe observations, not yet a
production ingestion contract. Credentials are read from the ignored root
`.env`; never add them to this file, logs, or version control.

## Météo-France WCS API

The local Swagger is `docs/Modèle_ARPEGE_Prévision_d'Ensemble_swagger.json`.
It defines the base URL `https://public-api.meteofrance.fr/public/pearpege/1.0`,
global GLOB025 member routes `MF-NWP-GLOBAL-PEARP{member}-025-GLOBE-WCS`,
EUROPE 0.1-degree routes, and WCS `GetCapabilities`, `DescribeCoverage`, and
`GetCoverage` operations. The path parameter called `run` enumerates `000` to
`034`; the Swagger describes these as ensemble members, so the probe calls the
parameter `member`. The API key is passed in the `apikey` header.

An authenticated `GetCapabilities` request to member endpoint `000` returned
HTTP 200 and XML. Parsing the complete response found 1,872 coverage IDs under
51 coverage titles. Coverage initialization times ranged from
`2026-09-29T00.00.00Z` to `2026-10-03T06.00.00Z` at six-hour intervals
(18 initializations). The Z500-like coverage title is
“Geopotential height of isobaric surfaces”; its coverage ID for the latest
initialization observed was:

```text
GEOPOTENTIAL__ISOBARIC_SURFACE___2026-10-03T06.00.00Z
```

`DescribeCoverage` for that coverage returned HTTP 200 with:

- axes `long lat pressure time`, units `deg deg hPa ISO8601`;
- full-globe bounds: longitude 0–359.75, latitude −90–90;
- pressure coefficients `50, 200, 250, 300, 400, 500, 700, 800, 850, 925,
  1000` hPa;
- forecast-time coefficients 0, 10,800, ..., 367,200 seconds: 35 steps from
  0 to 102 h at 3-hour intervals;
- initialization 2026-10-03 06Z and valid-time end 2026-10-07 12Z.

The same response reports a `GridEnvelope` high coordinate of
`1440 721 6 35`, although the pressure axis lists 11 coefficients. A selection
at 1000 hPa succeeds despite this discrepancy, but it does not validate every
listed pressure or resolve the inconsistent metadata.

A bounded `GetCoverage` request for member 000, this coverage, pressure 500 hPa,
time 86,400 seconds (+24 h), longitude 0–1°, and latitude 45–46° returned HTTP
200 and a 232-byte GRIB message. ecCodes decoded it as Z500 (`paramId=129`),
member 0, step 24 h, on a 5×5 grid with 0.25° spacing and the requested bounds.
The corresponding data.gouv message is a global 689,516-byte field. Its values
for the same 25 cells match the WCS subset to within 0.123333 m² s⁻² (mean
absolute difference 0.072347, RMSE 0.083508); all differences are within half
of a 0.25 m² s⁻² quantization step. The API response uses GRIB simple packing,
whereas the open GRIB uses second-order packing. This is evidence of equivalent
fields at differing precision, not bit-identical data.

A separate request selecting pressure 1000 hPa and +24 h without spatial
subsetting returned HTTP 200 and one global GRIB message: `paramId=129`,
`typeOfLevel=isobaricInhPa`, `level=1000`, `step=24`, member 0, and 1440 × 721
points. The response was 2,076,662 bytes. This confirms pressure selection at
500 and 1000 hPa despite the envelope discrepancy. A request combining 1000
hPa with the bbox used for the successful 500-hPa subset instead returned HTTP
400, saying `Lat and long parameters are not allowed` for the global route.
Since the 500-hPa bbox did succeed, spatial subset support is confirmed for
that exact request only; compatibility across axis combinations remains
unresolved.

A full-grid WCS download was attempted for the same member, coverage, pressure,
and lead on 3 October, but the endpoint returned HTTP 429 with
`nextAccessTime=2026-10-03 13:52 UTC`. This earlier throttle was not repeated in
the complete 70-request pilot on 4 October; it remains evidence that limits can
vary with the service/account, not a measured permanent quota.

### WCS decision-gate pilot, 4 October 2026

The current `GetCapabilities` response advertised the latest Z500 initialization
as `2026-10-04T00.00.00Z`. For the README's initial analysis interval of 24 h,
the pilot target was t₁=+24 h and the only preceding predictor lead was +0 h.
An attempted multi-time subset `time(0,86400)` returned HTTP 404
`InvalidSubsetting`; the service therefore required one `GetCoverage` per
member and lead in this test.

`docs/pilot_pearp_wcs.py` then queried the 35 API member endpoints sequentially
for each of +24 h and +0 h. All 70 requests returned HTTP 200; each response
decoded to exactly one full-globe Z500 message and passed checks for
initialization, lead, member ID, parameter 129, isobaric level 500, ensemble
size 35, regular lat/lon 1440 × 721 grid, and units `m**2 s**-2`. Member
`number` and `perturbationNumber` matched every route ID from 000 to 034 at
both leads. No throttling or network retries occurred.

| Metric | Observed |
| --- | ---: |
| Requests | 70 / 70 succeeded |
| Payload per field | 2,076,662 bytes |
| Total WCS payload | 145,366,340 bytes (138.63 MiB) |
| Wall time | 31.022 s |
| Mean per-request elapsed time | 0.441 s |
| HTTP 429 / retries | 0 / 0 |

A same-run +24 h regional query for member 000 returned a 232-byte, 5 × 5
message. Its 25 decoded values exactly matched the corresponding cells from
that member's 2,076,662-byte global response. The global payload is 8,951 times
the regional payload for this sample. This verifies both global transfer and
the crop against the same run, lead, member, pressure, grid, and encoding.

**Decision for v1:** use the WCS API as the primary Z500 source for manually
launched retrievals. The latest advertised initialization passed the full
35-member check for the required +24 h and +0 h fields in 31 seconds, with no
throttling. The manual-run selector must validate every requested lead/member
pair and choose the newest candidate that is complete; a coverage ID in
`GetCapabilities` is not sufficient proof of a complete run. Keep data.gouv
GRIB as an independent verification path and possible fallback, but its
comparable latest-run index and retrieval cost has not been measured. Since
WCS returns one field per request, request volume scales with 35 × requested
lead count; revisit the source choice if the default lead set or bandwidth
budget grows.

### HTTP 429 and quota policy (investigated 4 October 2026)

Météo-France's official [quota FAQ](https://confluence-meteofrance.atlassian.net/wiki/spaces/OpenDataMeteoFrance/pages/457803410)
says request quotas exist **per API and per portal login account**. Exceeding a
quota produces HTTP `429 Too Many Requests` and temporarily disables access;
clients should stop requesting and resume after a sufficient pause. The
associated [reasonable-use guidance](https://confluence-meteofrance.atlassian.net/wiki/spaces/OpenDataMeteoFrance/pages/457803452)
recommends sequential rather than parallel requests, a delay between successive
requests, delayed retries after failures, and caching instead of downloading
the same resource repeatedly. It also warns that excessive global concurrency
can cause dropped connections and bandwidth saturation can lead to IP blocking.
That guidance explicitly says numerical indicators are pending definition by
Météo-France's IT department.

Consequently, the published material confirms that a quota exists, but does
**not** specify its numerical threshold or whether it is daily, rolling-window,
burst-based, or another policy. The local PEARP Swagger describes `429` as
`ThrottlingLimit` while each operation also declares
`x-throttling-tier: Unlimited`; that tier label is not a published numeric
quota and does not override the FAQ's explicit quota/429 behavior. Neither
document identifies whether the 429 in the supplied trace was caused by a
quota counter, a short-lived service limit, or earlier calls from another
process using the same API/login account.

The supplied traceback fails in `GetCapabilities`, before any coverage is
selected or any member/lead `GetCoverage` fields are requested. Thus the
`--target-lead-hours 96 --step-hours 24` batch itself had not yet made its
planned 175 field requests; earlier API usage by this or another process
remains possible. The stack trace contains only the status, not the response
body or headers, so it does not tell us whether this particular response
included a server retry time. A separate 3 October `GetCoverage` 429 did
include `nextAccessTime=2026-10-03 13:52 UTC`; that is evidence of a
server-directed temporary retry time on that request, not evidence of a daily
or rolling quota.

The reported PEARP limit of **400 requests per minute** (communicated for this
API; not specified in the official pages consulted above) is applied to both
`GetCapabilities` and `GetCoverage`, including retries. A process-wide sliding
60-second window limits this client to **390 requests per minute**, with at
least `60 / 389` seconds between request starts. This smooths traffic and
reserves ten requests per minute for occasional calls made by other processes
using the same account, instead of using the entire reported allowance. It
replaces the former fixed five-second delay. The reserve reduces risk but
cannot guarantee compliance if other clients using the account consume more
than ten requests per minute; all such clients need coordinated pacing for a
strict account-wide guarantee.

On HTTP 429, the client still retries at most five times only when a valid
`nextAccessTime` is supplied, and otherwise fails with a contextual diagnostic.
The server-directed wait is honored in addition to the rate limiter. For
recurring 429s, avoid repeatedly relaunching the fetch; check the API portal's
account/API usage information and ask Météo-France support to confirm the
applicable quota/window, providing the request time and endpoint but never the
API token.

Re-run the pilot (requires the optional ecCodes binding; no project runtime
dependency was added by the original probe):

```powershell
uv run --no-project --with eccodes python -m docs.pilot_pearp_wcs --target-lead-hours 24 --step-hours 24
```

The pilot downloads each response into memory only, validates it, then discards
the GRIB payload. Its JSON measurement report is written to the system temp
directory by default. The package pipeline below persists the validated fields
and is the user-facing retrieval implementation.

### User-facing retrieval and visualization

Install the project dependencies, then fetch the newest complete run and
render any one validated member/lead separately:

```powershell
uv sync --locked
uv run ensemble-sensitivity fetch --target-lead-hours 24 --step-hours 24 --output-dir .\data\pearp
uv run ensemble-sensitivity plot --run-dir .\data\pearp\run_YYYYMMDDHH_t24_s24 --lead-hours "*" --members "*"
```

The API token is read from `PEARP_METEO_FRANCE_API_TOKEN` or `.env`; it is sent
only in the request header. Retrieval writes one GRIB file per required
member/lead and a `manifest.json` in each candidate run directory. Only a
manifest with `status: complete` can feed the plot command. The plot command
reads only its selected field and writes a PNG plus a provenance JSON sidecar.
These quicklooks show raw geopotential fields; they are not sensitivity maps.
Replace `run_YYYYMMDDHH_t24_s24` with the complete run directory reported by
the fetch command. Each command displays a progress bar: fetch counts
lead/member fields for a candidate run; plot counts each output map. Progress
labels include the active variable (Z500), lead, and member. Use comma-separated
IDs for subsets, for example `--lead-hours 24,48 --members 0,7,34`; `*`
selects all retrieved leads or all 35 members. The quicklook maps use the
validated geographic coordinates, center the antimeridian, and apply Cartopy's
Plate Carrée projection with coastlines, borders, land/ocean shading, and a
graticule. Cartopy may download Natural Earth 1:110m outlines to its local cache
the first time these maps are rendered.

Reusable probe:

```powershell
python docs\probe_pe_arpege_api.py
python docs\probe_pe_arpege_api.py --title-filter "Geopotential height"
python docs\probe_pe_arpege_api.py --coverage-id "GEOPOTENTIAL__ISOBARIC_SURFACE___2026-10-03T06.00.00Z"
python docs\probe_pe_arpege_api.py --member 034
python docs\probe_pe_arpege_api.py --get-coverage --coverage-id "GEOPOTENTIAL__ISOBARIC_SURFACE___2026-10-03T06.00.00Z" --subset "pressure(500)" --subset "time(86400)" --subset "long(0,1)" --subset "lat(45,46)" --output "$env:TEMP\pearp-api-z500-sample.grib"
```

## data.gouv.fr GRIB2 resource set

On 3 October, the dataset API returned 103 resources for run `202610030600`,
named from `00:00` through `102:00` at one-hour intervals. The live catalog had
rotated to newer resources by 4 October and no longer listed that run, but the
three historical object URLs probed below remained reachable. The resources
are multi-gigabyte GRIB2 objects served by object storage. A request with
`Range: bytes=0-63` returned HTTP 206 and exactly 64 bytes. The files are
therefore range-readable; this alone does not provide variable or geographic
selection. Several likely `.idx` sidecar URL conventions returned 404 for
probed resources; this does not establish that no index is supplied anywhere
in the dataset.

The downloaded GRIB2 messages decode with ecCodes as follows:

| Forecast lead | Resource size | Z500 message offset | Message size | Member | GRIB `step` |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0 h | 2,862,685,455 bytes | 36,404,734 | 700,368 bytes | 0 | 0 |
| 24 h | 4,009,661,534 bytes | 59,940,203 | 689,516 bytes | 0 | 24 |
| 102 h | 3,861,451,479 bytes | 57,482,745 | 677,223 bytes | 0 | 102 |

In each sample, Z500 is GRIB2 `paramId=129`, `shortName=z`,
`typeOfLevel=isobaricInhPa`, level 500, units `m**2 s**-2`, and the message's
run is 3 October 2026 06 UTC. This is geopotential, not geopotential height in
metres. If a height in metres is required, the conversion and chosen gravity
constant must be made explicit in the implementation contract; the conversion
has not been applied here.

The decoded 0-hour field has `gridType=regular_ll`, `Ni=1440`, `Nj=721`
(1,038,240 values), 0.25-degree increments, longitude 0 to 359.75 degrees,
and latitude scanning from 90 to -90 degrees. All 1,038,240 values were finite;
the sample range was 46,101.400 to 58,490.011 m² s⁻². This supports the global
grid and orientation claims for this message, but does not replace a visual
meteorological plausibility check.

The message metadata reports `numberOfForecastsInEnsemble=35`. The sampled
500-hPa fields for members 0 and 1 have `number` and `perturbationNumber` equal
to 0 and 1 respectively. This verifies those two member IDs and the declared
ensemble size, not the presence of every member's Z500 message in every file.

To resolve hourly lead and member coverage, the complete `00:00`, `01:00`, and
`02:00` resources for this run were scanned as validated HTTP `Range` requests
in 32-MiB chunks; the scan streamed through memory and did not save the files.
The server returned the exact requested `Content-Range` for every chunk. The
scan covered 7,471,944,527 bytes in 224 ranges and decoded 8,925 complete GRIB
messages:

| Resource lead | File bytes scanned | Complete messages | Z500 500-hPa messages | Z500 member IDs / result |
| ---: | ---: | ---: | ---: | --- |
| 0 h | 2,862,685,455 | 3,675 | 35 | Exactly once each: 0–34 |
| 1 h | 2,314,432,767 | 2,625 | 0 | No Z500 field at this lead |
| 2 h | 2,294,826,305 | 2,625 | 0 | No Z500 field at this lead |

For the +0 h resource, every Z500 message has `step=0`,
`numberOfForecastsInEnsemble=35`, and `number=perturbationNumber`, covering all
IDs 0 through 34 once each with no duplicates. The run metadata is 3 October
2026 06 UTC and units are `m**2 s**-2`. For +1 h and +2 h, the full files were
examined and no message matched `paramId=129`, `typeOfLevel=isobaricInhPa`,
`level=500`; the missing-member list is therefore not applicable, because the
Z500 field itself is absent, not an incomplete 35-member Z500 ensemble.

This establishes Z500 availability at the sampled +0 h lead and its absence at
+1 h and +2 h for this run, but does not prove a complete cadence rule for all
103 resources. The API `DescribeCoverage` advertises Z500 every 3 hours through
+102 h; confirming matching GRIB availability at every three-hour lead and
inventoring other variables remain open.

Finding the 0-hour member-0 Z500 message by scanning sequential GRIB headers
required reading byte ranges through roughly 64 MiB to reach offset 36.4 MiB;
the target message itself is about 0.7 MiB. Once offsets are known, the message
can be fetched directly. The first-message length is encoded in the GRIB2
section-0 header, but a scalable remote indexer and its total header-transfer
cost still need to be designed and measured.

## Implementation-relevant conclusions

- Confirmed for this dataset/run: global Z500 is present; its native GRIB
  parameter and units are `129` and m² s⁻²; sample leads 0, 24, and 102 h are
  available; the catalog lists hourly lead resources through 102 h; the sample
  grid is 1440 × 721 at 0.25 degrees. Complete scans found no Z500 500-hPa
  field in the +1 h and +2 h resources; complete Z500 lead cadence across the
  full run remains unknown.
- Confirmed for the +0 h Z500 resource: exactly one field for each member ID
  0–34, where `number` and `perturbationNumber` agree; ensemble declaration
  is 35. This verifies completeness for that resource only.
- Confirmed for the API: authenticated global GLOB025 capabilities are
  accessible; the API lists a geopotential-at-isobaric-surfaces coverage,
  18 six-hourly initialization times in the observed window, 11 listed pressure
  levels, and 3-hourly Z500 leads through +102 h. Selections at 500 and
  1000 hPa work. A 5×5 spatial subset is verified at 500 hPa; combining the
  same bbox with 1000 hPa returned HTTP 400. The reported pressure dimension
  count still conflicts with its level coefficients. A same-run regional patch
  matches the global field exactly. The latest advertised 00Z initialization
  passed a 70-request full-member pilot at +0 and +24 h with no retries.
- Still open: complete variable/member inventory per GRIB resource, Z500
  availability at other leads (including +3 h and remaining advertised
  3-hourly leads), sidecar indexes for the full catalog, full WCS member/lead
  validation beyond +0/+24 h, comparable latest-run direct-GRIB index cost,
  EUROPE 0.1 coverage and access, and run-completion delays.
- Decision for v1: choose WCS as the primary Z500 source for manually launched
  retrievals whose required fields pass the run/member/lead/grid/unit checks.
  Comparing the latest-run direct-GRIB index cost remains open; re-evaluate if
  that route can meet the same completeness contract with materially lower
  transfer cost or better availability.

The working probe script uses only the Python standard library. ecCodes was
used in an isolated `uv run --no-project --with eccodes` environment to inspect
sample GRIB messages; it has not been added to the project's dependencies.
