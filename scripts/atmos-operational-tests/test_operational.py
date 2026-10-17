"""Offline cycle, identity, durability and held-workflow refusal tests."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
if sys.platform == 'win32':
    sys.modules['resource'] = types.SimpleNamespace(RLIMIT_AS=9, getrlimit=lambda key: (2147483648, 2147483648))
import atmos_operational as op
from publisher_contract import ContractError

SHA = 'a' * 40
NOW = datetime(2026, 10, 17, 10, 47, tzinfo=timezone.utc)


def environment():
    return {'GITHUB_SHA': SHA, 'GITHUB_REPOSITORY': op.REPOSITORY, 'GITHUB_REF': 'refs/heads/master',
            'GITHUB_ACTOR': 'shanestratton', 'GITHUB_WORKFLOW_SHA': SHA,
            'GITHUB_WORKFLOW_REF': op.REPOSITORY + '/' + op.WORKFLOW + '@refs/heads/master',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
            'REQUEST_DATASET': 'gfs-atmos', 'ALLOW_DOWNLOADS': 'true', 'ALLOW_PUBLISH': 'true',
            'GH_TOKEN': 'offline-fixture-not-a-token'}


def identity(env=None, now=NOW):
    return op.admit(env or environment(), SHA, True, now.strftime('%Y-%m-%dT%H:%M:%SZ'), now)


def run_metadata():
    return {'id': 123, 'head_sha': SHA, 'head_branch': 'master', 'event': 'workflow_dispatch',
            'run_attempt': 1, 'path': op.WORKFLOW, 'actor': {'login': 'shanestratton'},
            'repository': {'full_name': op.REPOSITORY}, 'created_at': '2026-10-17T10:47:00Z'}


def marker():
    return {'id': 456, 'name': identity()['attemptName'], 'expired': False, 'size_in_bytes': 1234,
            'digest': 'sha256:' + 'b' * 64,
            'workflow_run': {'id': 123, 'head_sha': SHA, 'head_branch': 'master'}}


class CycleTests(unittest.TestCase):
    def test_gfs_four_hour_boundary_and_previous_day_are_exact(self):
        self.assertEqual(op.cycle_at('gfs-atmos', NOW).isoformat(), '2026-10-17T06:00:00+00:00')
        at = NOW.replace(hour=4, minute=0)
        self.assertEqual(op.cycle_at('gfs-atmos', at).hour, 0)
        self.assertEqual(op.cycle_at('gfs-atmos', at - timedelta(microseconds=1)).isoformat(), '2026-10-16T18:00:00+00:00')

    def test_ifs_eight_hour_boundary_uses_only_full_horizon_00_or_12(self):
        at = NOW.replace(hour=8, minute=0)
        self.assertEqual(op.cycle_at('ifs-pressure', at).hour, 0)
        self.assertEqual(op.cycle_at('ifs-pressure', at - timedelta(seconds=1)).isoformat(), '2026-10-16T12:00:00+00:00')
        self.assertEqual(op.cycle_at('ifs-pressure', NOW.replace(hour=20)).hour, 12)

    def test_entire_day_has_only_four_gfs_and_two_ifs_eligible_cycles(self):
        for dataset, maximum in [('gfs-atmos', 4), ('ifs-pressure', 2)]:
            cycles = {op.cycle_at(dataset, NOW.replace(hour=0, minute=0) + timedelta(minutes=i)) for i in range(1440)}
            # A rolling UTC day straddles one lag window; exactly period count +1
            # includes yesterday's final cycle. Every contiguous 24h cycle span
            # still has only four/two unique original model timestamps.
            self.assertEqual(len(cycles), maximum + 1)
            hours = sorted(cycles)
            self.assertTrue(all((b - a).total_seconds() == 86400 / maximum for a, b in zip(hours, hours[1:])))

    def test_unknown_or_naive_clock_refuses(self):
        for dataset, clock in [('other', NOW), ('gfs-atmos', NOW.replace(tzinfo=None))]:
            with self.assertRaises(ContractError): op.cycle_at(dataset, clock)

    def test_every_exact_cron_selects_its_own_fixed_profile(self):
        for dataset, expected in [('gfs-atmos', 'aws'), ('ifs-pressure', 'google')]:
            env = {**environment(), 'GITHUB_EVENT_NAME': 'schedule', 'SCHEDULE_ENABLED': 'true',
                   'EVENT_SCHEDULE': op.POLICY[dataset]['cron'], 'REQUEST_DATASET': 'ignored'}
            result = identity(env)
            self.assertEqual((result['dataset'], result['profile']), (dataset, expected))

    def test_schedule_requires_exact_enabled_value_and_literal_cron(self):
        for change in ({'SCHEDULE_ENABLED': ''}, {'SCHEDULE_ENABLED': 'True'}, {'EVENT_SCHEDULE': '*/5 * * * *'}):
            env = {**environment(), 'GITHUB_EVENT_NAME': 'schedule', 'SCHEDULE_ENABLED': 'true',
                   'EVENT_SCHEDULE': op.POLICY['gfs-atmos']['cron'], **change}
            with self.assertRaises(ContractError): identity(env)

    def test_manual_requires_both_gates_and_known_dataset(self):
        for change in ({'ALLOW_DOWNLOADS': 'false'}, {'ALLOW_PUBLISH': ''}, {'REQUEST_DATASET': 'other'}):
            with self.assertRaises(ContractError): identity({**environment(), **change})

    def test_first_independently_published_cycles_are_consumed(self):
        for dataset, clock in [('gfs-atmos', NOW.replace(hour=4)), ('ifs-pressure', NOW.replace(hour=7))]:
            with self.assertRaisesRegex(ContractError, 'already consumed'):
                identity({**environment(), 'REQUEST_DATASET': dataset}, clock)

    def test_queued_cycle_change_is_refusal_not_old_source_fallback(self):
        created = NOW.replace(hour=9, minute=59)
        with self.assertRaisesRegex(ContractError, 'cycle changed'):
            op.admit(environment(), SHA, True, created.strftime('%Y-%m-%dT%H:%M:%SZ'), NOW)

    def test_future_and_overly_delayed_creation_clock_refuse(self):
        for created in (NOW + timedelta(seconds=1), NOW - timedelta(seconds=5401)):
            with self.assertRaises(ContractError):
                op.admit(environment(), SHA, True, created.strftime('%Y-%m-%dT%H:%M:%SZ'), NOW)

    def test_bad_sender_ref_event_workflow_source_attempt_and_dirty_checkout_refuse(self):
        for key, value in [('GITHUB_ACTOR', 'other'), ('GITHUB_REF', 'refs/heads/other'),
                           ('GITHUB_REPOSITORY', 'other/repo'), ('GITHUB_EVENT_NAME', 'push'),
                           ('GITHUB_WORKFLOW_REF', op.REPOSITORY + '/other@refs/heads/master'),
                           ('GITHUB_WORKFLOW_SHA', 'b' * 40), ('GITHUB_RUN_ATTEMPT', '2'), ('GITHUB_RUN_ID', '0')]:
            with self.subTest(key=key), self.assertRaises(ContractError): identity({**environment(), key: value})
        for head, clean in [(SHA, False), ('b' * 40, True)]:
            with self.assertRaises(ContractError): op.admit(environment(), head, clean, '2026-10-17T10:47:00Z', NOW)

    def test_source_cycle_and_profile_overrides_refuse(self):
        for key in ('SOURCE_SHA', 'CONTROL_SHA', 'ATMOS_RUN_UTC', 'IFS_SOURCE'):
            with self.subTest(key=key), self.assertRaises(ContractError): identity({**environment(), key: 'override'})


class ControlMetadataTests(unittest.TestCase):
    def context(self, env=None, record=None):
        with patch.object(op, 'git_identity', return_value=(SHA, True)), patch.object(op, 'verify_proven_source'):
            return op.context(env or environment(), NOW, lambda *args: record or run_metadata())

    def test_actual_github_run_record_and_context_agree(self):
        self.assertEqual(self.context(), identity())

    def test_wrong_actual_run_source_attempt_path_actor_or_repo_refuses(self):
        for key, value in [('id', 124), ('head_sha', 'b' * 40), ('head_branch', 'other'),
                           ('run_attempt', True), ('path', 'other.yml'), ('event', 'push'),
                           ('actor', {'login': 'other'}), ('repository', {'full_name': 'other/repo'})]:
            with self.subTest(key=key), self.assertRaises(ContractError): self.context(record={**run_metadata(), key: value})

    def test_invalid_local_context_refuses_before_any_control_network(self):
        with patch.object(op, 'git_identity', return_value=(SHA, True)), patch.object(op, 'verify_proven_source'), patch.object(op, 'api') as read:
            for env in ({**environment(), 'GITHUB_ACTOR': 'other'}, {**environment(), 'IFS_SOURCE': 'aws'}):
                with self.assertRaises(ContractError): op.context(env, NOW, read)
            read.assert_not_called()

    def test_api_uses_only_fixed_github_origin_and_bounded_single_response(self):
        class Reply(io.BytesIO): status = 200
        class Reader:
            def __init__(self): self.calls = []
            def open(self, request, timeout):
                self.calls.append((request, timeout)); return Reply(b'{"id":123}')
        reader = Reader()
        result = op.api('/repos/' + op.REPOSITORY + '/actions/runs/123', 'private-fixture', reader)
        self.assertEqual(result, {'id': 123}); self.assertEqual(len(reader.calls), 1)
        request, timeout = reader.calls[0]
        self.assertEqual(timeout, 20)
        self.assertEqual(request.full_url, 'https://api.github.com/repos/' + op.REPOSITORY + '/actions/runs/123')
        self.assertEqual(request.get_header('Authorization'), 'Bearer private-fixture')

    def test_control_oversize_invalid_utf8_json_and_array_refuse_without_retry(self):
        for body in (b'x' * (op.API_BYTES + 1), b'\xff', b'{bad', b'[]'):
            class Reply(io.BytesIO): status = 200
            reader = types.SimpleNamespace(open=lambda *args, **kwargs: Reply(body))
            with self.assertRaises((ContractError, ValueError, UnicodeError)):
                op.api('/repos/' + op.REPOSITORY + '/actions/runs/123', 'fixture', reader)

    def test_foreign_endpoint_or_missing_token_refuses_before_open(self):
        reader = types.SimpleNamespace(open=lambda *args, **kwargs: self.fail('no request allowed'))
        for path, token in [('/repos/other/repo/actions/runs/123', 'fixture'), ('https://provider/', 'fixture'),
                            ('/repos/' + op.REPOSITORY + '/actions/runs/123', '')]:
            with self.assertRaises(ContractError): op.api(path, token, reader)

    def test_transport_failure_is_one_request_with_no_fallback(self):
        calls = []
        def refused(*args, **kwargs): calls.append(True); raise TimeoutError('offline')
        with self.assertRaises(TimeoutError):
            op.api('/repos/' + op.REPOSITORY + '/actions/runs/123', 'fixture', types.SimpleNamespace(open=refused))
        self.assertEqual(len(calls), 1)


class DurableAttemptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_empty_inventory_prepares_only_a_zero_request_marker(self):
        path = op.prepare(identity(), environment(), self.root, lambda *args: {'total_count': 0, 'artifacts': []})
        value = json.loads((path / 'attempt.json').read_text())
        self.assertEqual(value['providerRequests'], 0); self.assertFalse(value['retryAllowed'])
        self.assertEqual(value['runUtc'], '2026-10-17T06:00:00Z')
        self.assertEqual(value['productionProofSha256'], op.PRODUCTION_PROOF)
        self.assertFalse((self.root / 'output').exists())

    def test_existing_failed_expired_or_unknown_attempt_never_refunds(self):
        for row in (marker(), {**marker(), 'expired': True}, {'id': 999}):
            with self.assertRaisesRegex(ContractError, 'prior durable attempt'):
                op.prepare(identity(), environment(), self.root, lambda *args: {'total_count': 1, 'artifacts': [row]})
        self.assertFalse((self.root / 'attempt').exists())

    def test_malformed_or_ambiguous_inventory_refuses_before_marker(self):
        for record in ({'total_count': True, 'artifacts': []}, {'total_count': 3, 'artifacts': [{}, {}, {}]},
                       {'total_count': 0, 'artifacts': [{}]}, {'artifacts': []}):
            with self.assertRaises(ContractError): op.prepare(identity(), environment(), self.root, lambda *args: record)
        self.assertFalse((self.root / 'attempt').exists())

    def test_control_inventory_failure_does_not_prepare_or_read_provider(self):
        def failed(*args): raise OSError('metadata unavailable')
        with self.assertRaises(OSError): op.prepare(identity(), environment(), self.root, failed)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_preexisting_local_marker_directory_is_never_replaced(self):
        (self.root / 'attempt').mkdir(); (self.root / 'attempt' / 'old').write_text('retain')
        with self.assertRaises(FileExistsError):
            op.prepare(identity(), environment(), self.root, lambda *args: {'total_count': 0, 'artifacts': []})
        self.assertEqual((self.root / 'attempt' / 'old').read_text(), 'retain')

    def test_acknowledged_exact_artifact_belongs_to_actual_cycle_run_source(self):
        result = op.acknowledged(identity(), {**environment(), 'ATTEMPT_ARTIFACT_ID': '456'},
                                 lambda *args: {'total_count': 1, 'artifacts': [marker()]})
        self.assertEqual(result['artifactId'], 456)
        self.assertEqual(result['digest'], 'sha256:' + 'b' * 64)

    def test_missing_upload_acknowledgement_refuses_before_metadata_or_provider(self):
        def read(*args): self.fail('no metadata expected')
        for value in ('', '0', '-1', 'false'):
            with self.assertRaises(ContractError): op.acknowledged(identity(), {**environment(), 'ATTEMPT_ARTIFACT_ID': value}, read)

    def test_acknowledged_artifact_drift_refuses(self):
        for key, value in [('id', 457), ('name', 'other'), ('expired', True), ('size_in_bytes', op.API_BYTES + 1),
                           ('size_in_bytes', 0), ('digest', 'bad'), ('workflow_run', {'id': 124, 'head_sha': SHA})]:
            row = {**marker(), key: value}
            with self.subTest(key=key), self.assertRaises(ContractError):
                op.acknowledged(identity(), {**environment(), 'ATTEMPT_ARTIFACT_ID': '456'},
                                lambda *args: {'total_count': 1, 'artifacts': [row]})

    def test_acknowledged_wrong_source_branch_duplicate_or_missing_inventory_refuse(self):
        for record in ({'total_count': 0, 'artifacts': []}, {'total_count': 2, 'artifacts': [marker(), marker()]},
                       {'total_count': 1, 'artifacts': [{**marker(), 'workflow_run': {'id': 123, 'head_sha': 'c' * 40, 'head_branch': 'master'}}]},
                       {'total_count': 1, 'artifacts': [{**marker(), 'workflow_run': {'id': 123, 'head_sha': SHA, 'head_branch': 'other'}}]}):
            with self.assertRaises(ContractError):
                op.acknowledged(identity(), {**environment(), 'ATTEMPT_ARTIFACT_ID': '456'}, lambda *args: record)


class WorkflowTests(unittest.TestCase):
    def test_held_scheduler_has_only_reviewed_crons_and_manual_false_gates(self):
        source = (ROOT / op.WORKFLOW).read_text()
        self.assertEqual(source.count('- cron:'), 2)
        self.assertTrue(all("- cron: '" + p['cron'] + "'" in source for p in op.POLICY.values()))
        self.assertEqual(source.count('default: false'), 2)
        self.assertEqual(source.count("vars.THALASSA_ATMOS_SCHEDULE_ENABLED == 'true'"), 4)
        self.assertNotRegex(source, r'(?m)^\s*(?:push|pull_request|workflow_run):')
        self.assertNotIn('secrets.', source)
        self.assertIn('group: publisher-atmos-${{', source)
        self.assertIn('cancel-in-progress: false', source)

    def test_upload_ack_is_before_generation_and_original_writer_is_isolated(self):
        source = (ROOT / op.WORKFLOW).read_text()
        self.assertLess(source.index('scripts/atmos_operational.py prepare'), source.index('id: attempt'))
        self.assertLess(source.index('id: attempt'), source.index('scripts/atmos_operational.py generate'))
        self.assertIn('ATTEMPT_ARTIFACT_ID: ${{ steps.attempt.outputs.artifact-id }}', source)
        self.assertIn('ATTEMPT_ARTIFACT_ID: ${{ needs.generate.outputs.attempt-id }}', source)
        self.assertIn('retention-days: 90', source)
        self.assertEqual(source.count('contents: write'), 1)
        publish = source[source.index('\n  publish:\n'):]
        self.assertNotIn('pip install', publish)
        self.assertIn('needs: [plan, generate]', publish)
        self.assertIn('scripts/publish_dataset.py --dataset "$ATMOS_DATASET" --bundle "$BUNDLE_DIR"', publish)
        self.assertLess(publish.index('publish-check'), publish.index('python -B scripts/publish_dataset.py'))

    def test_original_caps_separate_hash_locks_and_zero_skip_proofs_remain(self):
        source = (ROOT / op.WORKFLOW).read_text()
        for value in ('expected: 126', 'expected: 7', '==23', 'scripts/cmems-requirements.lock',
                      'scripts/atmos-requirements.lock', '--require-hashes --only-binary=:all:',
                      'ulimit -v 2097152', '1200s', 'not result.skipped', 'not operational_result.skipped',
                      'statuses=("${PIPESTATUS[@]}")', 'if: always()'):
            self.assertIn(value, source)
        self.assertEqual((op.ObservedBudget.__bases__[0].__name__, op.TOTAL_SECONDS), ('DownloadBudget', 1200))

    def test_exact_writer_allowlist_retains_impostor_and_extra_permission_refusals(self):
        spec = importlib.util.spec_from_file_location('_operational_gate', ROOT / 'scripts/publisher-tests/test_release_gate.py')
        gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for original in (ROOT / '.github/workflows').glob('*.yml'):
                (directory / original.name).write_text(original.read_text())
            with patch.object(gate, 'WORKFLOWS_DIR', directory):
                test = gate.PublisherReleaseGateTests('test_every_workflow_is_least_privilege_pinned_and_on_one_runner')
                test.test_every_workflow_is_least_privilege_pinned_and_on_one_runner()
                reviewed = directory / 'atmos-operational.yml'; impostor = directory / 'unreviewed-operational.yml'
                reviewed.rename(impostor)
                with self.assertRaises(AssertionError): test.test_every_workflow_is_least_privilege_pinned_and_on_one_runner()
                impostor.rename(reviewed)
                reviewed.write_text(reviewed.read_text().replace('contents: write', 'contents: write\n      issues: write'))
                with self.assertRaises(AssertionError): test.test_every_workflow_is_least_privilege_pinned_and_on_one_runner()


class GenerationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def invoke(self, producer, cap=(2147483648, 2147483648)):
        signals = types.SimpleNamespace(SIGALRM=14, signal=lambda *args: None, alarm=lambda *args: None)
        with patch('resource.getrlimit', return_value=cap), patch.object(op, 'signal', signals), patch.object(op, 'generate', producer):
            op.generate_cohort(identity(), {'artifactId': 456}, self.root)

    def test_wrong_native_ceiling_refuses_before_generation(self):
        for cap in ((2147483648, -1), (-1, -1), (3221225472, 3221225472)):
            with patch.object(op, 'generate') as producer, self.assertRaises(ContractError): self.invoke(producer, cap)
            producer.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_partial_native_failure_preserves_output_and_failed_observer(self):
        def producer(dataset, run, output, **kwargs):
            output.mkdir(); (output / 'step-000.bin').write_bytes(b'partial')
            raise RuntimeError('original native failure')
        with self.assertRaisesRegex(RuntimeError, 'original native failure'): self.invoke(producer)
        self.assertEqual((self.root / 'output/step-000.bin').read_bytes(), b'partial')
        receipt = json.loads((self.root / 'observer.json').read_text())
        self.assertFalse(receipt['outcome']['completeCohort'])
        self.assertEqual(receipt['attempt']['artifactId'], 456)
        self.assertFalse(receipt['publication']); self.assertFalse(receipt['providerFallback'])

    def test_reentered_output_is_refused_and_preserved_before_producer(self):
        (self.root / 'output').mkdir(); (self.root / 'output/partial').write_text('retain')
        with patch.object(op, 'generate') as producer, self.assertRaises(ContractError): self.invoke(producer)
        producer.assert_not_called(); self.assertEqual((self.root / 'output/partial').read_text(), 'retain')

    def test_original_generator_receives_exact_selected_cycle_and_profile_without_env_override(self):
        calls = []
        def producer(dataset, run, output, **kwargs):
            calls.append((dataset, run, output, kwargs)); raise RuntimeError('stop before provider')
        before = dict(os.environ)
        with self.assertRaises(RuntimeError): self.invoke(producer)
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(calls[0][:2], ('gfs-atmos', datetime(2026, 10, 17, 6, tzinfo=timezone.utc)))
        self.assertEqual(calls[0][3]['ifs_source'], 'aws')
        self.assertIsInstance(calls[0][3]['budget'], op.ObservedBudget)


if __name__ == '__main__':
    unittest.main(verbosity=2)
