#!/usr/bin/env python3
"""Local, durable manager operations. Python 3.10+, no third-party packages."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid


class Failure(Exception):
    pass


def connect(root):
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'state.sqlite', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS packets(run TEXT, version INTEGER, body TEXT, digest TEXT,
        PRIMARY KEY(run,version));
      CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
        run TEXT, kind TEXT, detail TEXT, created REAL);
    ''')
    return db


def get(db, rid):
    row = db.execute('SELECT data FROM runs WHERE id=?', (rid,)).fetchone()
    if not row:
        raise Failure('Unknown run')
    return json.loads(row[0])


def save(db, run, kind, detail=None):
    db.execute('INSERT OR REPLACE INTO runs VALUES (?,?)', (run['id'], json.dumps(run)))
    db.execute('INSERT INTO events(run,kind,detail,created) VALUES (?,?,?,?)',
               (run['id'], kind, json.dumps(detail), time.time()))


def command(argv, cwd=None, timeout=60):
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Failure(str(e)) from e
    if p.returncode:
        raise Failure((p.stderr or p.stdout)[-6000:] or f'Command exited {p.returncode}')
    return p.stdout


def herdr(*args):
    if os.environ.get('HERDR_ENV') != '1':
        raise Failure('Visible sessions require running inside Herdr. No session was controlled.')
    return json.loads(command(['herdr', *args]))


def create(db, config_path, task):
    path = Path(config_path).resolve()
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise Failure('Configuration must be an object')
    project = (path.parent / config.get('project', '.')).resolve()
    if not project.is_dir():
        raise Failure('Project directory does not exist')
    for field in ('skill',):
        value = (project / config[field]).resolve()
        if not value.is_file():
            raise Failure(f'Missing {field}: {value}')
        config[field] = str(value)
    for key in ('validator', 'preflight'):
        value = config.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise Failure(f'{key} must be an argv array')
    if config.get('validator') and not any('{artifact}' in x for x in config['validator']):
        raise Failure('Validator must receive {artifact}')
    revisions = config.get('max_revisions', 3)
    if type(revisions) is not int or revisions < 0:
        raise Failure('max_revisions must be a nonnegative integer')
    if config.get('provider', 'claude') != 'claude':
        raise Failure('v1 visible adapter supports Claude only')
    rid = uuid.uuid4().hex[:16]
    run = dict(id=rid, task=task, config=config, project=str(project), state='created',
               version=0, accepted=None, token=secrets.token_urlsafe(32),
               agent='am-' + rid, pane=None, feedback=[], cleanup='pending')
    with db:
        save(db, run, 'created')
    return public(run)


def public(run):
    return {k: v for k, v in run.items() if k != 'token'}


def submit(db, rid, token, body):
    """Submission and notification commit in the same SQLite transaction."""
    if len(body.encode()) > 2_000_000:
        raise Failure('Packet exceeds 2 MB')
    db.execute('BEGIN IMMEDIATE')
    try:
        run = get(db, rid)
        if not secrets.compare_digest(run['token'], token):
            raise Failure('Invalid submission capability')
        if run['state'] not in ('working', 'revision_requested'):
            raise Failure('Run is not accepting a submission')
        version = run['version'] + 1
        digest = hashlib.sha256(body.encode()).hexdigest()
        db.execute('INSERT INTO packets VALUES (?,?,?,?)', (rid, version, body, digest))
        run.update(version=version, state='submitted', validated=None)
        save(db, run, 'submitted', {'version': version, 'sha256': digest})
        db.commit()
        return {'run': rid, 'version': version, 'sha256': digest}
    except Exception:
        db.rollback()
        raise


def packet(db, run, version):
    row = db.execute('SELECT body,digest FROM packets WHERE run=? AND version=?',
                     (run['id'], version)).fetchone()
    if not row:
        raise Failure('Unknown artifact version')
    return row


def validate(db, root, rid):
    run = get(db, rid)
    if run['state'] not in ('submitted', 'validation_failed', 'review_ready'):
        raise Failure('No candidate awaiting validation')
    version = run['version']
    row = packet(db, run, version)
    directory = Path(root).resolve() / rid
    directory.mkdir(mode=0o700, exist_ok=True)
    candidate = directory / f'candidate-{version}.json'
    candidate.write_text(row['body'])
    try:
        json.loads(row['body'])
        validator = run['config'].get('validator', [])
        if validator:
            command([x.replace('{artifact}', str(candidate)) for x in validator],
                    cwd=run['project'], timeout=120)
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != row['digest']:
            raise Failure('Validator modified candidate bytes; validation is not approval of the submitted version')
        error = None
    except (Failure, ValueError) as e:
        error = str(e)
    with db:
        db.execute('BEGIN IMMEDIATE')
        current = get(db, rid)
        if current['version'] != version or current['state'] not in ('submitted', 'validation_failed', 'review_ready'):
            raise Failure('Run changed during validation; retry against current state')
        current.update(state='validation_failed' if error else 'review_ready',
                       validated=None if error else row['digest'], validation_error=error)
        save(db, current, current['state'], {'version': version, 'error': error})
    return public(current)


def accept(db, rid, version, reason):
    db.execute('BEGIN IMMEDIATE')
    try:
        run = get(db, rid)
        row = packet(db, run, version)
        if run['state'] != 'review_ready' or version != run['version'] or run.get('validated') != row['digest']:
            raise Failure('Approval requires the current successfully validated version')
        run.update(state='accepted', accepted=version, review=reason)
        save(db, run, 'accepted', {'version': version, 'reason': reason, 'sha256': row['digest']})
        db.commit()
        return public(run)
    except Exception:
        db.rollback()
        raise


def revise(db, rid, reason):
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if run['state'] not in ('submitted', 'validation_failed', 'review_ready'):
            raise Failure('No candidate available for revision')
        if len(run['feedback']) >= run['config'].get('max_revisions', 3):
            raise Failure('Revision limit reached; manager must discuss next steps with user')
        run['feedback'].append({'version': run['version'], 'reason': reason})
        run.update(state='revision_requested', validated=None, delivery='pending')
        save(db, run, 'revision_requested', run['feedback'][-1])
    return public(run)


def deliver(db, rid):
    run = get(db, rid)
    if run['state'] != 'revision_requested' or run.get('delivery') != 'pending':
        raise Failure('No pending revision message')
    # Mark before external IO. Uncertain delivery must never automatically double-send.
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if run['state'] != 'revision_requested' or run.get('delivery') != 'pending':
            raise Failure('Revision delivery already attempted')
        run['delivery'] = 'uncertain'
        save(db, run, 'delivery_started')
    herdr('agent', 'prompt', run['agent'],
          'Revise your submitted artifact: ' + run['feedback'][-1]['reason'] +
          '\nSubmit the complete revised packet using the submit_artifact tool.')
    with db:
        current = get(db, rid)
        current['delivery'] = 'sent'
        save(db, current, 'revision_delivered')
    return public(current)


def start(db, root, rid, direction):
    run = get(db, rid)
    if run['state'] != 'created':
        raise Failure('Session already attempted. Inspect saved pane before recovery; do not duplicate it.')
    if os.environ.get('HERDR_ENV') != '1':
        raise Failure('Start the manager inside Herdr to launch a visible worker')
    config = run['config']
    wrapper = os.environ.get('AGENT_MANAGER_AUTH_WRAPPER',
        str(Path.home() / '.codex/skills/external-model-orchestrator/scripts/run-external-model'))
    command([wrapper, '--check', 'claude'])
    if config.get('preflight'):
        command(config['preflight'], cwd=run['project'])
    directory = Path(root).resolve() / rid
    directory.mkdir(mode=0o700, exist_ok=True)
    mcp_path = directory / 'mcp.json'
    mcp_path.write_text(json.dumps({'mcpServers': {'submission': {
        'command': sys.executable, 'args': [str(Path(__file__).resolve()), '--root',
        str(Path(root).resolve()), 'worker-server', rid, '--token', run['token']]}}}))
    mcp_path.chmod(0o600)
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if run['state'] != 'created':
            raise Failure('Another process already started this run')
        run['state'] = 'starting'
        save(db, run, 'starting')
    response = herdr('pane', 'split', '--current', '--direction', direction,
                     '--cwd', run['project'], '--no-focus')
    with db:
        run['pane'] = response['result']['pane']['pane_id']
        save(db, run, 'pane_created')
    args = ['--permission-mode', 'plan', '--strict-mcp-config', '--mcp-config', str(mcp_path),
            '--allowedTools', 'mcp__submission__submit_artifact']
    if config.get('model'):
        args += ['--model', config['model']]
    herdr('agent', 'start', run['agent'], '--kind', 'claude', '--pane', run['pane'], '--', *args)
    return assign(db, rid)


def assign(db, rid):
    """Finish an interrupted startup only after the exact worker is ready."""
    run = get(db, rid)
    if run['state'] != 'starting' or not run['pane']:
        raise Failure('Only a recorded starting session can receive its initial assignment')
    identity = herdr('agent', 'get', run['agent'])['result']['agent']
    if (identity.get('pane_id') != run['pane'] or identity.get('agent') != 'claude'
            or identity.get('agent_status') not in ('idle', 'done')):
        raise Failure('Exact Claude worker must be ready before assigning; inspect startup UI')
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if run['state'] != 'starting':
            raise Failure('Initial assignment already attempted')
        run.update(state='working', initial_delivery='uncertain')
        save(db, run, 'worker_ready')
    prompt = ('[external-model-orchestrator child]\nDo the assigned work directly. Do not spawn agents. '
              'Read the skill at ' + run['config']['skill'] + ' completely.\nTask: ' + run['task'] +
              '\nWork read-only. Submit the complete candidate JSON as the packet string to '
              'mcp__submission__submit_artifact. This tool stores it and notifies your manager. '
              'Do not write project files. After submission wait for revision instructions. '
              'You do not approve your own work.')
    herdr('agent', 'prompt', run['agent'], prompt)
    with db:
        current = get(db, rid)
        current['initial_delivery'] = 'sent'
        save(db, current, 'assigned')
    return public(current)


def events(db, after, timeout):
    deadline = time.monotonic() + timeout
    while True:
        rows = db.execute('SELECT * FROM events WHERE id>? ORDER BY id', (after,)).fetchall()
        if rows or time.monotonic() >= deadline:
            return [dict(r) for r in rows]
        time.sleep(0.2)


def close(db, root, rid):
    run = get(db, rid)
    if run['state'] != 'accepted' or not run['pane']:
        raise Failure('Cleanup requires an accepted run with an owned session')
    if run['cleanup'] == 'confirmed':
        return public(run)
    if run['cleanup'] == 'exit_requested':
        return confirm_close(db, rid)
    identity = herdr('agent', 'get', run['agent'])['result']['agent']
    if identity.get('pane_id') != run['pane'] or identity.get('agent') != 'claude':
        raise Failure('Worker identity changed; inspect before cleanup')
    transcript = herdr('agent', 'read', run['agent'], '--source', 'recent-unwrapped', '--lines', '2000')
    path = Path(root).resolve() / rid / 'terminal-snapshot.json'
    path.write_text(json.dumps(transcript))
    with db:
        run['cleanup'] = 'exit_requested'
        save(db, run, 'exit_requested', {'snapshot': str(path)})
    # Native exit control is not a research prompt. prompt may report a stalled
    # lifecycle after /exit, so the postcondition is checked independently.
    herdr('agent', 'send-keys', run['agent'], 'esc')
    try:
        herdr('agent', 'prompt', run['agent'], '/exit')
    except Failure:
        pass
    return confirm_close(db, rid)


def confirm_close(db, rid):
    run = get(db, rid)
    try:
        herdr('agent', 'get', run['pane'])
    except Failure as e:
        try:
            absent = json.loads(str(e)).get('error', {}).get('code') == 'agent_not_found'
        except ValueError:
            absent = False
        if absent:
            with db:
                run['cleanup'] = 'confirmed'
                save(db, run, 'worker_exited')
    return public(run)


def worker_server(db, rid, token):
    """Minimal newline-delimited MCP stdio transport with one scoped submission tool."""
    for line in sys.stdin:
        try:
            req = json.loads(line)
            if 'id' not in req:
                continue
            method = req.get('method')
            if method == 'initialize':
                result = {'protocolVersion': req.get('params', {}).get('protocolVersion', '2024-11-05'),
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'agent-manager-submission', 'version': '0.1.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': [{'name': 'submit_artifact', 'description': 'Durably submit candidate JSON and notify manager. Wait for review afterward.',
                          'inputSchema': {'type': 'object', 'properties': {'packet': {'type': 'string'}}, 'required': ['packet'], 'additionalProperties': False}}]}
            elif method == 'tools/call':
                try:
                    if req['params']['name'] != 'submit_artifact':
                        raise Failure('Unknown tool')
                    result = {'content': [{'type': 'text', 'text': json.dumps(submit(db, rid, token, req['params']['arguments']['packet']))}]}
                except (Failure, KeyError, TypeError) as e:
                    result = {'isError': True, 'content': [{'type': 'text', 'text': str(e)}]}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'error': {'code': -32601, 'message': 'Unknown method'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}), flush=True)
        except (ValueError, TypeError) as e:
            print(json.dumps({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': str(e)}}), flush=True)


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default='.manager', help='Private run store; reuse across manager restarts')
    sub = p.add_subparsers(dest='action', required=True)
    c = sub.add_parser('create'); c.add_argument('--config', required=True); c.add_argument('--task', required=True)
    for name in ('status', 'start', 'assign', 'validate', 'revise', 'deliver', 'accept', 'artifact', 'close', 'worker-server'):
        c = sub.add_parser(name); c.add_argument('run')
        if name in ('accept', 'artifact'): c.add_argument('--version', type=int, required=True)
        if name in ('accept', 'revise'): c.add_argument('--reason', required=True)
        if name == 'start': c.add_argument('--direction', choices=['right', 'down'], default='right')
        if name == 'worker-server': c.add_argument('--token', required=True)
    sub.add_parser('list')
    c = sub.add_parser('wait'); c.add_argument('--after', type=int, default=0); c.add_argument('--timeout', type=float, default=45)
    args = p.parse_args()
    db = connect(args.root)
    try:
        a = args.action
        if a == 'create': result = create(db, args.config, args.task)
        elif a == 'list': result = [public(json.loads(r[0])) for r in db.execute('SELECT data FROM runs')]
        elif a == 'status': result = public(get(db, args.run))
        elif a == 'start': result = start(db, args.root, args.run, args.direction)
        elif a == 'assign': result = assign(db, args.run)
        elif a == 'validate': result = validate(db, args.root, args.run)
        elif a == 'revise': result = revise(db, args.run, args.reason)
        elif a == 'deliver': result = deliver(db, args.run)
        elif a == 'accept': result = accept(db, args.run, args.version, args.reason)
        elif a == 'artifact':
            row = packet(db, get(db, args.run), args.version)
            result = {'version': args.version, 'sha256': row['digest'], 'packet': row['body']}
        elif a == 'close': result = close(db, args.root, args.run)
        elif a == 'wait': result = events(db, args.after, min(60, max(0, args.timeout)))
        elif a == 'worker-server': return worker_server(db, args.run, args.token)
        print(json.dumps(result, indent=2))
    except (Failure, OSError, ValueError, KeyError, sqlite3.Error) as e:
        print(json.dumps({'error': str(e), 'recovery': 'Results remain in the run store. Inspect status before retrying.'}), file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
