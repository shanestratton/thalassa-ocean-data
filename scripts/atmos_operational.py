"""Held cycle admission; original science, reader, observer and writer stay intact.

The only remote reads here are bounded GitHub control metadata. An acknowledged
immutable Actions artifact consumes a cycle before the original provider reader
can run. Failed/unknown attempts are never retried, refunded or replaced.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atmos_contract import ATMOS_DATASETS, file_bytes, validate_atmos_manifest
from atmos_pipeline import NoRedirect, generate
from atmos_production_runner import (ObservedBudget, PREREQUISITES, SOURCE_SHA,
                                     TOTAL_SECONDS, bounded_json, expire,
                                     git_identity, verify_proven_source)
from publisher_contract import (_require, parse_utc, validate_publish_context,
                                validate_publication_freshness)

REPOSITORY = 'shanestratton/thalassa-ocean-data'
WORKFLOW = '.github/workflows/atmos-operational.yml'
POLICY = {
    'gfs-atmos': {'lag': 4, 'period': 6, 'profile': 'aws', 'reads': 174,
                  'cron': '47 4,10,16,22 * * *', 'first': '2026-10-17T00:00:00Z'},
    'ifs-pressure': {'lag': 8, 'period': 12, 'profile': 'google', 'reads': 82,
                     'cron': '47 8,20 * * *', 'first': '2026-10-16T12:00:00Z'},
}
API_BYTES = 64 * 1024
API_SECONDS = 20
MAX_QUEUE_SECONDS = 90 * 60
PRODUCTION_PROOF = 'b5b022a9b8baf962e4fe9d469ba1650fb650d511cba233d10aece81e92ebdbce'


def cycle_at(dataset, clock):
    _require(dataset in POLICY and isinstance(clock, datetime)
             and clock.tzinfo is not None, 'unknown dataset or unqualified UTC clock')
    policy = POLICY[dataset]
    eligible = clock.astimezone(timezone.utc) - timedelta(hours=policy['lag'])
    return eligible.replace(hour=(eligible.hour // policy['period']) * policy['period'],
                            minute=0, second=0, microsecond=0)


def admit(env, head, clean, created_at, now):
    source = env.get('GITHUB_SHA', '')
    _require(re.fullmatch(r'[0-9a-f]{40}', source) is not None
             and source != SOURCE_SHA and head == source and clean,
             'operational source/checkout identity mismatch')
    _require(env.get('GITHUB_REPOSITORY') == REPOSITORY
             and env.get('GITHUB_REF') == 'refs/heads/master'
             and env.get('GITHUB_ACTOR') == 'shanestratton', 'operational sender/ref mismatch')
    _require(env.get('GITHUB_WORKFLOW_REF') == REPOSITORY + '/' + WORKFLOW + '@refs/heads/master'
             and env.get('GITHUB_WORKFLOW_SHA') == source, 'operational workflow identity mismatch')
    _require(not any(env.get(key) for key in ('SOURCE_SHA', 'CONTROL_SHA', 'ATMOS_RUN_UTC', 'IFS_SOURCE')),
             'operational source/cycle/profile overrides refused')
    _require(env.get('GITHUB_RUN_ID', '').isdigit() and int(env['GITHUB_RUN_ID']) > 0
             and env.get('GITHUB_RUN_ATTEMPT') == '1', 'first actual run attempt required')
    event = env.get('GITHUB_EVENT_NAME')
    if event == 'schedule':
        matches = [name for name, rule in POLICY.items() if rule['cron'] == env.get('EVENT_SCHEDULE')]
        _require(env.get('SCHEDULE_ENABLED') == 'true' and len(matches) == 1,
                 'schedule is held or unknown')
        dataset = matches[0]
    else:
        _require(event == 'workflow_dispatch' and env.get('ALLOW_DOWNLOADS') == 'true'
                 and env.get('ALLOW_PUBLISH') == 'true', 'manual admission requires both explicit gates')
        dataset = env.get('REQUEST_DATASET')
        _require(dataset in POLICY, 'unknown requested dataset')
    created = parse_utc(created_at, 'GitHub run creation time')
    _require(isinstance(now, datetime) and now.tzinfo is not None,
             'unqualified operational clock')
    age = (now.astimezone(timezone.utc) - created).total_seconds()
    _require(0 <= age <= MAX_QUEUE_SECONDS, 'future or overly delayed workflow run')
    cycle = cycle_at(dataset, created)
    _require(cycle == cycle_at(dataset, now), 'eligible cycle changed while queued; no older fallback')
    _require(cycle > parse_utc(POLICY[dataset]['first'], 'independently published first cycle'),
             'initial production cycle is already consumed')
    name = 'atmos-attempt-' + dataset + '-' + cycle.strftime('%Y%m%dT%H%M%SZ')
    return {'source': source, 'control': source, 'runId': env['GITHUB_RUN_ID'], 'runAttempt': '1',
            'workflowRef': env['GITHUB_WORKFLOW_REF'], 'event': event, 'createdAt': created_at,
            'dataset': dataset, 'runUtc': cycle.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'profile': POLICY[dataset]['profile'], 'attemptName': name,
            'proofBaseline': SOURCE_SHA, 'productionProofSha256': PRODUCTION_PROOF}


def api(path, token, opener=None):
    _require(path.startswith('/repos/' + REPOSITORY + '/actions/')
             and len(path) <= 512 and isinstance(token, str) and 0 < len(token) <= 4096,
             'GitHub control request outside fixed namespace')
    request = urllib.request.Request('https://api.github.com' + path,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'thalassa-atmos-admission'})
    reader = opener or urllib.request.build_opener(NoRedirect())
    with reader.open(request, timeout=API_SECONDS) as response:
        _require(response.status == 200, 'GitHub control metadata unavailable')
        raw = response.read(API_BYTES + 1)
    _require(len(raw) <= API_BYTES, 'GitHub control metadata exceeds64KiB')
    value = json.loads(raw.decode('utf-8', errors='strict'))
    _require(isinstance(value, dict), 'GitHub control metadata shape')
    return value


def context(env, now, read=api):
    head, clean = git_identity()
    verify_proven_source(Path.cwd())
    # Refuse untrusted sender/ref/override before any metadata or provider I/O.
    admit(env, head, clean, now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), now)
    run_id = env.get('GITHUB_RUN_ID', '')
    _require(run_id.isdigit() and int(run_id) > 0, 'missing actual run id')
    record = read('/repos/' + REPOSITORY + '/actions/runs/' + run_id, env.get('GH_TOKEN', ''))
    _require(type(record.get('id')) is int and str(record['id']) == run_id
             and record.get('head_sha') == env.get('GITHUB_SHA') and record.get('head_branch') == 'master'
             and record.get('event') == env.get('GITHUB_EVENT_NAME')
             and type(record.get('run_attempt')) is int and record['run_attempt'] == 1
             and record.get('path') == WORKFLOW
             and record.get('actor', {}).get('login') == 'shanestratton'
             and record.get('repository', {}).get('full_name') == REPOSITORY,
             'actual GitHub run metadata disagrees with execution context')
    return admit(env, head, clean, record.get('created_at'), now)


def attempt_inventory(identity, env, read=api):
    path = '/repos/' + REPOSITORY + '/actions/artifacts?name=' + urllib.parse.quote(identity['attemptName']) + '&per_page=2'
    value = read(path, env.get('GH_TOKEN', ''))
    _require(type(value.get('total_count')) is int and isinstance(value.get('artifacts'), list)
             and len(value['artifacts']) <= 2, 'attempt inventory malformed or ambiguous')
    return value


def prepare(identity, env, evidence, read=api):
    inventory = attempt_inventory(identity, env, read)
    _require(inventory['total_count'] == 0 and inventory['artifacts'] == [],
             'cycle has a prior durable attempt; no retry or refund')
    marker = evidence / 'attempt'
    marker.mkdir()  # Fresh directory, no replacement or symlink adoption.
    bounded_json(marker / 'attempt.json', {**identity, 'state': 'consumed-before-provider-read',
                 'providerRequests': 0, 'retryAllowed': False, 'prerequisites': PREREQUISITES})
    return marker


def acknowledged(identity, env, read=api):
    marker_id = env.get('ATTEMPT_ARTIFACT_ID', '')
    _require(marker_id.isdigit() and int(marker_id) > 0, 'durable attempt upload not acknowledged')
    value = attempt_inventory(identity, env, read)
    _require(value['total_count'] == 1 and len(value['artifacts']) == 1,
             'durable attempt inventory changed or is ambiguous')
    row = value['artifacts'][0]
    _require(type(row.get('id')) is int and row['id'] == int(marker_id)
             and row.get('name') == identity['attemptName'] and row.get('expired') is False
             and type(row.get('size_in_bytes')) is int and 0 < row['size_in_bytes'] <= API_BYTES
             and re.fullmatch(r'sha256:[a-f0-9]{64}', row.get('digest', '')) is not None
             and row.get('workflow_run', {}).get('id') == int(identity['runId'])
             and row['workflow_run'].get('head_sha') == identity['source']
             and row['workflow_run'].get('head_branch') == 'master',
             'durable attempt does not belong to this exact cycle/run/source')
    return {'artifactId': row['id'], 'name': row['name'], 'digest': row['digest']}


def generate_cohort(identity, marker, evidence):
    import resource
    _require(resource.getrlimit(resource.RLIMIT_AS) == (2147483648, 2147483648),
             'original exact2GiB native address-space ceiling required')
    dataset, profile = identity['dataset'], identity['profile']
    run = parse_utc(identity['runUtc'], 'eligible original cycle')
    output = evidence / 'output'
    _require(not output.exists() and not output.is_symlink(), 'fresh generation output required')
    budget = ObservedBudget(dataset, run, profile, evidence, {**identity, 'attempt': marker})
    outcome = {'state': 'failed', 'completeCohort': False}
    signal.signal(signal.SIGALRM, expire)
    signal.alarm(TOTAL_SECONDS)
    try:
        budget.persist()
        draft = generate(dataset, run, output, budget=budget, ifs_source=profile)
        manifest = json.loads(draft.read_text())
        validate_atmos_manifest(manifest, dataset, allow_draft=True, bundle_dir=draft.parent)
        validate_publish_context(manifest, REPOSITORY)
        validate_publication_freshness(manifest)
        steps = ATMOS_DATASETS[dataset]['steps']
        _require(manifest['published_at'] is None and manifest['data_start'] == identity['runUtc']
                 and manifest['producer']['run_attempt'] == 1 and len(manifest['files']) == steps
                 and budget.requests == POLICY[dataset]['reads']
                 and len(budget.records) == budget.requests
                 and all(row['state'] == 'returned' for row in budget.records), 'incomplete original cohort')
        expected = {f'step-{i:03d}.bin' for i in range(steps)}
        expected.update('bundle/' + row['filename'] for row in manifest['files'])
        expected.add('bundle/manifest.draft.json')
        entries = list(output.rglob('*'))
        files = [path for path in entries if path.is_file()]
        _require(not any(path.is_symlink() for path in entries)
                 and {path.relative_to(output).as_posix() for path in files} == expected
                 and sum(path.stat().st_size for path in files) == file_bytes(dataset) * steps * 2 + draft.stat().st_size,
                 'unexpected or partial output inventory')
        budget.check()
        outcome = {'state': 'verified-complete-draft', 'completeCohort': True, 'steps': steps,
                   'draftSha256': hashlib.sha256(draft.read_bytes()).hexdigest(), 'publishedAt': None}
    except BaseException as error:
        outcome.update(errorType=type(error).__name__, error=str(error)[:512])
        raise
    finally:
        signal.alarm(0)
        budget.outcome = outcome
        budget.persist()


def main():
    env = os.environ
    _require(len(sys.argv) == 2 and sys.argv[1] in {'plan', 'prepare', 'generate', 'publish-check'},
             'unknown operational phase')
    phase = sys.argv[1]
    identity = context(env, datetime.now(timezone.utc))
    if phase == 'plan':
        with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write('dataset=' + identity['dataset'] + '\n')
        return
    evidence = Path(env['EVIDENCE_DIR'])
    _require(evidence.is_dir() and not evidence.is_symlink(), 'missing fresh operational evidence directory')
    bounded_json(evidence / 'admission.json', identity)
    if phase == 'prepare':
        prepare(identity, env, evidence)
        with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write('attempt-name=' + identity['attemptName'] + '\n')
        return
    marker = acknowledged(identity, env)
    if phase == 'generate':
        generate_cohort(identity, marker, evidence)
    else:
        bundle = Path(env['BUNDLE_DIR'])
        _require(bundle.is_dir() and not bundle.is_symlink(), 'missing same-run sealed bundle')
        manifest = json.loads((bundle / 'manifest.draft.json').read_text())
        validate_publish_context(manifest, REPOSITORY)
        _require(manifest.get('data_start') == identity['runUtc']
                 and manifest.get('dataset', {}).get('key') == identity['dataset']
                 and manifest.get('producer', {}).get('run_attempt') == 1,
                 'sealed cohort does not match actual eligible cycle/run')
        validate_publication_freshness(manifest)
        bounded_json(evidence / 'publish-admission.json', {**identity, 'attempt': marker})


if __name__ == '__main__':
    main()
