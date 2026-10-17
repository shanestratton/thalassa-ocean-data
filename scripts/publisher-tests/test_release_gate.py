"""Release-gate checks that moved here with the publishers.

Until 2026-10 these lived in the app repository's scripts/check-beta-readiness.mjs,
which read this code directly. The publishers now run here, so the checks that only
look at publisher code run here too, inside every pipeline's offline fixture step
and on every push (.github/workflows/ci.yml). The app keeps the checks on its own
side of the contract (proxy, clients, Vercel routing).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github/workflows"
DATASETS = ("currents", "waves", "sst", "chl", "seaice", "mld", "mpa")
PIPELINE_WORKFLOW = re.compile(r"^(?:cmems-(?:currents|waves|sst|chl|seaice|mld)|mpa)-pipeline\.yml$")
# Every action this repository may run, pinned to a reviewed commit.
REVIEWED_ACTION_PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
}


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text()


def workflow(dataset: str) -> str:
    name = "mpa-pipeline.yml" if dataset == "mpa" else f"cmems-{dataset}-pipeline.yml"
    return (WORKFLOWS_DIR / name).read_text()


def includes_all(source: str, values: list[str]) -> bool:
    return all(value in source for value in values)


def includes_in_order(source: str, values: list[str]) -> bool:
    cursor = -1
    for value in values:
        cursor = source.find(value, cursor + 1)
        if cursor < 0:
            return False
    return True


def job_source(source: str, job_name: str) -> str:
    lines = source.splitlines()
    try:
        start = lines.index(f"    {job_name}:")
    except ValueError:
        return ""
    end = next(
        (index for index in range(start + 1, len(lines)) if re.fullmatch(r" {4}[A-Za-z0-9_-]+:\s*", lines[index])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def step_source(source: str, name_prefix: str) -> str:
    lines = source.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith(f"- name: {name_prefix}")), -1)
    if start < 0:
        return ""
    indentation = len(lines[start]) - len(lines[start].lstrip())
    pattern = re.compile(rf"^ {{{indentation}}}- (?:name|uses|run):")
    end = next((i for i in range(start + 1, len(lines)) if pattern.match(lines[i])), len(lines))
    return "\n".join(lines[start:end])


class PublisherReleaseGateTests(unittest.TestCase):
    def test_atmospheric_provider_reads_and_publication_are_separately_held(self) -> None:
        source = read(".github/workflows/atmos-held-pipeline.yml")
        self.assertIn("workflow_dispatch:", source)
        self.assertNotRegex(source, r"(?m)^\s*(?:schedule|push|pull_request|workflow_run):")
        for name in ["allow_provider_downloads", "publish"]:
            self.assertRegex(source, rf"(?s){name}:\s*\n.*?type: boolean\s*\n\s*default: false")
        generate = job_source(source, "generate")
        publish = job_source(source, "publish")
        self.assertIn("github.ref == 'refs/heads/master' && inputs.allow_provider_downloads == true", generate)
        self.assertIn("github.ref == 'refs/heads/master' && inputs.allow_provider_downloads == true && inputs.publish == true", publish)
        self.assertIn("needs: proof", generate)
        self.assertIn("needs: generate", publish)
        self.assertNotIn("contents: write", generate)
        self.assertNotIn("secrets.", source)
        self.assertNotIn("pip install", publish)
        self.assertIn("scripts/atmos-native-tests", source)
        self.assertIn("--require-hashes --only-binary=:all:", generate)
        self.assertNotRegex(source, r"(?m)^\s*run:.*\$\{\{\s*inputs\.")

    def test_every_workflow_is_least_privilege_pinned_and_on_one_runner(self) -> None:
        paths = sorted(WORKFLOWS_DIR.glob("*.yml"))
        self.assertEqual(sum(1 for path in paths if PIPELINE_WORKFLOW.match(path.name)), 7)
        for path in paths:
            source = path.read_text()
            code = re.sub(r"(?m)^\s*#.*$", "", source)
            with self.subTest(workflow=path.name):
                # Top-level permissions are {} or a block, never a blanket grant.
                self.assertRegex(source, r"(?m)^permissions:\s*(?:\{\})?\s*$")
                self.assertNotRegex(code, r"\b(?:read-all|write-all)\b")
                runners = re.findall(r"(?m)^\s*runs-on:\s*([^\s#]+)\s*$", code)
                self.assertTrue(runners)
                self.assertEqual(set(runners), {"ubuntu-24.04"})
                for action, ref in re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*([^@\s]+)@([^\s#]+)", code):
                    self.assertEqual(REVIEWED_ACTION_PINS.get(action), ref, f"{action}@{ref} is not a reviewed pin")
                # Every pin keeps an audit-friendly version comment.
                for line in re.findall(r"(?m)^\s*(?:-\s+)?uses:.*$", source):
                    self.assertRegex(line, r"@[0-9a-f]{40} # v\d+\.\d+\.\d+\s*$", line)
                self.assertEqual(len(re.findall(r"(?m)^\s*(?:-\s+)?uses:", code)), len(re.findall(r"(?m)uses:\s*[^@\s]+@", code)))
                writes = sorted(re.findall(r"([A-Za-z0-9_-]+):\s*write\b", code))
                expected = ["contents"] if PIPELINE_WORKFLOW.match(path.name) or path.name in {"keepalive.yml", "atmos-held-pipeline.yml"} else []
                self.assertEqual(writes, expected)
                self.assertNotRegex(code, r"pull_request_target|workflow_run")

    def test_publishers_use_reviewed_master_sealed_artifacts_and_isolated_writer(self) -> None:
        for dataset in DATASETS:
            source = workflow(dataset)
            generate = job_source(source, "generate")
            publish = job_source(source, "publish")
            action_refs = re.findall(r"(?m)^\s*uses:\s*[^@\s]+@([^\s#]+)", source)
            run_stable = f"{dataset}-${{{{ github.run_id }}}}"
            with self.subTest(dataset=dataset):
                self.assertTrue(
                    includes_all(
                        source,
                        [
                            "runs-on: ubuntu-24.04",
                            "python-version: '3.11.15'",
                            "cancel-in-progress: false",
                            "persist-credentials: false",
                            "--require-hashes --only-binary=:all:",
                            "Run offline publisher",
                            "Generate and validate immutable bundle",
                            "Seal generated bundle as workflow artifact",
                            "Restore sealed bundle",
                            "overwrite: true",
                        ],
                    )
                )
                self.assertGreaterEqual(source.count(run_stable), 2)
                self.assertNotIn(f"{run_stable}-${{{{ github.run_attempt }}}}", source)
                self.assertNotIn("3.11.11", source)
                self.assertEqual(source.count("if: github.ref == 'refs/heads/master'"), 2)
                self.assertGreaterEqual(len(action_refs), 5)
                self.assertEqual(len(action_refs), len(re.findall(r"(?m)^\s*uses:", source)))
                self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs))
                self.assertIn("contents: read", generate)
                self.assertNotIn("contents: write", generate)
                self.assertIn("contents: write", publish)
                self.assertIn(
                    f"python scripts/publish_dataset.py --dataset {dataset} --bundle /tmp/publisher-bundle", publish
                )
                self.assertNotIn("pip install", publish)
                self.assertNotIn("COPERNICUS_", publish)
                self.assertLess(source.index("Run offline publisher"), source.index("Generate and validate immutable bundle"))
        self.assertIn("cron: '20 */6 * * *'", workflow("currents"))
        for lock in (read("scripts/cmems-requirements.lock"), read("scripts/mpa-pipeline/requirements.txt")):
            pinned = re.findall(r"(?m)^[-A-Za-z0-9_.]+==[^\n]+", lock)
            self.assertIn("--python-version 3.11.15", lock)
            self.assertNotIn("--python-version 3.11.11", lock)
            self.assertGreaterEqual(len(pinned), 5)
            self.assertGreaterEqual(lock.count("--hash=sha256:"), len(pinned))

    def test_waves_keep_one_exact_three_hour_margin_without_weakening_currents(self) -> None:
        # The app checks its half (cmemsGridTrust maxSourceAgeHours 15 for waves, 12 for currents).
        self.assertIn("cron: '0 7,19 * * *'", workflow("waves"))
        self.assertIn('maximum_source_age = 15 if dataset_key == "waves" else 12', read("scripts/publisher_contract.py"))
        self.assertTrue(
            includes_all(
                read("scripts/publisher-tests/test_contracts.py"),
                ["test_wave_freshness_allows_one_native_step_of_schedule_margin", "timedelta(hours=15, seconds=1)"],
            )
        )
        self.assertTrue(
            includes_all(
                read("scripts/cmems-waves-pipeline/README.md"),
                [
                    "Schedule: `07:00` and `19:00` UTC, exactly 12 hours apart",
                    "Maximum source age: 15 hours",
                    "one exact three-hour native-cadence margin",
                ],
            )
        )

    def test_offline_fixtures_have_a_localhost_only_dead_proxy_fence(self) -> None:
        ci = read(".github/workflows/ci.yml")
        # CI runs the suite once per lock, so a test that needs a package only the CMEMS jobs
        # install fails on push, not in the weekly MPA run (it broke that run for five weeks).
        self.assertTrue(
            includes_all(
                ci,
                [
                    "lock: scripts/cmems-requirements.lock",
                    "lock: scripts/mpa-pipeline/requirements.txt",
                    "cache-dependency-path: ${{ matrix.lock }}",
                    "LOCK: ${{ matrix.lock }}",
                    '-r "$LOCK"',
                ],
            )
        )
        self.assertNotRegex(ci, r"(?m)^\s+run:.*\$\{\{")
        for dataset, source in [(dataset, workflow(dataset)) for dataset in DATASETS] + [("ci", ci)]:
            offline = step_source(source, "Run offline publisher")
            with self.subTest(dataset=dataset):
                self.assertTrue(
                    includes_all(
                        offline,
                        [
                            "NO_PROXY: '127.0.0.1,localhost,::1'",
                            "HTTPS_PROXY: 'http://127.0.0.1:9'",
                            "HTTP_PROXY: 'http://127.0.0.1:9'",
                            "ALL_PROXY: 'http://127.0.0.1:9'",
                            'export no_proxy="$NO_PROXY"',
                            'export https_proxy="$HTTPS_PROXY"',
                            'export http_proxy="$HTTP_PROXY"',
                            'export all_proxy="$ALL_PROXY"',
                            "python -m unittest discover -s scripts/publisher-tests -p 'test_*.py'",
                        ],
                    )
                )
                self.assertNotRegex(offline, r"(?m)^\s+(?:no_proxy|https_proxy|http_proxy|all_proxy):")
                self.assertNotRegex(source, r"(?im)^\s*no_proxy:\s*['\"]?\*")
                self.assertNotRegex(offline, r"(?i)export\s+no_proxy=['\"]?\*")
                self.assertNotRegex(offline, r"\$\{\{\s*secrets\.|COPERNICUS_|\bcurl\b|\bwget\b")

    def test_producer_contract_validates_science_and_shards_before_dual_slot_publication(self) -> None:
        producers = {
            dataset: read(
                "scripts/mpa-pipeline/pipeline.py" if dataset == "mpa" else f"scripts/cmems-{dataset}-pipeline/pipeline.py"
            )
            for dataset in DATASETS
        }
        for dataset, source in producers.items():
            with self.subTest(producer=dataset):
                for forbidden in ("GH_TOKEN", "GITHUB_TOKEN", "gh release", "--clobber", "upload_to_github_release"):
                    self.assertNotIn(forbidden, source)
                if dataset != "mpa":
                    self.assertTrue(
                        includes_in_order(
                            source, ["validate_cmems_source(", "validate_thcu_payloads(", "build_cmems_bundle("]
                        )
                    )
        self.assertTrue(
            includes_in_order(
                producers["mpa"],
                ["fetch_capad_marine()", "normalise_features(raw)", "write_validated_geojson(normalised)", "build_mpa_bundle("],
            )
        )
        self.assertTrue(
            includes_all(
                read("scripts/publisher_contract.py"),
                [
                    "SCHEMA_VERSION = 2",
                    'max_file_bytes": 16 * 1024 * 1024',
                    "manifest is missing, non-regular or oversized",
                    "generation digest does not match source time and assets",
                    "same generation has altered immutable core metadata",
                    "candidate source start time regressed",
                    "validate_legacy_v1_bootstrap",
                    "legacy CMEMS manifest fields are not the known v1 shape",
                    "Australian external territories",
                    "SHARD_TAG_RE",
                    "def asset_shard_tag(",
                    "def validate_shard_inventory(",
                    "shard capacity guard must leave headroom below 1,000",
                    "asset shard would exceed the conservative",
                ],
            )
        )
        self.assertTrue(
            includes_all(
                read("scripts/cmems_contract.py"),
                [
                    "source cadence is not exactly",
                    "CF_METADATA_CONTRACT",
                    "require_invariant_finite_mask",
                    "implausible finite-ocean/land-mask fraction",
                    "refusing to fabricate ocean values",
                    "land mask contains values other than 0/1",
                    "encoded land mask changed between forecast steps",
                ],
            )
        )
        publisher = read("scripts/publish_dataset.py")
        self.assertTrue(
            includes_all(
                publisher,
                [
                    "publisher refuses to run with Copernicus credentials",
                    "remote immutable asset hash collision",
                    "uploaded asset cannot be downloaded",
                    'V2_MANIFEST_SLOTS = ("manifest-v2-a.json", "manifest-v2-b.json")',
                    "inactive manifest slot upload failed verification",
                    "target_slots = publication_manifest_slots(active_slot, target_slot)",
                    "validate_same_generation_core(current, draft)",
                    "validate_legacy_v1_bootstrap(legacy, dataset_key)",
                    "validate_mpa_feature_count_review_bound(",
                    '"--latest=false"',
                ],
            )
        )
        self.assertEqual(len(re.findall(r'upload_args\.append\("--clobber"\)', publisher)), 1)
        boundary = publisher[publisher.index("def publish(") : publisher.index("def main()")]
        self.assertTrue(
            includes_in_order(
                boundary,
                [
                    "validate_manifest(draft,",
                    "validate_bundle_layout(bundle_dir, draft)",
                    "validate_publish_context(draft, repo)",
                    "validate_publication_freshness(draft)",
                    "inventory = release_assets(shard_tag, repo)",
                    "existing = validate_shard_inventory(",
                    "remote immutable asset hash collision",
                    "validate_publication_freshness(draft)",
                    "ensure_release(discovery_tag,",
                    "ensure_release(shard_tag,",
                    '["release", "upload", shard_tag,',
                    'final["published_at"] =',
                    "target_slots = publication_manifest_slots(active_slot, target_slot)",
                    "for slot in target_slots:",
                    "upload_manifest_slot(discovery_tag, repo, slot,",
                    "Published verified generation %s to discovery slot(s)",
                ],
            )
        )

    def test_publisher_suites_keep_their_named_regressions(self) -> None:
        expectations = {
            "test_contracts.py": [
                "test_weekly_shard_derivation_and_iso_year_boundary",
                "test_shard_capacity_and_collision_guard_runs_before_mutation",
                "test_generation_digest_is_bound_to_ordered_hashes",
                "test_freshness_rejects_stale_and_future_sources",
                "test_consumer_metadata_size_and_coordinate_contracts_match_publisher_authority",
                "test_wave_freshness_allows_one_native_step_of_schedule_margin",
                "test_same_generation_refresh_allows_new_health_provenance_only",
                "test_real_shaped_legacy_bootstrap_and_arbitrary_legacy_rejection",
                "test_nonfinite_mask_domain_and_wave_magnitude_fail",
                "test_representative_external_territory_bounds_and_exact_properties",
            ],
            "test_source_gates.py": [
                "test_workflows_have_exact_trust_boundary",
                "test_schedule_interval_is_shorter_than_forecast_coverage",
                "test_producers_cannot_publish_or_clobber_stable_assets",
                "self.assertEqual(4 * 7 * 13, 364)",
                "self.assertEqual(2 * 7 * 17, 238)",
            ],
            "test_mpa_transport.py": [
                "test_valid_json_is_streamed_and_closed",
                "test_oversized_declared_body_is_rejected_before_iteration",
                "test_oversized_chunked_body_is_rejected_incrementally",
                "test_aggregate_page_budget_blocks_multi_page_memory_growth",
                "test_unsimplified_fallback_omits_offset_and_coordinate_rounding",
                "test_gis_area_hectares_are_converted_without_losing_tiny_positive_area",
                "test_sub_square_metre_slivers_keep_a_positive_area",
                "test_marine_sanctuaries_and_uncategorised_reserves_are_never_understated",
            ],
            "test_cmems_science.py": [
                "test_official_cf_metadata_is_accepted_for_every_field_and_axis",
                "test_convertible_but_unhandled_units_are_rejected_for_every_field_and_axis",
                "test_wrong_standard_names_and_coordinate_directions_are_rejected",
                "test_time_varying_nan_mask_is_rejected",
                "test_chlorophyll_floor_midpoint_and_ceiling_are_exact",
            ],
            "test_wave_math.py": [
                "test_wraparound_359_and_1_average_to_zero_not_180",
                "test_height_weighting_preserves_mean_height_as_vector_magnitude",
                "test_equal_maxima_use_lowest_normalized_bearing_not_array_order",
                "test_grid_fallback_uses_same_stable_tie_rule",
            ],
            "test_publish_flow.py": [
                "test_missing_release_is_created_at_validated_producer_commit",
                "test_release_lookup_failure_does_not_mutate_remote_state",
                "test_legacy_mpa_bootstrap_accepts_feature_count_within_review_bound",
                "test_legacy_mpa_bootstrap_rejects_feature_count_outside_review_bound_before_mutation",
                "test_absent_asset_reports_false_rather_than_raising",
                "test_real_failures_still_raise",
                "test_first_v2_publish_can_bootstrap_with_neither_slot_present",
            ],
            "test_ssh_grid.py": [
                "test_distinct_protocol_and_reserved_bytes",
                "test_exact_dimensions_size_and_original_time",
                "test_unknown_and_masked_values_are_not_silent",
                "test_nonfinite_and_nonphysical_never_publish",
                "test_stable_four_channel_ice_and_coast_mask",
                "test_freshness_has_no_future_forecast_exception",
                "test_original_cf_packed_values_fill_ice_and_bounds_dimension",
                "test_cf_wrong_units_identity_or_orientation_rejected",
                "test_cf_original_time_calendar_and_unreviewed_dimension_rejected",
            ],
        }
        for filename, names in expectations.items():
            with self.subTest(suite=filename):
                self.assertTrue(includes_all(read(f"scripts/publisher-tests/{filename}"), names))
        # A no-tests-deleted tripwire: a deliberate ADDITION moves it up.
        # 66 = the app repo's pin of 60, minus the two app-side source checks that
        # stayed with the app (proxy and Vercel routing), plus the eight in this file,
        # plus the MPA sliver-area and classification tests (68).
        counts = {path.name: len(re.findall(r"(?m)^\s*def test_", path.read_text()))
                  for path in sorted((REPO_ROOT / "scripts/publisher-tests").glob("test_*.py"))}
        # Original 68 stay pinned, plus the one new held-atmosphere workflow gate.
        self.assertEqual(sum(n for name, n in counts.items()
                             if name not in {'test_ssh_grid.py', 'test_atmos.py'}), 68 + 1)
        self.assertEqual(counts['test_ssh_grid.py'], 14)
        self.assertEqual(counts['test_atmos.py'], 15)
        self.assertEqual(sum(counts.values()), 68 + 1 + 14 + 15)

    def test_pipeline_readmes_keep_their_science_claims(self) -> None:
        self.assertTrue(
            includes_all(
                read("scripts/cmems-currents-pipeline/README.md"),
                ["exactly 13 hourly snapshots", "deterministic UTC ISO-week shard", "364 current assets"],
            )
        )
        self.assertTrue(
            includes_all(
                read("scripts/cmems-waves-pipeline/README.md"),
                [
                    "17 snapshots at three-hour cadence",
                    "12-hour publish interval plus",
                    "one exact three-hour native-cadence margin",
                    "fail closed after that 15-hour boundary",
                    "height-weighted circular mean",
                ],
            )
        )
        self.assertTrue(
            includes_all(
                read("scripts/mpa-pipeline/README.md"),
                [
                    "GIS_AREA` is hectares and is explicitly divided by 100",
                    "classification_source: indicative_heuristic",
                    "UTC ISO-week release shard",
                ],
            )
        )

    def test_products_schedules_and_retired_paths_match_what_the_app_attributes(self) -> None:
        # The app's attribution (tests/CmemsAttribution.test.tsx) and hosted freshness
        # checks name these exact products and schedules.
        products = {
            "currents": "cmems_mod_glo_phy_anfc_merged-uv_PT1H-i",
            "waves": "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i",
            "sst": "cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m",
            "chl": "cmems_mod_glo_bgc-pft_anfc_0.25deg_P1D-m",
            "seaice": "cmems_mod_glo_phy_anfc_0.083deg_P1D-m",
            "mld": "cmems_mod_glo_phy_anfc_0.083deg_P1D-m",
        }
        for dataset, product in products.items():
            with self.subTest(dataset=dataset):
                self.assertIn(product, read(f"scripts/cmems-{dataset}-pipeline/pipeline.py"))
        for dataset, cron in (
            ("currents", "- cron: '20 */6 * * *'"),
            ("currents", "- cron: '20 3,9,15,21 * * *'"),
            ("sst", "- cron: '17 3,15 * * *'"),
            ("chl", "- cron: '47 4,16 * * *'"),
        ):
            self.assertIn(cron, workflow(dataset))
        # The retired Mapbox tileset publisher left no credential, endpoint or
        # instruction anywhere (the markers are split so this file never matches).
        markers = [
            "_".join(["MAPBOX", "UPLOAD", "TOKEN"]),
            "_".join(["MAPBOX", "USERNAME"]),
            "_".join(["VITE", "MAPBOX", "USERNAME"]),
            "-".join(["mapbox", "tilesets"]),
            "/".join(["api.mapbox.com", "tilesets", "v1"]),
            "/".join(["api.mapbox.com", "rasterarrays", "v1"]),
            "-".join(["thalassa", "currents", "h"]),
        ]
        for path in sorted(REPO_ROOT.rglob("*")):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text().lower()
            except UnicodeDecodeError:
                continue
            for marker in markers:
                self.assertNotIn(marker.lower(), text, f"{path.relative_to(REPO_ROOT)} names the retired {marker}")


if __name__ == "__main__":
    unittest.main()
