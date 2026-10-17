# Ongoing atmosphere publication — prepared, held

This source is not an activated schedule. The new workflow's scheduled jobs
require the exact repository variable `THALASSA_ATMOS_SCHEDULE_ENABLED=true`.
An absent/other value holds them. Manual execution requires both explicit
download and publication gates, which default false. No variable, dispatch,
provider request or release mutation was performed while preparing this source.

The first actual publication remains independently recorded by proof
`b5b022a9b8baf962e4fe9d469ba1650fb650d511cba233d10aece81e92ebdbce`:
GFS run 39020313862 / IFS run 39020315339, source a16d599f39c68a53fa996c1b49dcc2a87aa62f97.
Those original cycles expire at 2026-10-17T12:00:00Z. This change does not extend
their clocks or claim uninterrupted service before a newer admitted cohort.

## Cycle admission

| Dataset | Original cycles | Conservative minimum lag | Scheduled UTC times | Fixed source |
| --- | --- | --- | --- | --- |
| GFS atmospheric scalars | 00/06/12/18 | 4 hours | 04:47, 10:47, 16:47, 22:47 | NOAA AWS |
| IFS pressure | 00/12 | 8 hours | 08:47, 20:47 | Official Google replica |

The full 240h IFS contract cannot use 06/18 runs. ECMWF's current open-data page
places the long leads in 00/12 and releases IFS open data after dissemination;
its full-product schedule finishes at 07:34/19:34. Eight hours is our conservative
policy, not a guarantee about cloud replication. NOAA documents four daily GFS
cycles. Four hours is our chosen minimum lag; a missing/late source fails the
single attempt. There is no claim that NOAA promises complete data at that time.

- [ECMWF open data](https://www.ecmwf.int/en/forecasts/datasets/open-data)
- [ECMWF dissemination schedule](https://www.ecmwf.int/en/forecasts/datasets/set-i)
- [NOAA GFS inventory](https://www.nco.ncep.noaa.gov/pmb/products/gfs/)

The actual authenticated GitHub run record supplies `created_at`, source, event,
sender, repository, branch, workflow path and first attempt. Cycle selection is
derived from that original clock and checked again against current UTC. Future,
over-90-minute queued, switched-eligible-cycle, foreign source/ref/sender, rerun
and override contexts refuse. Inputs cannot specify a cycle, origin or profile.
Already-published initial cycles are explicitly consumed. A failed source does
not select an older/newer run or another provider.

## Durable no-repeat boundary

Before a provider read, under the same existing `publisher-atmos-{dataset}`
concurrency group, authenticated bounded Actions metadata must show no prior
`atmos-attempt-{dataset}-{original-cycle}` artifact. A small zero-provider-read
marker is then uploaded with replacement forbidden and 90-day retention. The
generator requires the acknowledged artifact ID and independently checks its
unique name, digest, size, expiry, actual run, branch and source before invoking
the original reader. The publisher checks the same marker and sealed run again.

An upload that succeeds followed by a failure consumes that cycle, even if no
provider data were read. Failed, expired, ambiguous or unknown attempts are not
refunded. Do not delete attempt artifacts to force a retry. Administrative
deletion would invalidate the no-repeat evidence and require a new reviewed
control, rather than permission to repeat. The first one-shot workflows have
fixed expired clocks and cannot be repurposed as a dynamic retry path.

The scheduled rate is four GFS plus two IFS cohorts/day, one per original cycle.
Manual admission can consume a currently eligible cycle missed by the schedule;
its durable marker then blocks the corresponding scheduled duplicate. There
is no separate daily byte cap claimed: the unchanged per-cohort ceiling is
210 reads / 512 MiB / 1200 seconds / 20-second read timeout / 2 GiB native address
space. Each full GFS/IFS uses exactly 174/82 reads and 29/41 frames respectively.
An initial/manual catch-up can straddle UTC days; quota planning must not call
the cron count a universal rolling-day bound. Seven small authenticated control
metadata reads per successful cohort are separate from provider traffic, each
at most 64 KiB, with no retries or cross-origin redirects.

## Unchanged science and publication

The producer, original ranged reader, native decode, units, masks, run/validity,
manifest contracts, two dependency locks and isolated release writer are
byte-identical to a16d. The original bounded pass-through observer is reused.
The new wrapper preserves partial output and failure receipts. Only a complete
same-run draft is sealed. The unchanged writer revalidates actual producer/run,
all bodies/checksums, shard inventory (900 limit) and final-switch freshness,
then writes both discovery slots last. No provider/native dependency is added
to its job. No app flag or physical server install is part of this workflow.

Three existing control-only edits are explicit: the exact new workflow name is
added to the test writer allowlist, its byte pin is refreshed in the old one-shot
runner, and that runner's new hash is reflected in the old one-shot workflow.
No test assertion, threshold or sender/freshness requirement was removed.

## Review and rollout

1. Review the exact source and control hashes; keep the schedule variable held.
2. Prove original 126 source + 7 native cases, existing 23 publication controls
   and the new 36 operational cases in the two original isolated Linux closures.
   Local 36 cases pass; local tests are not a Linux/native acceptance claim.
3. Promote only the reviewed actual master successor. If needed, manually admit
   the eligible IFS 00Z cycle before the first old cohort expires. Do not repeat
   any cycle whose attempt state is present or uncertain.
4. Independently read back both public slots and every actual published frame,
   source/run, clocks, masks, hashes and freshness before enabling the schedule.
5. After explicit review, set only the schedule variable. Inspect the first real
   scheduled runs and their marker/publication proofs. Rollback holds the variable
   and retains the last qualified cohort until its original expiry. It does not
   manufacture a fresh clock, delete markers, retry a failure or change caps.

The preparation also retains two local test-harness failures: an initially
ambiguous `publish` text locator matched the input declaration, then a check
matched the writer hash command before its invocation. Both were repaired to
select the exact job/invocation; no production guard or deadline was weakened.
