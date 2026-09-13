#!/usr/bin/env python3
"""Opt-in real subscription/Herdr lifecycle tests. Never included in unit discovery."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manager as m

TASK = ('Fetch https://www.python.org/doc/ and explain in two sentences why official documentation '
        'is useful as a primary source. Distinguish observation from inference, cite what you actually '
        'inspected, and disclose limitations. This is an integration smoke test, not publication work. '
        'Do not explore the repository beyond the assigned skill.')
REVISION = 'Preserve your summary and source. Add a limitations entry containing the exact text: integration-smoke-v2.'


def exercise(root, provider, model, timeout):
    db = m.connect(root)
    repo = Path(m.__file__).parent
    run = m.create(db, repo / 'examples/research/config.json', TASK, provider, model, 'Source research')
    rid = run['id']
    print(json.dumps({'run': rid, 'provider': provider, 'model': model, 'event': 'created'}), flush=True)
    directory = Path(root) / rid
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / 'launch.json').open('w', encoding='utf-8') as output:
        process = subprocess.Popen([sys.executable, str(repo / 'manager.py'), '--root', str(root),
                                    'start', rid], stdout=output, stderr=output, cwd=repo)
    deadline = time.monotonic() + timeout
    previous = None
    try:
        while time.monotonic() < deadline:
            run = m.get(db, rid)
            status = (run['state'], run['version'], run['cleanup'], run.get('acknowledged_generation'))
            if status != previous:
                print(json.dumps({'run': rid, 'status': status}), flush=True)
                previous = status
            if run.get('tab_removed'):
                acknowledgments = [json.loads(r[0])['generation'] for r in db.execute(
                    "SELECT detail FROM events WHERE run=? AND kind='assignment_acknowledged'", (rid,))]
                if acknowledgments != [0, 1]:
                    raise m.Failure('Missing generation acknowledgments: ' + str(acknowledgments))
                result = {'run': rid, 'provider': provider, 'model': model, 'result': 'passed'}
                (directory / 'smoke-result.json').write_text(json.dumps(result), encoding='utf-8')
                return result
            if run['state'] == 'submitted':
                candidate = json.loads(m.packet(db, run, run['version'])['body'])
                if m.validate(db, root, rid)['state'] != 'review_ready':
                    raise m.Failure('Packet failed contract validation')
                if run['version'] == 1:
                    m.revise(db, rid, REVISION, 1)
                elif run['version'] == 2 and 'integration-smoke-v2' in '\n'.join(candidate['limitations']):
                    m.accept(db, rid, 2, 'Integration-only acceptance: contract and requested revision marker verified. Not publication approval.')
                else:
                    raise m.Failure('Unexpected version or missing requested correction')
            if run.get('pane'):
                try:
                    m.service(db, root, rid)
                except m.Failure as error:
                    if process.poll() is not None or 'agent_not_found' not in str(error):
                        raise
            elif process.poll() is not None:
                raise m.Failure('Launcher exited without a worker pane')
            time.sleep(1)
        raise m.Failure('Live lifecycle timed out; inspect retained worker')
    except (m.Failure, OSError, ValueError) as error:
        result = {'run': rid, 'provider': provider, 'model': model, 'result': 'failed', 'error': str(error)}
        (directory / 'smoke-result.json').write_text(json.dumps(result), encoding='utf-8')
        return result
    finally:
        # The launcher has its own bounded timeout. Do not terminate worker processes here.
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--case', action='append', required=True, help='provider:model; uses real subscription quota')
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    if os.environ.get('HERDR_ENV') != '1':
        parser.error('Run inside Herdr')
    root = Path(args.root).resolve()
    db = m.connect(root)
    m.manager_workspace(db, str(Path(m.__file__).parent), 'Manager Live Verification')
    db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(exercise, root, *case.split(':', 1), args.timeout) for case in args.case]
        results = [future.result() for future in futures]
    (root / 'matrix.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results, indent=2), flush=True)
    return int(any(r['result'] != 'passed' for r in results))


if __name__ == '__main__':
    sys.exit(main())
