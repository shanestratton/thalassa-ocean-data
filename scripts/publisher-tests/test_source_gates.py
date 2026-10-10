from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = [
    *sorted((REPO_ROOT / ".github/workflows").glob("cmems-*-pipeline.yml")),
    REPO_ROOT / ".github/workflows/mpa-pipeline.yml",
]
PRODUCERS = [
    *sorted((REPO_ROOT / "scripts").glob("cmems-*-pipeline/pipeline.py")),
    REPO_ROOT / "scripts/mpa-pipeline/pipeline.py",
]


# The checks on the app's side of the contract (the Vercel release proxy, the
# clients' manifest paths and the dotted-asset routing) live with the app:
# thalassa-marine-weather tests/MarineReleaseSourceGates.test.ts.
class PublisherSourceGateTests(unittest.TestCase):
    def test_workflows_have_exact_trust_boundary(self) -> None:
        self.assertEqual(len(WORKFLOWS), 7)
        action_ref = re.compile(r"uses:\s+[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@([0-9a-f]{40})(?:\s|$)")
        for path in WORKFLOWS:
            text = path.read_text()
            with self.subTest(path=path.name):
                self.assertNotIn("3.11.11", text)
                self.assertIn("python-version: '3.11.15'", text)
                self.assertIn("runs-on: ubuntu-24.04", text)
                self.assertIn("cancel-in-progress: false", text)
                self.assertEqual(text.count("if: github.ref == 'refs/heads/master'"), 2)
                self.assertNotIn("github.event_name", text)
                self.assertGreaterEqual(text.count("persist-credentials: false"), 2)
                self.assertIn("--require-hashes --only-binary=:all:", text)
                self.assertLess(text.index("Run offline"), text.index("Generate and validate"))
                self.assertNotIn("NO_PROXY: '*'", text)
                refs = action_ref.findall(text)
                self.assertGreaterEqual(len(refs), 5)
                self.assertEqual(text.count("uses:"), len(refs))
                publish = text.split("\n    publish:", 1)[1]
                self.assertNotIn("COPERNICUS_", publish)
                self.assertNotIn("pip install", publish)
                self.assertIn("contents: write", publish)
                self.assertIn("github.run_id", publish)
                self.assertNotIn("github.run_attempt", publish)
                self.assertIn("GITHUB_REPOSITORY", publish)
                for default_name in ("GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
                    self.assertNotRegex(publish, rf"(?m)^\s+{default_name}:", msg=f"{path.name} overrides {default_name}")
                generate = text.split("\n    generate:", 1)[1].split("\n    publish:", 1)[0]
                self.assertIn("contents: read", generate)
                self.assertNotIn("contents: write", generate)
                self.assertIn("overwrite: true", generate)

    def test_schedule_interval_is_shorter_than_forecast_coverage(self) -> None:
        expectations = {
            "cmems-currents-pipeline.yml": (6, 13, 1, 12, "20 */6 * * *"),
            "cmems-waves-pipeline.yml": (12, 17, 3, 15, "0 7,19 * * *"),
            # Twice daily since f66e5150 (2026-08-28, minute offsets against
            # the top-of-hour thundering herd). That commit changed the crons
            # WITHOUT updating these expectations, and because every pipeline
            # runs this same suite, two mismatches failed all six pipelines
            # for four days — every SEA layer on the OBS chart starved off a
            # 502 "No valid dataset manifest slot" (found 2026-09-02).
            "cmems-sst-pipeline.yml": (12, 6, 24, 48, "17 3,15 * * *"),
            "cmems-chl-pipeline.yml": (12, 6, 24, 48, "47 4,16 * * *"),
            "cmems-seaice-pipeline.yml": (24, 6, 24, 48, "30 16 * * *"),
            "cmems-mld-pipeline.yml": (24, 6, 24, 48, "0 17 * * *"),
        }
        by_name = {path.name: path.read_text() for path in WORKFLOWS}
        for filename, (interval, steps, cadence, maximum_source_age, cron) in expectations.items():
            self.assertLess(interval, (steps - 1) * cadence, filename)
            self.assertLessEqual(interval, maximum_source_age, filename)
            self.assertIn(f"cron: '{cron}'", by_name[filename])
        self.assertEqual(4 * 7 * 13, 364)
        self.assertEqual(2 * 7 * 17, 238)
        self.assertLess(364, 900)
        self.assertLess(238, 900)
        waves_readme = (REPO_ROOT / "scripts/cmems-waves-pipeline/README.md").read_text()
        self.assertIn("12-hour publish interval plus", waves_readme)
        self.assertIn("one exact three-hour native-cadence margin", waves_readme)
        self.assertIn("fail closed after that 15-hour boundary", waves_readme)

    def test_producers_cannot_publish_or_clobber_stable_assets(self) -> None:
        for path in PRODUCERS:
            text = path.read_text()
            with self.subTest(path=path):
                self.assertNotIn("GH_TOKEN", text)
                self.assertNotIn("GITHUB_TOKEN", text)
                self.assertNotIn("gh release", text)
                self.assertNotIn("--clobber", text)
                self.assertNotIn("upload_to_github_release", text)
        publisher = (REPO_ROOT / "scripts/publish_dataset.py").read_text()
        self.assertIn("uploaded asset cannot be downloaded", publisher)
        self.assertIn("inactive manifest slot upload failed verification", publisher)
        self.assertIn("target_slots = publication_manifest_slots", publisher)
        self.assertIn("validate_mpa_feature_count_review_bound(", publisher)
        self.assertIn('V2_MANIFEST_SLOTS = ("manifest-v2-a.json", "manifest-v2-b.json")', publisher)
        self.assertNotIn('LEGACY_MANIFEST_NAME, str(final_path)', publisher)
        publish_body = publisher.split("def publish(", 1)[1]
        self.assertLess(publish_body.index("validate_publish_context"), publish_body.index("download_asset"))
        self.assertGreaterEqual(publish_body.count("validate_publication_freshness(draft)"), 3)
        self.assertIn('"--target"', publisher)
        self.assertIn('"--latest=false"', publisher)
        mpa = (REPO_ROOT / "scripts/mpa-pipeline/pipeline.py").read_text()
        self.assertIn('WHERE = "GIS_AREA>0"', mpa)
        self.assertIn("unsimplified fallback lost source IDs", mpa)
        self.assertIn("MAX_AGGREGATE_SOURCE_BYTES = 64 * 1024 * 1024", mpa)
        self.assertIn("MAX_AGGREGATE_COORDINATES = 5_000_000", mpa)

    def test_hash_locks_are_pinned_and_target_current_patch(self) -> None:
        for path in (REPO_ROOT / "scripts/cmems-requirements.lock", REPO_ROOT / "scripts/mpa-pipeline/requirements.txt"):
            text = path.read_text()
            with self.subTest(path=path):
                self.assertIn("--python-version 3.11.15", text)
                self.assertNotRegex(text, r"^[A-Za-z0-9_.-]+\s*[>~]", msg=str(path))
                packages = re.findall(r"^([A-Za-z0-9_.-]+)==[^\n]+", text, flags=re.MULTILINE)
                self.assertGreater(len(packages), 4)
                self.assertGreaterEqual(text.count("--hash=sha256:"), len(packages))


if __name__ == "__main__":
    unittest.main()
