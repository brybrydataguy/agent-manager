import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manager as m
import providers


class Providers(unittest.TestCase):
    def test_submission_grant_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            providers.prepare(directory, 'codex', {'command': 'python3', 'args': []})
            for provider in ('claude', 'grok', 'codex'):
                self.assertNotIn('submit_artifact', ' '.join(providers.arguments(provider, directory)))
                self.assertIn('submit_artifact', ' '.join(providers.arguments(
                    provider, directory, approvals={'artifact_submission': True})))

    def test_cleanup_waits_for_final_worker_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = m.connect(tmp)
            run = {'id': 'busy', 'feedback': [], 'state': 'accepted', 'cleanup': 'pending', 'pane': 'p'}
            with db: m.save(db, run, 'fixture')
            with patch.object(m, 'worker_identity', return_value={'agent_status': 'working'}), patch.object(m, 'herdr') as h:
                self.assertEqual(m.close(db, tmp, 'busy')['cleanup'], 'pending')
                h.assert_not_called()
            db.close()

    def test_explicit_models_and_private_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            for provider in providers.NAMES:
                directory = Path(tmp) / provider
                directory.mkdir()
                cwd = providers.prepare(directory, provider, {'command': 'python3', 'args': ['worker-server']})
                args = providers.arguments(provider, directory, 'chosen-model')
                self.assertEqual(args[-2:], ['--model', 'chosen-model'])
                self.assertNotIn('--dangerously-skip-permissions', args)
                self.assertEqual((directory / 'mcp.json').stat().st_mode & 0o777, 0o600)
                if cwd:
                    self.assertTrue(Path(cwd).is_relative_to(directory))

    def test_provider_specific_identity(self):
        for provider in providers.NAMES:
            run = {'agent': 'worker', 'pane': 'w1:p2', 'config': {'provider': provider}}
            with patch.object(m, 'herdr', return_value={'result': {'agent': {
                    'pane_id': 'w1:p2', 'agent': provider, 'agent_status': 'idle'}}}):
                m.worker_identity(run, ready=True)
            with patch.object(m, 'herdr', return_value={'result': {'agent': {
                    'pane_id': 'w1:p2', 'agent': 'wrong', 'agent_status': 'idle'}}}):
                with self.assertRaises(m.Failure):
                    m.worker_identity(run)

    def test_native_exit_commands(self):
        self.assertEqual(providers.exit_controls('codex'), ('esc', '/quit'))
        self.assertEqual(providers.exit_controls('claude'), ('esc', '/exit'))
        self.assertEqual(providers.exit_controls('grok'), ('ctrl+c', '/exit'))
        self.assertEqual(providers.exit_controls('agy'), ('ctrl+c', '/exit'))

    def test_workspace_reused_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = m.connect(tmp)
            reply = {'result': {'workspace': {'workspace_id': 'w9'}}}
            with patch.object(m, 'herdr', return_value=reply) as h:
                self.assertEqual(m.manager_workspace(db, tmp)['workspace'], 'w9')
                db.close()
                db = m.connect(tmp)
                self.assertEqual(m.manager_workspace(db, tmp)['workspace'], 'w9')
                self.assertEqual([c.args[:2] for c in h.call_args_list],
                                 [('workspace', 'create'), ('workspace', 'get')])
            db.close()

    def test_uncertain_workspace_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = m.connect(tmp)
            with patch.object(m, 'herdr', side_effect=m.Failure('lost response')) as h:
                with self.assertRaises(m.Failure): m.manager_workspace(db, tmp)
                with self.assertRaises(m.Failure): m.manager_workspace(db, tmp)
                self.assertEqual(h.call_count, 1)
            db.close()

    def test_cleanup_refuses_tab_with_another_pane(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = m.connect(tmp)
            run = {'id': 'test', 'feedback': [], 'tab': 'w1:t2', 'pane': 'w1:p2', 'cleanup': 'confirmed'}
            with db: m.save(db, run, 'fixture')
            def fake(*args):
                if args[:2] == ('tab', 'get'):
                    return {'result': {'tab': {'pane_count': 2}}}
                return {'result': {'pane': {'tab_id': 'w1:t2', 'agent': None}}}
            with patch.object(m, 'herdr', side_effect=fake) as h:
                with self.assertRaises(m.Failure): m.remove_worker_tab(db, 'test')
                self.assertNotIn(('tab', 'close'), [c.args[:2] for c in h.call_args_list])
            db.close()
