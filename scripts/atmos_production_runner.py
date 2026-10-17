"""One-shot production generator admission: original producer/transport/decoder, bounded receipts.

This file is embedded verbatim in a reviewed immutable workflow. It neither
publishes nor selects a newer cycle, retries, or changes an existing source cap.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import subprocess
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'scripts'))
from atmos_contract import ATMOS_DATASETS, file_bytes, validate_atmos_manifest
from atmos_pipeline import (DownloadBudget, GFS_HOST, MAX_REQUESTS, MAX_TOTAL_BYTES,
                            TOTAL_SECONDS, generate, ifs_original_url)
from publisher_contract import DRAFT_MANIFEST_NAME, _require

SOURCE_SHA = 'b60f8796429d2c71f95790ece690cf52202e90e8'
PROVEN_FILES = {
    ".github/workflows/atmos-held-pipeline.yml": "39494b1a96f5d47d37c375ab94e85d5faa0320e36398cc292321ac555550100f",
    "scripts/atmos-first-source-review.md": "2d1c6a551571d0d39ad3099770b9baeb354314d40d688c835e6b377c814ab5c2",
    "scripts/atmos-ifs-origin-review.md": "be5e04c9469752ff49751516d1440c77e77234d0ca7713693c32842b59a12ec5",
    "scripts/atmos-ifs-replica-review.md": "ef694a1c32cb535ded3e21ed53be3b6386d44ef3bb6093254020bf582c7816bc",
    "scripts/atmos-native-tests/fixtures/ifs-msl-20261016T1200Z-f000.grib2": "599ae5df95354ad5b2070bb853762ff7c8111d6dec0acb25d9af5c705e4e696e",
    "scripts/atmos-native-tests/fixtures/ifs-msl-20261016T1200Z-f000.json": "8b7406191c0c80f7dce292725ac89d887a736832e103804f8fc1d98a5e3671c9",
    "scripts/atmos-native-tests/test_native_atmos.py": "a6cd886e4cc3479acfea555a7356fc714b000b3e3860fb966b3b8602b7b25317",
    "scripts/atmos-native-tests/test_native_first_source.py": "5034b132cdac969c623bed6ff402d14e9aa88bbe315b4e5615c607648a0df473",
    "scripts/atmos-native-tests/test_native_ifs_origin.py": "cab85664ddf5a770e5d1e18d3da1e95b7c1a71fd52c42ea751ad351ae425c2bc",
    "scripts/atmos-requirements.lock": "cfd6e6ae3863ee58bd40f780df1fbf86443790989f050e2063163f2f96df2f64",
    "scripts/atmos_contract.py": "cc53adf63d7e6608f5a95c8473601df084ebcafa0a0a635716c292b1d2d2f104",
    "scripts/atmos_pipeline.py": "559d7c68f8bbeffc9ed1ff94918e7bc587694f8fbbff0ca7632bbc5a08c33c59",
    "scripts/cmems-requirements.lock": "8cbdd76e0cba1668c4f53973407e7bfea3ce6ecc60a9c0496025d43d15bbab03",
    "scripts/prove_atmos_first_source.py": "7ef4955e6a59a923a36e461f65c1c0b2c115074ad17533b6d2bfac3e4614aa08",
    "scripts/publish_dataset.py": "3fcb7e836fc13a290425b4e3f075aa0fadaab3148ba3cbe703c9feda884f66c6",
    "scripts/publisher-tests/test_atmos.py": "e70e50830ec4c8b84308c09fe5bedc53740d6ddf4e3a3aa5212525c1085c9a6e",
    "scripts/publisher-tests/test_atmos_admission.py": "3eb36dd58b27dcf533dd441cc671c46621264cd1d7d605f841b738704fedf79f",
    "scripts/publisher-tests/test_atmos_first_source.py": "5bed15833ced1e309ce9910b7d6a88684ff1ffe76bc387eb153dd82328a98130",
    "scripts/publisher-tests/test_atmos_longitude.py": "275f3456185e42353e906d063f13207a17f6bc0974d8a3fe1896b9be25f9a05c",
    "scripts/publisher-tests/test_atmos_replica.py": "3d2e321bf45f64711abfcdaf355ab2b4e09a3a1c5521ae7f86df8c030693f40f",
    "scripts/publisher-tests/test_release_gate.py": "526b3c94da4c9bd23ffe69401eaebefed68df9a5b8d38796c27a9f884535f62e",
    "scripts/publisher-tests/test_ssh_grid.py": "421d12b4eb08a04e5dd3d48e6b79a18755d4c943a9eafad60d9b7ffb52067ea2",
    "scripts/publisher_contract.py": "3ecec84d3d6afb4e44f309425a7d7eba78160a61474de78c433ac5e6c2fca6a2",
    "scripts/ssh_grid.py": "3b229876e75930f0b37d9ebf3368a4e9731625d403f14a8178fada412090bfb4"
}
MAX_METADATA_BYTES = 1024 * 1024
CONTEXT = {
    'gfs-atmos': ('2026-10-17T00:00:00+00:00', 'aws', 29, 174),
    'ifs-pressure': ('2026-10-16T12:00:00+00:00', 'google', 41, 82),
}
PREREQUISITES = {
    'offline': {
        'source': SOURCE_SHA, 'run': 39015257829,
        'definition': 'c42f547bd01e3ca2daea2c1dc3de999a12cb2d60',
        'proofSha256': '54a94dcaf91cacf4ae82158707e5fa5d93659239b1c4808eddafa9433f282653',
        'sourceCases': 126, 'nativeCases': 7, 'skipped': 0,
        'sourceArtifact': {'id': 12094202789, 'sha256': 'ca34bb8dfe8609b3216d82ebc37363ad3763ed83894ac2109d7b7052c3309e54'},
        'nativeArtifact': {'id': 12094247725, 'sha256': 'c37443202ad326d1959f9a56ca3c83b17bde44acad1300d331776469f42a6c21'},
    },
    'googleEndpoints': {
        'source': SOURCE_SHA, 'run': 39015598953,
        'definition': 'b410c261f5cb3c5c3db3a9e59c9fa8ddacf63b3d',
        'proofSha256': 'ba7127459b5989cac1dd37a4d7a51eb75c1fb4478f6605d5d426b3ff7231f6e4',
        'reads': 4, 'frames': 2, 'ifsOffsets': [0, 240],
        'artifact': {'id': 12094637169, 'sha256': 'b3acebdc3f8e6b894be020739b0278877c0c57f4e5c167f842f8dc94724e5b08'},
    },
}


def bounded_json(path, value):
    raw = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()
    _require(len(raw) <= MAX_METADATA_BYTES, 'observer metadata exceeds1MiB')
    temporary = path.with_suffix('.pending')
    with temporary.open('wb') as target:
        target.write(raw)
    temporary.replace(path)


class ObservedBudget(DownloadBudget):
    """Call the unchanged reader once; hash only bytes it actually returned."""

    def __init__(self, dataset, run, profile, evidence, identity):
        super().__init__()
        self.started = self.clock()
        self.dataset, self.run, self.profile = dataset, run, profile
        self.evidence, self.identity = evidence, identity
        self.records = []
        self.outcome = {'state': 'running', 'completeCohort': False}
        self.allowed = set()
        stamp, hour = run.strftime('%Y%m%d'), run.strftime('%H')
        for offset in range(0, ATMOS_DATASETS[dataset]['steps'] * 6, 6):
            if dataset == 'gfs-atmos':
                base = f'https://{GFS_HOST}/gfs.{stamp}/{hour}/atmos/gfs.t{hour}z.pgrb2.0p25.f{offset:03d}'
                host, suffix = GFS_HOST, '.idx'
            else:
                base, host = ifs_original_url(run, offset, profile)
                suffix = '.index'
            self.allowed.add((base.removesuffix('.grib2') + suffix, host, False))
            self.allowed.add((base, host, True))

    def snapshot(self):
        return {
            **self.identity, 'dataset': self.dataset, 'ifsSource': self.profile,
            'runUtc': self.run.isoformat(), 'prerequisites': PREREQUISITES,
            'limits': {'requests': MAX_REQUESTS, 'bodyBytes': MAX_TOTAL_BYTES,
                       'seconds': TOTAL_SECONDS, 'metadataBytes': MAX_METADATA_BYTES},
            'requests': self.requests, 'bodyBytes': self.bytes,
            'elapsedSeconds': self.clock() - self.started,
            'records': self.records, 'outcome': self.outcome,
            'publication': False, 'activation': False, 'retryLoop': False,
            'providerFallback': False,
        }

    def persist(self):
        bounded_json(self.evidence / 'observer.json', self.snapshot())

    def read(self, url, host, *, byte_range=None):
        _require(len(self.records) < MAX_REQUESTS, 'observer request-row bound')
        _require((url, host, byte_range is not None) in self.allowed,
                 'observer URL outside fixed dataset/cycle/profile')
        _require(byte_range is None or (type(byte_range) is tuple and len(byte_range) == 2
                 and all(type(n) is int for n in byte_range)), 'observer range shape')
        row = {'url': url, 'host': host, 'range': list(byte_range) if byte_range is not None else None}
        try:
            original = super().read(url, host, byte_range=byte_range)
            row.update(state='returned', number=self.requests, bytes=len(original),
                       sha256=hashlib.sha256(original).hexdigest())
            return original
        except BaseException as error:
            # No digest/length is invented for a refused or partially read body.
            row.update(state='failed', number=self.requests,
                       errorType=type(error).__name__, error=str(error)[:512])
            raise
        finally:
            self.records.append(row)
            self.persist()


def expire(_signum, _frame):
    raise TimeoutError('original1200-second production cohort deadline')


def admit_context(env, head, clean):
    source = env.get('GITHUB_SHA', '')
    _require(re.fullmatch(r'[a-f0-9]{40}', source) is not None and source != SOURCE_SHA,
             'production requires its actual reviewed master successor')
    _require(head == source and clean, 'production checkout/source identity mismatch')
    _require(env.get('GITHUB_REPOSITORY') == 'shanestratton/thalassa-ocean-data'
             and env.get('GITHUB_REF') == 'refs/heads/master'
             and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
             and env.get('GITHUB_ACTOR') == 'shanestratton', 'production sender/ref context mismatch')
    _require(env.get('GITHUB_WORKFLOW_REF') == 'shanestratton/thalassa-ocean-data/.github/workflows/atmos-reviewed-production-once.yml@refs/heads/master'
             and env.get('GITHUB_WORKFLOW_SHA') == source, 'production workflow identity mismatch')
    _require(not env.get('SOURCE_SHA') and not env.get('CONTROL_SHA'),
             'production refuses source/control provenance overrides')
    run_id = env.get('GITHUB_RUN_ID', '')
    _require(run_id.isdigit() and int(run_id) > 0 and env.get('GITHUB_RUN_ATTEMPT') == '1',
             'production requires the first actual workflow attempt')
    return {'source': source, 'control': source, 'runId': run_id, 'runAttempt': '1',
            'workflowRef': env['GITHUB_WORKFLOW_REF'], 'proofBaseline': SOURCE_SHA}


def verify_proven_source(root):
    for name, expected in PROVEN_FILES.items():
        path = root / name
        _require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 2 * 1024 * 1024,
                 'proven source file missing/oversized')
        _require(hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                 'proven source/lock/writer changed: ' + name)


def git_identity():
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True,
                          text=True, timeout=10).stdout.strip()
    status = subprocess.run(['git', 'status', '--porcelain'], check=True, capture_output=True,
                            text=True, timeout=10).stdout
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', SOURCE_SHA, 'HEAD'],
                             capture_output=True, timeout=10)
    _require(ancestor.returncode == 0, 'proven baseline is not an ancestor of production source')
    return head, not status


def main():
    dataset = os.environ.get('ATMOS_DATASET', '')
    evidence = Path(os.environ['EVIDENCE_DIR'])
    _require(evidence.is_dir() and not evidence.is_symlink(), 'missing/faulty evidence directory')
    budget = None
    outcome = {'state': 'preflight-failed', 'completeCohort': False}
    identity = {'source': os.environ.get('GITHUB_SHA', ''),
                'runId': os.environ.get('GITHUB_RUN_ID', ''), 'runAttempt': os.environ.get('GITHUB_RUN_ATTEMPT', '')}
    try:
        identity = admit_context(os.environ, *git_identity())
        verify_proven_source(Path.cwd())
        _require(dataset in CONTEXT, 'unreviewed production dataset')
        cycle, profile, steps, expected_reads = CONTEXT[dataset]
        _require(os.environ.get('ATMOS_RUN_UTC') == cycle
                 and os.environ.get('IFS_SOURCE') == profile, 'unreviewed production cycle or source profile')
        run = datetime.fromisoformat(cycle)
        _require(datetime.now(timezone.utc) < datetime(2026, 10, 17, 12, tzinfo=timezone.utc),
                 'fixed production cycles expired; no newer run selected')
        _require(resource.getrlimit(resource.RLIMIT_AS) == (2147483648, 2147483648),
                 'production requires exact2GiB native address-space ceiling')
        output = evidence / 'output'
        _require(not output.exists() and not output.is_symlink(), 'production output must be fresh')
        budget = ObservedBudget(dataset, run, profile, evidence, identity)
        budget.persist()
        signal.signal(signal.SIGALRM, expire)
        signal.alarm(TOTAL_SECONDS)
        # Original generator, reader, decoder/science and ACTUAL CI provenance.
        # This wrapper never writes GITHUB_SHA/RUN_ID/ATTEMPT or publishes.
        draft = generate(dataset, run, output, budget=budget, ifs_source=profile)
        manifest = json.loads(draft.read_text())
        validate_atmos_manifest(manifest, dataset, allow_draft=True, bundle_dir=draft.parent)
        _require(draft.name == DRAFT_MANIFEST_NAME and manifest['published_at'] is None,
                 'production generator did not return an unpublished draft')
        _require(manifest['producer'] == {'commit': identity['source'],
                 'run_id': int(identity['runId']), 'run_attempt': 1}, 'production producer identity mismatch')
        _require(len(manifest['files']) == steps and budget.requests == expected_reads
                 and len(budget.records) == expected_reads
                 and all(row['state'] == 'returned' for row in budget.records), 'production cohort incomplete')
        expected_paths = {f'step-{i:03d}.bin' for i in range(steps)}
        expected_paths.update('bundle/' + row['filename'] for row in manifest['files'])
        expected_paths.add('bundle/' + DRAFT_MANIFEST_NAME)
        actual = list(output.rglob('*'))
        files = [p for p in actual if p.is_file()]
        _require(not any(p.is_symlink() for p in actual)
                 and {p.relative_to(output).as_posix() for p in files} == expected_paths,
                 'unexpected/partial production output inventory')
        expected_payload = file_bytes(dataset) * steps * 2
        _require(sum(p.stat().st_size for p in files) == expected_payload + draft.stat().st_size,
                 'production directory byte mismatch')
        budget.check()
        _require(budget.clock() - budget.started <= TOTAL_SECONDS, 'production deadline exceeded')
        outcome = {'state': 'verified-complete-draft', 'completeCohort': True,
                   'steps': steps, 'expectedRequests': expected_reads,
                   'draft': str(draft.relative_to(evidence)),
                   'draftSha256': hashlib.sha256(draft.read_bytes()).hexdigest(),
                   'generation': manifest['generation'], 'outputFiles': len(files),
                   'outputBytes': expected_payload + draft.stat().st_size, 'publishedAt': None}
        print(json.dumps({'dataset': dataset, **outcome, 'requests': budget.requests,
                          'bodyBytes': budget.bytes, 'publication': False}))
    except BaseException as error:
        outcome = {'state': 'failed', 'completeCohort': False,
                   'errorType': type(error).__name__, 'error': str(error)[:512]}
        raise
    finally:
        signal.alarm(0)
        if budget is not None:
            budget.outcome = outcome
            budget.persist()
        else:
            bounded_json(evidence / 'observer.json', {**identity, 'dataset': dataset,
                         'outcome': outcome, 'prerequisites': PREREQUISITES,
                         'publication': False, 'activation': False})


if __name__ == '__main__':
    main()
