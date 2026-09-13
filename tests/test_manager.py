import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import manager as m


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = m.connect(self.root)
        skill = self.root / 'SKILL.md'; skill.write_text('Research')
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'project': '.', 'skill': 'SKILL.md',
            'validator': [sys.executable, str(self.root / 'missing-validator.py'), '{artifact}']}))
        self.run = m.create(self.db, self.config, 'Research a question')['id']

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def working(self):
        run = m.get(self.db, self.run)
        with self.db:
            run['state'] = 'working'
            m.save(self.db, run, 'working')
        return run['token']

    def test_failure_restart_revision_accept(self):
        token = self.working()
        first = m.submit(self.db, self.run, token, '{"summary":"first"}')
        self.assertEqual(m.validate(self.db, self.root, self.run)['state'], 'validation_failed')
        self.db.close()
        self.db = m.connect(self.root)
        self.assertEqual(m.packet(self.db, m.get(self.db, self.run), 1)['body'], '{"summary":"first"}')
        (self.root / 'missing-validator.py').write_text('import sys\nsys.exit(0)\n')
        self.assertEqual(m.validate(self.db, self.root, self.run)['state'], 'review_ready')
        m.revise(self.db, self.run, 'Add source detail')
        second = m.submit(self.db, self.run, token, '{"summary":"revised"}')
        m.validate(self.db, self.root, self.run)
        with self.assertRaises(m.Failure):
            m.accept(self.db, self.run, first['version'], 'Old version')
        approved = m.accept(self.db, self.run, second['version'], 'Sources checked')
        self.assertEqual(approved['accepted'], 2)
        with self.assertRaises(m.Failure):
            m.submit(self.db, self.run, token, '{}')

    def test_invalid_json_is_durable(self):
        token = self.working()
        m.submit(self.db, self.run, token, 'bad json')
        self.assertEqual(m.validate(self.db, self.root, self.run)['state'], 'validation_failed')
        self.assertEqual(m.packet(self.db, m.get(self.db, self.run), 1)['body'], 'bad json')

    def test_scoped_submission_and_notification(self):
        token = self.working()
        with self.assertRaises(m.Failure):
            m.submit(self.db, self.run, 'wrong', '{}')
        m.submit(self.db, self.run, token, '{}')
        ev = m.events(self.db, 0, 0)
        self.assertEqual(ev[-1]['kind'], 'submitted')
        self.assertEqual(m.events(self.db, ev[-1]['id'], 0), [])
        with self.assertRaises(m.Failure):
            m.submit(self.db, self.run, token, '{}')

    def test_mcp_submission_survives_process_exit(self):
        token = self.working()
        req = {'jsonrpc':'2.0','id':1,'method':'tools/call',
               'params': {'name':'submit_artifact','arguments':{'packet':'{"summary":"MCP"}'}}}
        result = subprocess.run([sys.executable, str(Path(m.__file__)), '--root', str(self.root),
            'worker-server', self.run, '--token', token], input=json.dumps(req)+'\n',
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('isError', json.loads(result.stdout)['result'])
        self.assertEqual(m.get(self.db, self.run)['state'], 'submitted')

    def test_herdr_order_and_no_duplicate_launch(self):
        calls = []
        def fake(*args):
            calls.append(args)
            if args[:2] == ('agent', 'get'):
                return {'result':{'agent':{'pane_id':'test:p2','agent':'claude','agent_status':'idle'}}}
            return {'result': {'pane': {'pane_id': 'test:p2'}}}
        with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch.object(m, 'herdr', fake), patch.object(m, 'command', return_value='ok'):
            result = m.start(self.db, self.root, self.run, 'down')
            self.assertEqual([x[:2] for x in calls], [('pane','split'), ('agent','start'), ('agent','get'), ('agent','prompt')])
            self.assertEqual(result['state'], 'working')
            with self.assertRaises(m.Failure):
                m.start(self.db, self.root, self.run, 'down')

    def test_startup_failure_preserves_pane(self):
        def fake(*args):
            if args[:2] == ('pane','split'):
                return {'result':{'pane':{'pane_id':'test:p3'}}}
            raise m.Failure('approval required')
        with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch.object(m, 'herdr', fake), patch.object(m, 'command', return_value='ok'):
            with self.assertRaises(m.Failure): m.start(self.db, self.root, self.run, 'right')
        self.assertEqual(m.get(self.db, self.run)['pane'], 'test:p3')

    def test_no_herdr_from_outside(self):
        with patch.dict(os.environ, {'HERDR_ENV':''}), patch.object(m, 'command') as command:
            with self.assertRaises(m.Failure): m.start(self.db, self.root, self.run, 'right')
            command.assert_not_called()

    def test_wait_wakes_on_durable_submission(self):
        token = self.working()
        last = m.events(self.db, 0, 0)[-1]['id']
        def producer():
            db = m.connect(self.root)
            try:
                m.submit(db, self.run, token, '{}')
            finally:
                db.close()
        thread = threading.Thread(target=producer)
        thread.start()
        found = m.events(self.db, last, 2)
        thread.join()
        self.assertEqual(found[-1]['kind'], 'submitted')

    def test_concurrent_submission_does_not_overwrite(self):
        token = self.working()
        results = []
        def producer(body):
            db = m.connect(self.root)
            try:
                m.submit(db, self.run, token, body)
                results.append('ok')
            except m.Failure:
                results.append('refused')
            finally:
                db.close()
        threads = [threading.Thread(target=producer, args=(body,)) for body in ('{}', '[]')]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(results), ['ok', 'refused'])
        self.assertEqual(m.get(self.db, self.run)['version'], 1)

    def test_validation_cannot_accept_modified_artifact(self):
        token = self.working()
        m.submit(self.db, self.run, token, '{}')
        (self.root / 'missing-validator.py').write_text('import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text("[]")\n')
        self.assertEqual(m.validate(self.db, self.root, self.run)['state'], 'validation_failed')
        with self.assertRaises(m.Failure): m.accept(self.db, self.run, 1, 'Cannot approve')

    def test_exit_requires_positive_absence(self):
        token = self.working()
        m.submit(self.db, self.run, token, '{}')
        (self.root / 'missing-validator.py').write_text('')
        m.validate(self.db, self.root, self.run)
        m.accept(self.db, self.run, 1, 'Reviewed')
        with self.db:
            run = m.get(self.db, self.run)
            run['pane'] = 'test:p2'
            m.save(self.db, run, 'test-pane')
        def fake(*args):
            if args == ('agent','get','test:p2'):
                raise m.Failure('{"error":{"code":"agent_not_found"}}')
            if args[:2] == ('agent','get'):
                return {'result':{'agent':{'pane_id':'test:p2','agent':'claude'}}}
            return {}
        with patch.object(m, 'herdr', fake):
            self.assertEqual(m.close(self.db, self.root, self.run)['cleanup'], 'confirmed')
        with self.db:
            run = m.get(self.db, self.run); run['cleanup'] = 'exit_requested'
            m.save(self.db, run, 'test-unknown')
        with patch.object(m, 'herdr', side_effect=m.Failure('connection failed')):
            self.assertEqual(m.close(self.db, self.root, self.run)['cleanup'], 'exit_requested')


if __name__ == '__main__':
    unittest.main()
