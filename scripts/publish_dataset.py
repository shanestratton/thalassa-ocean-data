#!/usr/bin/env python3
"""Publish an already-generated bundle without producer credentials or deps."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from publisher_contract import (
    DATASETS,
    DRAFT_MANIFEST_NAME,
    ContractError,
    asset_shard_tag,
    load_manifest,
    sha256_path,
    utc_iso,
    validate_manifest,
    validate_bundle_layout,
    validate_legacy_v1_bootstrap,
    validate_mpa_feature_count_review_bound,
    validate_no_source_regression,
    validate_publication_freshness,
    validate_publish_context,
    validate_same_generation_core,
    validate_shard_inventory,
)

log = logging.getLogger("trusted-release-publisher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

LEGACY_MANIFEST_NAME = "manifest.json"
V2_MANIFEST_SLOTS = ("manifest-v2-a.json", "manifest-v2-b.json")


def run_gh(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def _is_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    message = (result.stderr + result.stdout).lower()
    return "not found" in message or "does not exist" in message or "release not found" in message


def _json_object(result: subprocess.CompletedProcess[str], label: str) -> dict[str, object]:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} is malformed")
    return value


def validate_public_repository(repo: str) -> None:
    result = run_gh(["repo", "view", repo, "--json", "visibility"], check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh repo view failed for {repo}: {result.stderr.strip()}")
    payload = _json_object(result, "repository state")
    if payload.get("visibility") != "PUBLIC":
        raise ContractError("publisher repository must be publicly readable")


def release_state(tag: str, repo: str) -> dict[str, object] | None:
    result = run_gh(
        [
            "release",
            "view",
            tag,
            "--repo",
            repo,
            "--json",
            "tagName,isDraft,isPrerelease,isImmutable,targetCommitish",
        ],
        check=False,
    )
    if result.returncode != 0:
        if _is_not_found(result):
            return None
        raise RuntimeError(f"gh release view failed for {tag}: {result.stderr.strip()}")
    payload = _json_object(result, f"release state for {tag}")
    if payload.get("tagName") != tag:
        raise ContractError(f"release {tag} returned a different tag")
    if payload.get("isDraft") is not False or payload.get("isPrerelease") is not False:
        raise ContractError(f"release {tag} must be a public non-prerelease release")
    if payload.get("isImmutable") is not False:
        raise ContractError(f"release {tag} must remain mutable for the active publication window")
    if not isinstance(payload.get("targetCommitish"), str) or not payload["targetCommitish"]:
        raise ContractError(f"release {tag} has no target commitish")
    return payload


def resolve_tag_commit(tag: str, repo: str) -> str | None:
    result = run_gh(["api", f"repos/{repo}/git/ref/tags/{quote(tag, safe='')}"], check=False)
    if result.returncode != 0:
        if _is_not_found(result):
            return None
        raise RuntimeError(f"gh tag lookup failed for {tag}: {result.stderr.strip()}")
    payload = _json_object(result, f"tag ref for {tag}")
    if payload.get("ref") != f"refs/tags/{tag}":
        raise ContractError(f"tag ref for {tag} is malformed")
    for _ in range(5):
        target = payload.get("object")
        if not isinstance(target, dict):
            raise ContractError(f"tag target for {tag} is malformed")
        target_type = target.get("type")
        target_sha = target.get("sha")
        if not isinstance(target_sha, str) or len(target_sha) != 40 or any(char not in "0123456789abcdef" for char in target_sha):
            raise ContractError(f"tag target for {tag} is not a full commit SHA")
        if target_type == "commit":
            return target_sha
        if target_type != "tag":
            raise ContractError(f"tag target for {tag} is not a commit")
        result = run_gh(["api", f"repos/{repo}/git/tags/{target_sha}"], check=False)
        if result.returncode != 0:
            raise RuntimeError(f"gh annotated-tag lookup failed for {tag}: {result.stderr.strip()}")
        payload = _json_object(result, f"annotated tag for {tag}")
    raise ContractError(f"tag {tag} exceeds the annotated-tag depth limit")


# Phrases gh uses when the thing simply is not there, as opposed to a real
# failure (auth, network, rate limit). Getting this list wrong is not a
# cosmetic bug: read_manifest_slots asks for BOTH v2 slots before it can write
# either, so a missing-asset reply that is mistaken for an error deadlocks the
# publisher — it cannot create the slots because it insists on reading them
# first.
#
# "no assets match the file pattern" is what `gh release download --pattern`
# actually says, and it matches none of the three phrases this list originally
# held. Every CMEMS pipeline (currents, waves, sst, chl, seaice, mld) plus MPA
# shares this publisher, so all seven died on the v2 cutover and stayed dead:
# the grids kept uploading, the v2 manifests never appeared, and the API
# answered 502 "No valid dataset manifest slot" for months.
_ABSENT_ASSET_PHRASES = (
    "no assets found",
    "no assets match",
    "release not found",
    "not found",
    "does not exist",
)


def download_asset(tag: str, repo: str, filename: str, destination: Path) -> bool:
    result = run_gh(
        ["release", "download", tag, "--repo", repo, "--pattern", filename, "--output", str(destination)],
        check=False,
    )
    if result.returncode == 0:
        return True
    message = (result.stderr + result.stdout).lower()
    if any(phrase in message for phrase in _ABSENT_ASSET_PHRASES):
        return False
    raise RuntimeError(f"gh release download failed for {filename}: {result.stderr.strip()}")


def release_assets(tag: str, repo: str) -> list[dict[str, object]] | None:
    result = run_gh(["release", "view", tag, "--repo", repo, "--json", "assets"], check=False)
    if result.returncode != 0:
        message = (result.stderr + result.stdout).lower()
        if "not found" in message or "does not exist" in message or "release not found" in message:
            return None
        raise RuntimeError(f"gh release view failed for {tag}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"release asset inventory for {tag} is not JSON") from exc
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, list):
        raise ContractError(f"release asset inventory for {tag} is malformed")
    normalized: list[dict[str, object]] = []
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str) or not isinstance(asset.get("size"), int):
            raise ContractError(f"release asset inventory for {tag} has a malformed entry")
        normalized.append({"name": asset["name"], "size": asset["size"]})
    return normalized


def active_manifest_slot(manifests: dict[str, dict]) -> str | None:
    """Select independently of fetch order: source time, publish time, generation, slot."""
    if not manifests:
        return None
    return max(
        manifests,
        key=lambda slot: (
            manifests[slot]["data_start"],
            manifests[slot]["published_at"],
            manifests[slot]["generation"],
            slot,
        ),
    )


def inactive_manifest_slot(manifests: dict[str, dict], present: set[str]) -> tuple[str | None, str]:
    active = active_manifest_slot(manifests)
    if active is None:
        return None, next((slot for slot in V2_MANIFEST_SLOTS if slot not in present), V2_MANIFEST_SLOTS[0])
    target = V2_MANIFEST_SLOTS[1] if active == V2_MANIFEST_SLOTS[0] else V2_MANIFEST_SLOTS[0]
    return active, target


def publication_manifest_slots(active: str | None, inactive: str) -> tuple[str, ...]:
    return V2_MANIFEST_SLOTS if active is None else (inactive,)


def read_manifest_slots(
    tag: str,
    repo: str,
    dataset_key: str,
    directory: Path,
) -> tuple[dict[str, dict], set[str]]:
    valid: dict[str, dict] = {}
    present: set[str] = set()
    for slot in V2_MANIFEST_SLOTS:
        path = directory / slot
        if not download_asset(tag, repo, slot, path):
            continue
        present.add(slot)
        try:
            manifest = load_manifest(path)
            validate_manifest(manifest, expected_dataset=dataset_key, allow_draft=False)
        except ContractError as exc:
            log.warning("Ignoring invalid inactive discovery slot %s: %s", slot, exc)
            continue
        valid[slot] = manifest
    return valid, present


def ensure_release(tag: str, repo: str, dataset_key: str, target_commit: str, *, asset_shard: bool) -> None:
    state = release_state(tag, repo)
    if state is not None:
        # A weekly shard tag is its creation anchor and legitimately receives
        # assets from later commits. Existing tags therefore need to resolve
        # to a real commit, but must not be forced to the current producer SHA.
        if resolve_tag_commit(tag, repo) is None:
            raise ContractError(f"existing release {tag} has no resolvable tag target")
        return
    existing_target = resolve_tag_commit(tag, repo)
    if existing_target is not None and existing_target != target_commit:
        raise ContractError(f"pre-existing tag {tag} does not target the validated producer commit")
    run_gh(
        [
            "release",
            "create",
            tag,
            "--repo",
            repo,
            "--target",
            target_commit,
            "--latest=false",
            "--title",
            f"Thalassa {dataset_key} {'immutable assets' if asset_shard else 'rolling manifest'}",
            "--notes",
            (
                "Immutable generation assets for one UTC ISO week. Assets are never replaced or deleted."
                if asset_shard
                else "Stable discovery release. Clients select the newest valid manifest-v2-a/b.json slot."
            ),
        ]
    )
    if release_state(tag, repo) is None:
        raise ContractError(f"created release {tag} is not publicly readable")
    if resolve_tag_commit(tag, repo) != target_commit:
        raise ContractError(f"created release {tag} does not target the validated producer commit")


def upload_manifest_slot(
    tag: str,
    repo: str,
    slot: str,
    candidate: Path,
    present_slots: set[str],
    verification_path: Path,
) -> None:
    upload_args = ["release", "upload", tag, str(candidate), "--repo", repo]
    if slot in present_slots:
        upload_args.append("--clobber")
    upload = run_gh(upload_args, check=False)
    verified = download_asset(tag, repo, slot, verification_path)
    if not verified or verification_path.read_bytes() != candidate.read_bytes():
        detail = upload.stderr.strip() or upload.stdout.strip() or f"exit {upload.returncode}"
        raise ContractError(f"inactive manifest slot upload failed verification ({detail})")


def publish(dataset_key: str, bundle_dir: Path, repo: str) -> None:
    if any(name.startswith("COPERNICUS_") for name in os.environ):
        raise ContractError("publisher refuses to run with Copernicus credentials in its environment")
    if not repo or repo.count("/") != 1:
        raise ContractError("GITHUB_REPOSITORY must be owner/repository")
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        raise ContractError("publisher requires GH_TOKEN or GITHUB_TOKEN")

    spec = DATASETS[dataset_key]
    draft = load_manifest(bundle_dir / DRAFT_MANIFEST_NAME)
    validate_manifest(draft, expected_dataset=dataset_key, allow_draft=True, bundle_dir=bundle_dir)
    validate_bundle_layout(bundle_dir, draft)
    target_commit = validate_publish_context(draft, repo)
    validate_publication_freshness(draft)
    validate_public_repository(repo)
    discovery_tag = str(spec["release_tag"])
    shard_tag = asset_shard_tag(dataset_key, draft["generation"])
    with tempfile.TemporaryDirectory(prefix=f"publish-{dataset_key}-") as temp_name:
        temp = Path(temp_name)
        slots, present_slots = read_manifest_slots(discovery_tag, repo, dataset_key, temp)
        active_slot, target_slot = inactive_manifest_slot(slots, present_slots)
        current: dict | None = None
        if active_slot is not None:
            current = slots[active_slot]
        else:
            # Legacy discovery remains byte-for-byte available during the
            # migration window. It is inspected only for a safe first stage;
            # this publisher never replaces manifest.json or stable v1 assets.
            legacy_path = temp / LEGACY_MANIFEST_NAME
            if download_asset(discovery_tag, repo, LEGACY_MANIFEST_NAME, legacy_path):
                legacy = load_manifest(legacy_path)
                if legacy.get("schema_version") == 2:
                    validate_manifest(legacy, expected_dataset=dataset_key, allow_draft=False)
                    current = legacy
                    log.warning("Staging a dual-slot manifest from a prior stable schema-v2 manifest")
                else:
                    validate_legacy_v1_bootstrap(legacy, dataset_key)
                    if dataset_key == "mpa":
                        # V1 has no trustworthy immutable bytes or registry
                        # source timestamp to compare. Its exact feature count
                        # can still enforce the existing v2 ±25% review bound
                        # before either discovery slot is created.
                        validate_mpa_feature_count_review_bound(
                            legacy.get("feature_count"),
                            draft.get("dimensions", {}).get("feature_count"),
                        )
                    log.info("Recognized legacy-v1 discovery; preserving it while staging schema-v2 slots")

        if current is not None:
            validate_no_source_regression(current, draft)
            if current.get("generation") == draft.get("generation"):
                validate_same_generation_core(current, draft)
                log.info(
                    "Generation %s is unchanged; assets will be reverified and publisher-health timestamps refreshed",
                    draft["generation"],
                )

        # Read and validate the entire target shard before creating a release or
        # uploading anything. The 900-asset guard leaves headroom below GitHub's
        # hard 1,000-asset limit even under repeated manual dispatches.
        inventory = release_assets(shard_tag, repo)
        existing = validate_shard_inventory(dataset_key, shard_tag, inventory or [], draft["files"])
        for entry in draft["files"]:
            filename = entry["filename"]
            if filename in existing:
                remote = temp / filename
                if not download_asset(shard_tag, repo, filename, remote):
                    raise ContractError(f"inventoried immutable asset cannot be downloaded: {filename}")
                if sha256_path(remote) != entry["sha256"]:
                    raise ContractError(f"remote immutable asset hash collision: {filename}")

        # Remote inventory and collision verification can take time. Recheck at
        # the exact first mutation boundary so a once-fresh artifact cannot age
        # out while the publisher is inspecting existing releases.
        validate_publication_freshness(draft)
        ensure_release(discovery_tag, repo, dataset_key, target_commit, asset_shard=False)
        ensure_release(shard_tag, repo, dataset_key, target_commit, asset_shard=True)

        for entry in draft["files"]:
            filename = entry["filename"]
            local = bundle_dir / filename
            remote = temp / filename
            if filename not in existing:
                run_gh(["release", "upload", shard_tag, str(local), "--repo", repo])
                if not download_asset(shard_tag, repo, filename, remote):
                    raise ContractError(f"uploaded asset cannot be downloaded: {filename}")
                if remote.stat().st_size != entry["bytes"] or sha256_path(remote) != entry["sha256"]:
                    raise ContractError(f"remote verification failed: {filename}")

        final = dict(draft)
        final["published_at"] = utc_iso(datetime.now(timezone.utc))
        validate_manifest(final, expected_dataset=dataset_key, allow_draft=False)
        validate_publication_freshness(draft)
        final_bytes = (json.dumps(final, indent=2, sort_keys=True) + "\n").encode()
        target_slots = publication_manifest_slots(active_slot, target_slot)
        for slot in target_slots:
            final_path = temp / "candidate" / slot
            final_path.parent.mkdir(exist_ok=True)
            final_path.write_bytes(final_bytes)

            # Only inactive v2 slots are replaced. On bootstrap both slots are
            # seeded before the job can succeed; thereafter the proxy keeps
            # serving the other validated slot throughout GitHub's
            # delete-then-upload --clobber implementation.
            verified_manifest = temp / f"verified-{slot}"
            upload_manifest_slot(discovery_tag, repo, slot, final_path, present_slots, verified_manifest)
        log.info(
            "Published verified generation %s to discovery slot(s) %s; legacy release assets unchanged",
            final["generation"],
            ", ".join(target_slots),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    try:
        publish(args.dataset, args.bundle, os.environ.get("GITHUB_REPOSITORY", ""))
    except Exception:  # noqa: BLE001
        log.exception("Publish failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
