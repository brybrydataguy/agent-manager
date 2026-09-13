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


# These are cleared in the new pane's environment, not just checked in the
# manager, because the Herdr server can have a different inherited environment.
API_ENV = (
    'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'CLAUDE_API_KEY',
    'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_USE_VERTEX',
    'XAI_API_KEY', 'XAI_BASE_URL', 'GROK_API_KEY', 'GROK_BASE_URL',
    'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS',
    'GOOGLE_GENAI_USE_VERTEXAI', 'OPENAI_API_KEY', 'CODEX_API_KEY',
    'OPENAI_BASE_URL', 'OPENAI_API_BASE', 'AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_ENDPOINT',
)

RESEARCH_TOOLS = 'Read,Glob,Grep,WebFetch,WebSearch'
SUBMISSION_TOOL = 'mcp__submission__submit_artifact'


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
      CREATE TABLE IF NOT EXISTS submissions(run TEXT, operation TEXT, generation INTEGER,
        version INTEGER, digest TEXT, PRIMARY KEY(run,operation));
      CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
        run TEXT, kind TEXT, detail TEXT, created REAL);
    ''')
    return db


def get(db, rid):
    row = db.execute('SELECT data FROM runs WHERE id=?', (rid,)).fetchone()
    if not row:
        raise Failure('Unknown run')
    run = json.loads(row[0])
    run.setdefault('generation', len(run['feedback']))
    return run


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
    output = command(['herdr', *args])
    if args[:2] in (('agent', 'read'), ('pane', 'read')):
        return {'text': output}
    return json.loads(output)


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
    approvals = config.get('approvals', {})
    if (not isinstance(approvals, dict) or set(approvals) - {'workspace_trust', 'artifact_submission'}
            or any(type(value) is not bool for value in approvals.values())):
        raise Failure('approvals supports only boolean workspace_trust and artifact_submission')
    rid = uuid.uuid4().hex[:16]
    run = dict(id=rid, task=task, config=config, project=str(project), state='created',
               version=0, accepted=None, token=secrets.token_urlsafe(32),
               agent='am-' + rid, pane=None, feedback=[], cleanup='pending')
    run['assignment_prompt'] = assignment_prompt(run)
    with db:
        save(db, run, 'created')
    return public(run)


def public(run):
    return {k: v for k, v in run.items() if k != 'token'}


def submit(db, rid, token, body, operation_id, generation):
    """Submission and notification commit in the same SQLite transaction."""
    if not isinstance(body, str):
        raise Failure('packet must be a JSON string')
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 128:
        raise Failure('operation_id must be a nonempty string of at most 128 characters')
    if type(generation) is not int or generation < 0:
        raise Failure('generation must be a nonnegative integer')
    if len(body.encode('utf-8')) > 2_000_000:
        raise Failure('Packet exceeds 2 MB')
    db.execute('BEGIN IMMEDIATE')
    try:
        run = get(db, rid)
        if not secrets.compare_digest(run['token'], token):
            raise Failure('Invalid submission capability')
        digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
        prior = db.execute('SELECT * FROM submissions WHERE run=? AND operation=?', (rid, operation_id)).fetchone()
        if prior:
            if prior['digest'] != digest or prior['generation'] != generation:
                raise Failure('operation_id already belongs to different content or generation')
            db.commit()
            return {'run': rid, 'version': prior['version'], 'sha256': digest,
                    'operation_id': operation_id, 'generation': generation}
        if generation != run['generation']:
            raise Failure('Stale submission generation; use the generation in the current assignment')
        if run['state'] not in ('working', 'revision_requested'):
            raise Failure('Run is not accepting a submission')
        version = run['version'] + 1
        db.execute('INSERT INTO packets VALUES (?,?,?,?)', (rid, version, body, digest))
        db.execute('INSERT INTO submissions VALUES (?,?,?,?,?)', (rid, operation_id, generation, version, digest))
        run.update(version=version, state='submitted', validated=None, delivery=None)
        save(db, run, 'submitted', {'version': version, 'sha256': digest})
        db.commit()
        return {'run': rid, 'version': version, 'sha256': digest,
                'operation_id': operation_id, 'generation': generation}
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
    candidate = directory / f'candidate-{version}.json'
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        candidate.write_text(row['body'], encoding='utf-8')
        json.loads(row['body'])
        validator = run['config'].get('validator', [])
        if validator:
            command([x.replace('{artifact}', str(candidate)) for x in validator],
                    cwd=run['project'], timeout=120)
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != row['digest']:
            raise Failure('Validator modified candidate bytes; validation is not approval of the submitted version')
        error = None
    except (Failure, ValueError, OSError) as e:
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


def revise(db, rid, reason, version):
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        prior = next((f for f in run['feedback'] if f['version'] == version), None)
        if prior:
            if prior['reason'] == reason:
                return public(run)
            raise Failure('This version already has different revision feedback')
        if version != run['version']:
            raise Failure('Revision must reference the current artifact version')
        if run['state'] not in ('submitted', 'validation_failed', 'review_ready'):
            raise Failure('No candidate available for revision')
        if len(run['feedback']) >= run['config'].get('max_revisions', 3):
            raise Failure('Revision limit reached; manager must discuss next steps with user')
        run['generation'] += 1
        run['feedback'].append({'version': version, 'reason': reason, 'id': uuid.uuid4().hex,
                                'sha256': packet(db, run, version)['digest'], 'generation': run['generation']})
        run.update(state='revision_requested', validated=None, delivery='pending')
        save(db, run, 'revision_requested', run['feedback'][-1])
    return public(run)


def worker_identity(run, ready=False):
    identity = herdr('agent', 'get', run['agent'])['result']['agent']
    if identity.get('pane_id') != run['pane'] or identity.get('agent') != 'claude':
        raise Failure('Worker identity changed; refusing to target this session')
    if ready and identity.get('agent_status') not in ('idle', 'done'):
        raise Failure('Worker is not ready; inspect its session before delivery')
    return identity


def deliver(db, rid, retry=False):
    run = get(db, rid)
    allowed = ('pending', 'uncertain') if retry else ('pending',)
    if run['state'] != 'revision_requested' or run.get('delivery') not in allowed:
        raise Failure('No pending revision message')
    worker_identity(run, ready=True)
    # Mark before external IO. Uncertain delivery must never automatically double-send.
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if run['state'] != 'revision_requested' or run.get('delivery') not in allowed:
            raise Failure('Revision delivery already attempted')
        feedback = run['feedback'][-1]
        feedback.setdefault('id', uuid.uuid4().hex)
        delivery_id = feedback['id']
        attempt = uuid.uuid4().hex
        run['delivery_attempt'] = attempt
        run['delivery'] = 'uncertain'
        save(db, run, 'delivery_started')
    herdr('agent', 'prompt', run['agent'],
          'Revise your submitted artifact: ' + feedback['reason'] +
          '\n' + submission_instructions(run['generation']))
    with db:
        db.execute('BEGIN IMMEDIATE')
        current = get(db, rid)
        if (current['state'] == 'revision_requested' and current['feedback'][-1].get('id') == delivery_id
                and current.get('delivery_attempt') == attempt and current.get('delivery') == 'uncertain'):
            current['delivery'] = 'sent'
            save(db, current, 'revision_delivered', {'id': delivery_id})
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
        str(Path(root).resolve()), 'worker-server', rid, '--token', run['token']]}}}), encoding='utf-8')
    mcp_path.chmod(0o600)
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if run['state'] != 'created':
            raise Failure('Another process already started this run')
        run['state'] = 'starting'
        save(db, run, 'starting')
    environment = [value for name in API_ENV for value in ('--env', name + '=')]
    response = herdr('pane', 'split', '--current', '--direction', direction,
                     '--cwd', run['project'], '--no-focus', *environment)
    with db:
        run['pane'] = response['result']['pane']['pane_id']
        save(db, run, 'pane_created')
    return launch(db, root, rid)


def bare_pane(run, pane):
    info = herdr('pane', 'get', pane)['result']['pane']
    cwd = info.get('foreground_cwd') or info.get('cwd')
    if (info.get('pane_id') != pane or info.get('agent') is not None or not cwd
            or Path(cwd).resolve() != Path(run['project']).resolve()):
        raise Failure('Recovery requires an agent-free pane in the recorded project')


def launch(db, root, rid, retry=False):
    run = get(db, rid)
    if run['state'] != 'starting' or not run['pane']:
        raise Failure('Launch requires a recorded starting pane')
    bare_pane(run, run['pane'])
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if run['state'] != 'starting' or (run.get('launch_state') == 'uncertain' and not retry):
            raise Failure('Launch already attempted; inspect before retrying')
        run['launch_state'] = 'uncertain'
        save(db, run, 'launch_started')
    mcp_path = Path(root).resolve() / rid / 'mcp.json'
    if not mcp_path.is_file():
        raise Failure('Saved MCP configuration missing; restore it before launching')
    # Plan mode forbids the side-effecting submission tool even when allowlisted.
    # Use execution mode with a small tool surface instead of a global bypass.
    args = ['--permission-mode', 'default', '--strict-mcp-config', '--mcp-config', str(mcp_path),
            '--tools', RESEARCH_TOOLS, '--allowedTools', RESEARCH_TOOLS + ',' + SUBMISSION_TOOL]
    if run['config'].get('model'):
        args += ['--model', run['config']['model']]
    herdr('agent', 'start', run['agent'], '--kind', 'claude', '--pane', run['pane'], '--', *args)
    return assign(db, rid)


def recover_start(db, rid, no_pane_created=False, pane=None):
    """Reconcile an uncertain split after inspecting Herdr; never silently split twice."""
    run = get(db, rid)
    if run['state'] != 'starting' or run['pane']:
        raise Failure('Only an uncertain split without a recorded pane can be reconciled')
    if bool(no_pane_created) == bool(pane):
        raise Failure('Specify either --no-pane-created or --pane for the recovered worker')
    if pane:
        candidate = dict(run, pane=pane)
        info = herdr('pane', 'get', pane)['result']['pane']
        if info.get('agent') is None:
            bare_pane(candidate, pane)
        else:
            worker_identity(candidate, ready=True)
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if run['state'] != 'starting' or run['pane']:
            raise Failure('Startup changed during reconciliation')
        run.update(state='created' if no_pane_created else 'starting', pane=pane)
        save(db, run, 'split_reconciled', {'no_pane_created': no_pane_created, 'pane': pane})
    return public(run)


def submission_instructions(generation):
    return ('Submit the complete JSON as the packet string using submit_artifact, with generation '
            f'{generation} and a new unique operation_id. On a transport retry, reuse the SAME '
            'operation_id, generation, and packet bytes. Do not treat a transport retry as revised work. '
            'After submission wait for manager feedback.')


def assignment_prompt(run):
    return ('[external-model-orchestrator child]\nDo the assigned work directly. Do not spawn agents. '
            'Read the skill at ' + run['config']['skill'] + ' completely.\nTask: ' + run['task'] +
            '\nWork read-only. Do not write project files. You do not approve your own work.\n' +
            submission_instructions(0))


def assign(db, rid, retry=False):
    """Finish an interrupted startup only after the exact worker is ready."""
    run = get(db, rid)
    def eligible(value):
        return (value['pane'] and (value['state'] == 'starting' or
                (retry and value['state'] == 'working' and value['version'] == 0
                 and value.get('initial_delivery') == 'uncertain')))
    if not eligible(run):
        raise Failure('Only a recorded starting session can receive its initial assignment')
    worker_identity(run, ready=True)
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if not eligible(run):
            raise Failure('Initial assignment already attempted')
        attempt = uuid.uuid4().hex
        run.setdefault('assignment_prompt', assignment_prompt(run))
        run.update(state='working', initial_delivery='uncertain', initial_attempt=attempt)
        save(db, run, 'worker_ready')
    herdr('agent', 'prompt', run['agent'], run['assignment_prompt'])
    with db:
        db.execute('BEGIN IMMEDIATE')
        current = get(db, rid)
        if current.get('initial_attempt') == attempt:
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


def inspect_worker(db, rid):
    run = get(db, rid)
    identity = worker_identity(run)
    return {'run': public(run), 'worker': identity,
            'terminal': herdr('agent', 'read', run['agent'], '--source', 'recent-unwrapped', '--lines', '120')}


def close(db, root, rid, retry=False):
    run = get(db, rid)
    if run['state'] != 'accepted' or not run['pane']:
        raise Failure('Cleanup requires an accepted run with an owned session')
    if run['cleanup'] == 'confirmed':
        return public(run)
    active = ('snapshot_in_progress', 'exit_uncertain', 'exit_requested')
    if run['cleanup'] in active:
        run = confirm_close(db, rid)
        if run['cleanup'] == 'confirmed' or not retry:
            return public(run)
    worker_identity(run)
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if run['cleanup'] == 'confirmed' or (run['cleanup'] in active and not retry):
            return public(run)
        attempt = uuid.uuid4().hex
        run.update(cleanup='snapshot_in_progress', cleanup_attempt=attempt)
        save(db, run, 'cleanup_claimed')
    path = Path(root).resolve() / rid / 'terminal-snapshot.json'
    try:
        transcript = herdr('agent', 'read', run['agent'], '--source', 'recent-unwrapped', '--lines', '2000')
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_text(json.dumps(transcript), encoding='utf-8')
    except (OSError, Failure) as e:
        with db:
            db.execute('BEGIN IMMEDIATE')
            current = get(db, rid)
            if current.get('cleanup_attempt') == attempt and current['cleanup'] == 'snapshot_in_progress':
                current.update(cleanup='snapshot_failed', snapshot_error=str(e))
                save(db, current, 'snapshot_failed')
        raise Failure('Snapshot failed; retry cleanup after fixing storage or session access') from e
    with db:
        db.execute('BEGIN IMMEDIATE')
        current = get(db, rid)
        if current.get('cleanup_attempt') != attempt or current['cleanup'] != 'snapshot_in_progress':
            return public(current)
        current.pop('snapshot_error', None)
        current['cleanup'] = 'exit_uncertain'
        save(db, current, 'exit_started', {'snapshot': str(path)})
    # Native exit control is separate from conversational revision delivery.
    try:
        worker_identity(run)
        herdr('agent', 'send-keys', run['agent'], 'esc')
        herdr('agent', 'prompt', run['agent'], '/exit')
    except Failure:
        pass
    else:
        with db:
            db.execute('BEGIN IMMEDIATE')
            current = get(db, rid)
            if current.get('cleanup_attempt') == attempt and current['cleanup'] == 'exit_uncertain':
                current['cleanup'] = 'exit_requested'
                save(db, current, 'exit_sent')
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
            if not isinstance(req, dict):
                raise ValueError('JSON-RPC request must be an object')
            if 'id' not in req:
                continue
            if not isinstance(req.get('params', {}), dict):
                raise ValueError('params must be an object')
            method = req.get('method')
            if method == 'initialize':
                result = {'protocolVersion': req.get('params', {}).get('protocolVersion', '2024-11-05'),
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'agent-manager-submission', 'version': '0.2.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': [{'name': 'submit_artifact', 'description': 'Durably submit candidate JSON and notify manager. Wait for review afterward.',
                          'inputSchema': {'type': 'object', 'properties': {
                              'packet': {'type': 'string'},
                              'operation_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                              'generation': {'type': 'integer', 'minimum': 0}},
                              'required': ['packet', 'operation_id', 'generation'], 'additionalProperties': False}}]}
            elif method == 'tools/call':
                try:
                    if req['params']['name'] != 'submit_artifact':
                        raise Failure('Unknown tool')
                    arguments = req['params']['arguments']
                    result = {'content': [{'type': 'text', 'text': json.dumps(submit(db, rid, token,
                        arguments['packet'], arguments['operation_id'], arguments['generation']))}]}
                except Exception as e:
                    # A malformed tool call or a recoverable storage exception must
                    # not disconnect the transport. submit rolls back its transaction.
                    db.rollback()
                    result = {'isError': True, 'content': [{'type': 'text', 'text': str(e)}]}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'error': {'code': -32601, 'message': 'Unknown method'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}), flush=True)
        except (ValueError, TypeError) as e:
            print(json.dumps({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': str(e)}}), flush=True)


def main():
    os.umask(0o077)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default='.manager', help='Private run store; reuse across manager restarts')
    sub = p.add_subparsers(dest='action', required=True)
    c = sub.add_parser('create'); c.add_argument('--config', required=True); c.add_argument('--task', required=True)
    for name in ('status', 'inspect', 'start', 'recover-start', 'launch', 'assign', 'validate', 'revise', 'deliver', 'accept', 'artifact', 'close', 'worker-server'):
        c = sub.add_parser(name); c.add_argument('run')
        if name in ('accept', 'artifact', 'revise'): c.add_argument('--version', type=int, required=True)
        if name in ('accept', 'revise'): c.add_argument('--reason', required=True)
        if name == 'start': c.add_argument('--direction', choices=['right', 'down'], default='right')
        if name == 'worker-server': c.add_argument('--token', required=True)
        if name in ('assign', 'deliver'): c.add_argument('--retry-undelivered', action='store_true', help='Only after inspecting the worker and establishing the message was not delivered')
        if name == 'close': c.add_argument('--retry-exit', action='store_true', help='Deliberately reissue exit after checking the recorded worker')
        if name == 'launch': c.add_argument('--retry-launch', action='store_true', help='Retry only after verifying the recorded pane has no agent')
        if name == 'recover-start':
            group = c.add_mutually_exclusive_group(required=True)
            group.add_argument('--no-pane-created', action='store_true', help='Attest that inspection found no pane from this attempt')
            group.add_argument('--pane', help='Adopt the exact ready worker found after an uncertain split')
    sub.add_parser('list')
    c = sub.add_parser('wait'); c.add_argument('--after', type=int, default=0); c.add_argument('--timeout', type=float, default=45)
    args = p.parse_args()
    db = connect(args.root)
    try:
        a = args.action
        if a == 'create': result = create(db, args.config, args.task)
        elif a == 'list': result = [public(json.loads(r[0])) for r in db.execute('SELECT data FROM runs')]
        elif a == 'status': result = public(get(db, args.run))
        elif a == 'inspect': result = inspect_worker(db, args.run)
        elif a == 'start': result = start(db, args.root, args.run, args.direction)
        elif a == 'launch': result = launch(db, args.root, args.run, args.retry_launch)
        elif a == 'recover-start': result = recover_start(db, args.run, args.no_pane_created, args.pane)
        elif a == 'assign': result = assign(db, args.run, args.retry_undelivered)
        elif a == 'validate': result = validate(db, args.root, args.run)
        elif a == 'revise': result = revise(db, args.run, args.reason, args.version)
        elif a == 'deliver': result = deliver(db, args.run, args.retry_undelivered)
        elif a == 'accept': result = accept(db, args.run, args.version, args.reason)
        elif a == 'artifact':
            row = packet(db, get(db, args.run), args.version)
            result = {'version': args.version, 'sha256': row['digest'], 'packet': row['body']}
        elif a == 'close': result = close(db, args.root, args.run, args.retry_exit)
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
