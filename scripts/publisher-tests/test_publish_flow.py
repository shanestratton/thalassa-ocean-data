from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import publish_dataset as publisher


class ReleaseCreationTrustTests(unittest.TestCase):
    @staticmethod
    def completed(payload: object | None = None, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
        stdout = "" if payload is None else json.dumps(payload)
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)

    @staticmethod
    def public_release(tag: str, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "tagName": tag,
            "isDraft": False,
            "isPrerelease": False,
            "isImmutable": False,
            "targetCommitish": "a" * 40,
        }
        value.update(overrides)
        return value

    @staticmethod
    def tag_ref(tag: str, sha: str = "a" * 40) -> dict[str, object]:
        return {"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": sha}}

    def test_missing_release_is_created_at_validated_producer_commit(self) -> None:
        tag = "cmems-currents-latest-assets-2026-W32"
        missing = self.completed(returncode=1, stderr="release not found")
        tag_missing = self.completed(returncode=1, stderr="HTTP 404: Not Found")
        with patch.object(
            publisher,
            "run_gh",
            side_effect=[
                missing,
                tag_missing,
                self.completed(),
                self.completed(self.public_release(tag)),
                self.completed(self.tag_ref(tag)),
            ],
        ) as run:
            publisher.ensure_release(
                tag,
                "owner/repo",
                "currents",
                "a" * 40,
                asset_shard=True,
            )
        create_args = run.call_args_list[2].args[0]
        self.assertEqual(create_args[:3], ["release", "create", tag])
        self.assertEqual(create_args[create_args.index("--target") + 1], "a" * 40)
        self.assertIn("--latest=false", create_args)

    def test_existing_release_must_be_public_mutable_and_have_a_commit_tag(self) -> None:
        tag = "cmems-currents-latest"
        with patch.object(
            publisher,
            "run_gh",
            side_effect=[
                self.completed(self.public_release(tag)),
                self.completed(self.tag_ref(tag, "b" * 40)),
            ],
        ) as run:
            publisher.ensure_release(tag, "owner/repo", "currents", "a" * 40, asset_shard=False)
        self.assertEqual(run.call_count, 2)

        with patch.object(
            publisher,
            "run_gh",
            return_value=self.completed(self.public_release(tag, isDraft=True)),
        ):
            with self.assertRaisesRegex(publisher.ContractError, "public non-prerelease"):
                publisher.ensure_release(tag, "owner/repo", "currents", "a" * 40, asset_shard=False)

    def test_preexisting_tag_cannot_bypass_validated_create_target(self) -> None:
        tag = "cmems-currents-latest-assets-2026-W32"
        with patch.object(
            publisher,
            "run_gh",
            side_effect=[
                self.completed(returncode=1, stderr="release not found"),
                self.completed(self.tag_ref(tag, "b" * 40)),
            ],
        ) as run:
            with self.assertRaisesRegex(publisher.ContractError, "does not target"):
                publisher.ensure_release(tag, "owner/repo", "currents", "a" * 40, asset_shard=True)
        self.assertEqual(run.call_count, 2)

    def test_repository_visibility_must_be_public(self) -> None:
        with patch.object(publisher, "run_gh", return_value=self.completed({"visibility": "PRIVATE"})):
            with self.assertRaisesRegex(publisher.ContractError, "publicly readable"):
                publisher.validate_public_repository("owner/repo")

    def test_release_lookup_failure_does_not_mutate_remote_state(self) -> None:
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 500 from GitHub")
        with patch.object(publisher, "run_gh", return_value=failed) as run:
            with self.assertRaisesRegex(RuntimeError, "release view failed"):
                publisher.ensure_release(
                    "cmems-currents-latest",
                    "owner/repo",
                    "currents",
                    "a" * 40,
                    asset_shard=False,
                )
        run.assert_called_once()


