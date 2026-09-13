import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import approvals
import manager as m


class Handshake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'skill.md').write_text('Research')
        config = self.root / 'config.json'
        config.write_text(json.dumps({'provider': 'agy', 'skill': 'skill.md',
            'approvals': {'workspace_trust': True, 'artifact_submission': True}, 'read_hosts': ['www.python.org']}))
        self.db = m.connect(self.root)
        self.rid = m.create(self.db, config, 'Research')['id']
        self.run = m.get(self.db, self.rid)
        self.run.update(state='starting', pane='w1:p2', worker_cwd=str(self.root),
                        bootstrap_text='Call get_assignment', bootstrap_phase='pending', bootstrap_after=0)
        with self.db: m.save(self.db, self.run, 'fixture')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_acknowledgment_survives_restart_and_is_idempotent(self):
        first = m.get_assignment(self.db, self.rid, self.run['token'])
        self.db.close(); self.db = m.connect(self.root)
        self.assertEqual(m.get_assignment(self.db, self.rid, self.run['token']), first)
        events = self.db.execute("SELECT * FROM events WHERE kind='assignment_acknowledged'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(m.get(self.db, self.rid)['initial_delivery'], 'acknowledged')

    def test_wrong_capability_cannot_acknowledge(self):
        with self.assertRaises(m.Failure): m.get_assignment(self.db, self.rid, 'wrong')
        self.assertEqual(m.get(self.db, self.rid)['state'], 'starting')

    def test_only_explicit_shell_refusal_has_bounded_retry(self):
        self.run.update(launch_state='shell_not_ready', shell_retries=1, shell_retry_after=0)
        with self.db: m.save(self.db, self.run, 'fixture')
        with patch.object(m, 'bare_pane') as bare, patch.object(m, 'launch', return_value={}) as launch:
            m.service(self.db, self.root, self.rid)
            bare.assert_called_once()
            launch.assert_called_once_with(self.db, self.root, self.rid, retry=True)
        self.run['shell_retries'] = 3
        with self.db: m.save(self.db, self.run, 'fixture')
        with patch.object(m, 'launch') as launch:
            with self.assertRaises(m.Failure): m.service(self.db, self.root, self.rid)
            launch.assert_not_called()

    def test_agy_mcp_bootstrap_is_not_resent(self):
        agent = {'result': {'agent': {'pane_id': 'w1:p2', 'agent': 'agy', 'agent_status': 'idle'}}}
        def herdr(*args):
            if args[:2] == ('agent', 'read'): return {'text': 'Prompt ready'}
            return agent
        with patch.object(m, 'herdr', side_effect=herdr) as h:
            m.service(self.db, self.root, self.rid)
            m.service(self.db, self.root, self.rid)
            prompts = [c.args for c in h.call_args_list if c.args[:2] == ('agent', 'prompt')]
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0][-1], '/mcp')

    def test_no_work_returned_after_submission_and_revision_has_new_identity(self):
        first = m.get_assignment(self.db, self.rid, self.run['token'])
        m.submit(self.db, self.rid, self.run['token'], '{}', 'op1', 0)
        self.assertEqual(m.get_assignment(self.db, self.rid, self.run['token'])['state'], 'submitted')
        m.revise(self.db, self.rid, 'Fix source', 1)
        second = m.get_assignment(self.db, self.rid, self.run['token'])
        self.assertNotEqual(first['assignment_id'], second['assignment_id'])
        self.assertIn('Fix source', second['instruction'])
        self.assertEqual(m.get(self.db, self.rid)['delivery'], 'acknowledged')

    def test_pending_herdr_launch_is_not_a_wrong_occupant(self):
        with patch.object(m, 'herdr', return_value={'result': {'agent': {
                'pane_id': 'w1:p2', 'launch_pending': True}}}):
            self.assertEqual(m.service(self.db, self.root, self.rid)['state'], 'starting')

    def test_approval_scope_and_no_sandbox_bypass(self):
        menu = "MCP\n\nsubmission/get_assignment\n\nAllow calling this tool?\n> 1. Yes, allow tool call\n  2. Yes, and always allow tool 'submission/get_assignment' in this conversation\nesc to cancel"
        self.assertEqual(approvals.decision(self.run, menu)[0], 'scoped_mcp')
        self.assertIsNone(approvals.decision(self.run, menu.replace('submission/', 'other/')))
        self.assertIsNone(approvals.decision(self.run, 'Allow sandbox bypass?\n> 1. Yes, run command\nesc to cancel'))
        url_menu = 'Read URL\n\nhttps://www.python.org/doc/\n\nAllow access to this URL?\n> 1. Yes, allow access\nesc to cancel'
        self.assertEqual(approvals.decision(self.run, url_menu)[0], 'configured_source_read')
        self.assertIsNone(approvals.decision(self.run, url_menu.replace('www.python.org', 'www.python.org.evil.test')))
        header = 'Command\nRequesting permission for:\n   curl -sI https://www.python.org/doc/\n\nRun this command?\n> 1. Yes, run command\nesc to cancel'
        self.assertEqual(approvals.decision(self.run, header)[0], 'configured_source_headers')
        self.assertIsNone(approvals.decision(self.run, header.replace('/doc/', '/doc/; touch /tmp/pwn')))
        self.assertIsNone(approvals.decision(self.run, header.replace('Run this command?', 'Allow sandbox bypass?')))

    def test_only_exact_workspace_trust(self):
        menu = f'Accessing workspace:\n\n{self.root}\n\nDo you trust the contents of this project?\n> Yes, I trust this folder\n  No, exit\nenter Confirm'
        self.assertEqual(approvals.decision(self.run, menu)[0], 'workspace_trust')
        self.assertIsNone(approvals.decision(self.run, menu.replace(str(self.root), '/other')))
        self.run['config']['approvals']['workspace_trust'] = False
        self.assertIsNone(approvals.decision(self.run, menu))
