"""Public-boundary regressions from the independent adversarial review."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import manager as m
import uuid


def submit(db, rid, token, body):
    return m.submit(db, rid, token, body, uuid.uuid4().hex, m.get(db, rid)['generation'])


def revise(db, rid, reason):
    return m.revise(db, rid, reason, m.get(db, rid)['version'])


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = m.connect(self.root)
        (self.root / 'skill.md').write_text('Research')
        config = self.root / 'config.json'
        config.write_text(json.dumps({'project': '.', 'provider': 'claude', 'skill': 'skill.md'}))
        self.rid = m.create(self.db, config, 'Research')['id']

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def setup_run(self, state='working', **fields):
        with self.db:
            run = m.get(self.db, self.rid)
            run.update(state=state, pane='test:p2', **fields)
            m.save(self.db, run, 'fixture')
        return run

    def ready(self, *args):
        if args[:2] == ('pane', 'get'):
            return {'result': {'pane': {'pane_id': 'test:p2', 'agent': 'claude', 'cwd':str(self.root)}}}
        return {'result': {'agent': {'pane_id': 'test:p2', 'agent': 'claude', 'agent_status': 'idle'}}}

    def test_mcp_bad_call_does_not_disconnect_valid_followup(self):
        run = self.setup_run()
        requests = [
            {'jsonrpc': '2.0', 'id': n, 'method': 'tools/call', 'params': {
                'name': 'submit_artifact', 'arguments': {'packet': body, 'operation_id': 'op-1', 'generation': 0}}}
            for n, body in [(1, {'summary': 'object'}), (2, '{"summary":"valid"}')]]
        p = subprocess.run([sys.executable, m.__file__, '--root', str(self.root),
            'worker-server', self.rid, '--token', run['token']],
            input='\n'.join(json.dumps(r) for r in requests)+'\n', text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        responses = [json.loads(s) for s in p.stdout.splitlines()]
        self.assertTrue(responses[0]['result']['isError'])
        self.assertNotIn('isError', responses[1]['result'])

    def test_validator_delete_records_failure(self):
        run = self.setup_run()
        submit(self.db, self.rid, run['token'], '{}')
        script = self.root / 'delete.py'
        script.write_text('import sys\nfrom pathlib import Path\nPath(sys.argv[1]).unlink()\n')
        with self.db:
            run = m.get(self.db, self.rid)
            run['config']['validator'] = [sys.executable, str(script), '{artifact}']
            m.save(self.db, run, 'validator')
        self.assertEqual(m.validate(self.db, self.root, self.rid)['state'], 'validation_failed')

    def test_cleanup_recreates_missing_directory(self):
        run = self.setup_run()
        submit(self.db, self.rid, run['token'], '{}')
        m.validate(self.db, self.root, self.rid)
        m.accept(self.db, self.rid, 1, 'Reviewed')
        directory = self.root / self.rid
        for file in directory.iterdir(): file.unlink()
        directory.rmdir()
        with patch.object(m, 'herdr', self.ready):
            result = m.close(self.db, self.root, self.rid)
        self.assertTrue((directory / 'terminal-snapshot.json').exists())
        self.assertNotEqual(result['cleanup'], 'confirmed')

    def test_delivery_checks_worker_identity(self):
        run = self.setup_run()
        submit(self.db, self.rid, run['token'], '{}')
        revise(self.db, self.rid, 'Fix evidence')
        with patch.object(m, 'herdr', return_value={'result':{'agent':{
                'pane_id':'wrong:p9', 'agent':'claude', 'agent_status':'idle'}}}) as h:
            with self.assertRaises(m.Failure): m.deliver(self.db, self.rid)
            self.assertNotIn(('agent', 'prompt'), [call.args[:2] for call in h.call_args_list])

    def test_split_recovery_requires_reconciliation(self):
        with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch.object(m, 'command', return_value='ok'), patch.object(m, 'herdr', side_effect=m.Failure('lost split response')):
            with self.assertRaises(m.Failure): m.start(self.db, self.root, self.rid, 'right')
        self.assertEqual(m.get(self.db, self.rid)['state'], 'starting')
        with self.assertRaises(m.Failure): m.recover_start(self.db, self.rid)
        self.assertEqual(m.recover_start(self.db, self.rid, no_pane_created=True)['state'], 'created')

    def test_split_recovery_adopts_exact_existing_worker(self):
        self.setup_run('starting', initial_delivery=None)
        with self.db:
            run = m.get(self.db, self.rid); run['pane'] = None
            m.save(self.db, run, 'unknown-pane')
        with patch.object(m, 'herdr', self.ready):
            self.assertEqual(m.recover_start(self.db, self.rid, pane='test:p2')['pane'], 'test:p2')
            self.assertEqual(m.assign(self.db, self.rid)['state'], 'working')

    def test_initial_prompt_retry_uses_persisted_bytes_after_restart(self):
        self.setup_run('starting')
        prompts = []
        def fail_prompt(*args):
            if args[:2] == ('agent', 'prompt'):
                prompts.append(args[-1])
                raise m.Failure('lost prompt response')
            return self.ready(*args)
        with patch.object(m, 'herdr', fail_prompt):
            with self.assertRaises(m.Failure): m.assign(self.db, self.rid)
        self.db.close(); self.db = m.connect(self.root)
        def retry_prompt(*args):
            if args[:2] == ('agent', 'prompt'): prompts.append(args[-1])
            return self.ready(*args)
        with patch.object(m, 'herdr', retry_prompt):
            with self.assertRaises(m.Failure): m.assign(self.db, self.rid)
            self.assertEqual(m.assign(self.db, self.rid, retry=True)['initial_delivery'], 'sent')
        self.assertEqual(prompts[0], prompts[1])

    def test_delayed_delivery_response_does_not_clobber_new_feedback(self):
        run = self.setup_run()
        m.submit(self.db, self.rid, run['token'], '{}', 'first', 0)
        m.revise(self.db, self.rid, 'first correction', 1)
        def interleave(*args):
            if args[:2] == ('agent', 'prompt'):
                m.submit(self.db, self.rid, run['token'], '{"v":2}', 'second', 1)
                m.revise(self.db, self.rid, 'second correction', 2)
            return self.ready(*args)
        with patch.object(m, 'herdr', interleave): m.deliver(self.db, self.rid)
        self.assertEqual(m.get(self.db, self.rid)['delivery'], 'pending')

    def test_old_revision_retry_is_idempotent(self):
        run = self.setup_run()
        m.submit(self.db, self.rid, run['token'], '{}', 'first', 0)
        m.revise(self.db, self.rid, 'correct first', 1)
        m.submit(self.db, self.rid, run['token'], '{"v":2}', 'second', 1)
        m.revise(self.db, self.rid, 'correct first', 1)
        self.assertEqual(len(m.get(self.db, self.rid)['feedback']), 1)
        self.assertEqual(m.get(self.db, self.rid)['state'], 'submitted')
        with self.assertRaises(m.Failure): m.revise(self.db, self.rid, 'different stale reason', 1)

    def test_submission_retry_returns_original_receipt_without_new_version(self):
        run = self.setup_run()
        first = m.submit(self.db, self.rid, run['token'], '{}', 'op-1', 0)
        m.revise(self.db, self.rid, 'correct first', 1)
        receipt = m.submit(self.db, self.rid, run['token'], '{}', 'op-1', 0)
        self.assertEqual(receipt, first)
        self.assertEqual(m.get(self.db, self.rid)['state'], 'revision_requested')
        with self.assertRaises(m.Failure): m.submit(self.db, self.rid, run['token'], '{}', 'stale-op', 0)
        with self.assertRaises(m.Failure): m.submit(self.db, self.rid, run['token'], '{"changed":true}', 'op-1', 0)
        m.submit(self.db, self.rid, run['token'], '{"v":2}', 'op-2', 1)
        self.assertIsNone(m.get(self.db, self.rid)['delivery'])

    def test_exit_failure_has_deliberate_retry(self):
        run = self.setup_run()
        submit(self.db, self.rid, run['token'], '{}')
        m.validate(self.db, self.root, self.rid)
        m.accept(self.db, self.rid, 1, 'Reviewed')
        prompts = []
        def fail(*args):
            if args[:2] == ('agent', 'prompt'):
                prompts.append(args[-1]); raise m.Failure('write failed')
            return self.ready(*args)
        with patch.object(m, 'herdr', fail):
            self.assertEqual(m.close(self.db, self.root, self.rid)['cleanup'], 'exit_uncertain')
            m.close(self.db, self.root, self.rid)
            self.assertEqual(len(prompts), 1)
            m.close(self.db, self.root, self.rid, retry=True)
            self.assertEqual(len(prompts), 2)

    def test_non_ascii_packet_roundtrip(self):
        run = self.setup_run()
        body = '{"summary":"日本語 café"}'
        receipt = submit(self.db, self.rid, run['token'], body)
        self.assertEqual(m.validate(self.db, self.root, self.rid)['state'], 'review_ready')
        self.assertEqual((self.root / self.rid / 'candidate-1.json').read_bytes(), body.encode('utf-8'))
        self.assertEqual(receipt['version'], 1)

    def test_overlapping_cleanup_does_not_send_exit_twice(self):
        run = self.setup_run()
        submit(self.db, self.rid, run['token'], '{}')
        m.validate(self.db, self.root, self.rid)
        m.accept(self.db, self.rid, 1, 'Reviewed')
        exits = []
        def overlap(*args):
            if args[:2] == ('agent', 'read'):
                m.close(self.db, self.root, self.rid)
            if args[:2] == ('agent', 'prompt'):
                exits.append(args[-1])
            return self.ready(*args)
        with patch.object(m, 'herdr', overlap): m.close(self.db, self.root, self.rid)
        self.assertEqual(exits, ['/exit'])

    def test_adopt_bare_pane_then_launch_without_splitting_again(self):
        self.setup_run('starting')
        with self.db:
            run = m.get(self.db, self.rid); run['pane'] = None
            m.save(self.db, run, 'lost-response')
        directory = self.root / self.rid
        directory.mkdir()
        (directory / 'mcp.json').write_text('{}')
        calls = []
        def bare(*args):
            calls.append(args)
            if args[:2] == ('pane', 'get'):
                return {'result': {'pane': {'pane_id':'test:p2', 'agent':None, 'cwd':str(self.root)}}}
            return self.ready(*args)
        with patch.object(m, 'herdr', bare):
            m.recover_start(self.db, self.rid, pane='test:p2')
            self.assertEqual(m.launch(self.db, self.root, self.rid)['state'], 'starting')
        self.assertNotIn(('pane','split'), [a[:2] for a in calls])
        self.assertEqual([a[:2] for a in calls].count(('agent','start')), 1)

    def test_herdr_read_accepts_plain_terminal_text(self):
        with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch.object(m, 'command', return_value='terminal output\n'):
            self.assertEqual(m.herdr('agent', 'read', 'some-agent'), {'text':'terminal output\n'})


if __name__ == '__main__': unittest.main()