class ManifestSlotTrustTests(unittest.TestCase):
    @staticmethod
    def manifest(data_start: str, published_at: str, generation: str) -> dict[str, str]:
        return {"data_start": data_start, "published_at": published_at, "generation": generation}

    def test_selection_is_deterministic_and_targets_only_inactive_slot(self) -> None:
        older = self.manifest("2026-08-05T00:00:00Z", "2026-08-05T02:00:00Z", "g-old")
        newer = self.manifest("2026-08-05T01:00:00Z", "2026-08-05T01:30:00Z", "g-new")
        first = {"manifest-v2-a.json": older, "manifest-v2-b.json": newer}
        reversed_order = {"manifest-v2-b.json": newer, "manifest-v2-a.json": older}
        self.assertEqual(publisher.inactive_manifest_slot(first, set(first)), ("manifest-v2-b.json", "manifest-v2-a.json"))
        self.assertEqual(publisher.inactive_manifest_slot(reversed_order, set(first)), ("manifest-v2-b.json", "manifest-v2-a.json"))

    def test_bootstrap_seeds_missing_slot_and_verified_upload_never_names_active_slot(self) -> None:
        self.assertEqual(
            publisher.publication_manifest_slots(None, "manifest-v2-a.json"),
            ("manifest-v2-a.json", "manifest-v2-b.json"),
        )
        self.assertEqual(
            publisher.publication_manifest_slots("manifest-v2-b.json", "manifest-v2-a.json"),
            ("manifest-v2-a.json",),
        )
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            candidate = directory / "manifest-v2-a.json"
            verified = directory / "verified.json"
            candidate.write_bytes(b'{"candidate":true}\n')

            def download(_tag: str, _repo: str, slot: str, destination: Path) -> bool:
                self.assertEqual(slot, "manifest-v2-a.json")
                destination.write_bytes(candidate.read_bytes())
                return True

            with (
                patch.object(publisher, "run_gh", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
                patch.object(publisher, "download_asset", side_effect=download),
            ):
                publisher.upload_manifest_slot(
                    "cmems-currents-latest",
                    "owner/repo",
                    "manifest-v2-a.json",
                    candidate,
                    {"manifest-v2-a.json", "manifest-v2-b.json"},
                    verified,
                )
            upload_args = run.call_args.args[0]
            self.assertIn("manifest-v2-a.json", str(upload_args))
            self.assertNotIn("manifest-v2-b.json", str(upload_args))
            self.assertIn("--clobber", upload_args)

    def test_failed_inactive_slot_upload_leaves_active_slot_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            candidate = directory / "manifest-v2-a.json"
            candidate.write_bytes(b'{"candidate":true}\n')
            with (
                patch.object(
                    publisher,
                    "run_gh",
                    return_value=subprocess.CompletedProcess([], 1, "", "upload failed"),
                ) as run,
                patch.object(publisher, "download_asset", return_value=False) as download,
            ):
                with self.assertRaisesRegex(publisher.ContractError, "inactive manifest slot"):
                    publisher.upload_manifest_slot(
                        "cmems-currents-latest",
                        "owner/repo",
                        "manifest-v2-a.json",
                        candidate,
                        {"manifest-v2-a.json", "manifest-v2-b.json"},
                        directory / "verified.json",
                    )
            self.assertNotIn("manifest-v2-b.json", str(run.call_args.args[0]))
            self.assertEqual(download.call_args.args[2], "manifest-v2-a.json")

    @staticmethod
    def legacy_mpa_manifest(feature_count: int) -> dict[str, object]:
        return {
            "version": 1,
            "generated_at": "2026-08-04T00:00:00+00:00",
            "feature_count": feature_count,
            "data_file": "mpa.geojson",
            "attribution": "© Commonwealth of Australia (DCCEEW)",
        }

    @staticmethod
    def draft_mpa_manifest(feature_count: int) -> dict[str, object]:
        return {
            "dataset": {"key": "mpa"},
            "generation": "g-20260805T000000Z-0123456789ab",
            "dimensions": {"feature_count": feature_count},
            "files": [],
        }

    def run_legacy_mpa_bootstrap(
        self,
        *,
        legacy_feature_count: int,
        draft_feature_count: int,
        mutations: list[str],
    ) -> None:
        draft = self.draft_mpa_manifest(draft_feature_count)
        legacy = self.legacy_mpa_manifest(legacy_feature_count)
        with tempfile.TemporaryDirectory() as name:
            with (
                patch.dict(os.environ, {"GH_TOKEN": "test-token"}, clear=True),
                patch.object(publisher, "load_manifest", side_effect=[draft, legacy]),
                patch.object(publisher, "validate_manifest"),
                patch.object(publisher, "validate_bundle_layout"),
                patch.object(publisher, "validate_publish_context", return_value="a" * 40),
                patch.object(publisher, "validate_publication_freshness"),
                patch.object(publisher, "validate_public_repository"),
                patch.object(publisher, "read_manifest_slots", return_value=({}, set())),
                patch.object(publisher, "download_asset", return_value=True),
                patch.object(publisher, "release_assets", return_value=[]),
                patch.object(
                    publisher,
                    "ensure_release",
                    side_effect=lambda *args, **kwargs: mutations.append(f"release:{args[0]}"),
                ),
                patch.object(
                    publisher,
                    "upload_manifest_slot",
                    side_effect=lambda _tag, _repo, slot, *_args: mutations.append(f"slot:{slot}"),
                ),
            ):
                publisher.publish("mpa", Path(name), "owner/repo")

    def test_legacy_mpa_bootstrap_accepts_feature_count_within_review_bound(self) -> None:
        mutations: list[str] = []
        self.run_legacy_mpa_bootstrap(
            legacy_feature_count=4541,
            draft_feature_count=5000,
            mutations=mutations,
        )
        self.assertEqual(
            mutations[-2:],
            ["slot:manifest-v2-a.json", "slot:manifest-v2-b.json"],
        )

    def test_legacy_mpa_bootstrap_rejects_feature_count_outside_review_bound_before_mutation(self) -> None:
        mutations: list[str] = []
        with self.assertRaisesRegex(publisher.ContractError, "feature-count delta exceeds 25%"):
            self.run_legacy_mpa_bootstrap(
                legacy_feature_count=4541,
                draft_feature_count=3000,
                mutations=mutations,
            )
        self.assertEqual(mutations, [])


if __name__ == "__main__":
    unittest.main()


class DownloadAssetAbsenceTests(unittest.TestCase):
    """`download_asset` must tell "not there" apart from "went wrong".

    read_manifest_slots asks for BOTH v2 manifest slots before it can write
    either one, so a missing-asset reply mistaken for an error deadlocks the
    publisher: it cannot create the slots because it insists on reading them
    first. That is exactly what happened on the v2 cutover — gh answers
    "no assets match the file pattern", which the original phrase list did not
    recognise, and every CMEMS pipeline plus MPA failed on every scheduled run
    from then on. The grids kept uploading; the manifests never did.

    The existing flow tests all patch download_asset wholesale, so the message
    matching itself had no coverage. That is why this survived.
    """

    @staticmethod
    def failed(stderr: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)

    def test_absent_asset_reports_false_rather_than_raising(self) -> None:
        for stderr in (
            "release download failed: no assets match the file pattern",
            "no assets found",
            "release not found",
            "HTTP 404: Not Found",
            "the release does not exist",
        ):
            with self.subTest(stderr=stderr):
                with patch.object(publisher, "run_gh", return_value=self.failed(stderr)):
                    self.assertIs(
                        publisher.download_asset("tag", "owner/repo", "manifest-v2-a.json", Path("/tmp/x")),
                        False,
                    )

    def test_real_failures_still_raise(self) -> None:
        # A gate that swallows auth/network/rate-limit errors would publish a
        # manifest that silently drops previously-live slots.
        for stderr in (
            "HTTP 401: Bad credentials",
            "API rate limit exceeded",
            "dial tcp: lookup api.github.com: no such host",
        ):
            with self.subTest(stderr=stderr):
                with patch.object(publisher, "run_gh", return_value=self.failed(stderr)):
                    with self.assertRaises(RuntimeError):
                        publisher.download_asset("tag", "owner/repo", "manifest-v2-a.json", Path("/tmp/x"))

    def test_first_v2_publish_can_bootstrap_with_neither_slot_present(self) -> None:
        # The end-to-end shape of the deadlock: both slots absent must yield
        # "nothing valid, nothing present", not an exception.
        with patch.object(publisher, "run_gh", return_value=self.failed("no assets match the file pattern")):
            with tempfile.TemporaryDirectory() as tmp:
                valid, present = publisher.read_manifest_slots("tag", "owner/repo", "currents", Path(tmp))
        self.assertEqual(valid, {})
        self.assertEqual(present, set())
