# Explicit official IFS replica

The AWS original-source diagnostic returned HTTP 503 before admitting any body,
field or frame. A separate bounded request preserved Amazon S3's `SlowDown`
response. A second official regional AWS endpoint also returned `SlowDown`.
These failures remain evidence; the HEAD 200 responses did not prove GET availability.

ECMWF documents its Google Cloud replica in its own
[client URL list](https://github.com/ecmwf/ecmwf-opendata/blob/main/ecmwf/opendata/urls.py)
and describes replicated sources on its
[open data page](https://www.ecmwf.int/en/forecasts/datasets/open-data).
Its [support response](https://forum.ecmwf.int/t/ecmwf-open-data-store-slow-since-the-50vr1-upgrade/14973)
explains AWS path throttling and recommends using the published replicas.

One explicit profile selects `storage.googleapis.com/ecmwf-open-data` for IFS.
The existing AWS profile remains the default. There is no arbitrary URL, bucket,
environment override, automatic failover or retry loop. An IFS profile cannot
silently alter a GFS-only generation. Both the held full producer and held
four-original diagnostic construct identical provider-relative paths from the
selected fixed profile. Explicit CLI selection and the diagnostic's `ifsSource`
receipt make the choice visible; the full producer's provenance remains its
existing commit/run/attempt record.

All original scientific and native admission, grid origin, clocks, byte ranges,
response checks, request/body/directory/time bounds, and publication boundaries
remain in force. The additional six offline cases check exact profiles/paths,
invalid values before reads or output, all four recorded Google requests,
the fifth-read hold, no fallback after refusal, and shared full-producer routing.
The pre-existing 120 source cases and seven native cases remain required.

A bounded PC GET on 17 October 2026 at 07:38:33 UTC received the original 40,580-byte
Google index for the 16 October 12 UTC IFS run, offset zero. Its SHA256
`edd83928ee2fe4c66e03ccf0b08505f209b64889bdf4fdacb9a396a48dd70273`
equals the earlier captured AWS index byte-for-byte. This proves only that one
index response. Actual Linux four-response/native proof, a complete cohort,
publication and activation are separate pending steps. No paid service has been
cancelled and no provider job or timer is activated by this source change.
