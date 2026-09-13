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
import providers
import approvals
from auth import API_ENV


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
      CREATE TABLE IF NOT EXISTS submissions(run TEXT, operation TEXT, generation INTEGER,
        version INTEGER, digest TEXT, PRIMARY KEY(run,operation));
      CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
        run TEXT, kind TEXT, detail TEXT, created REAL);
      CREATE TABLE IF NOT EXISTS manager_layout(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
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
    output = command(['herdr', *args], timeout=190 if args[:2] == ('agent', 'start') else 60)
    if args[:2] in (('agent', 'read'), ('pane', 'read')):
        return {'text': output}
    return json.loads(output)


def create(db, config_path, task, provider=None, model=None, label=None):
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
    if provider is not None:
        config['provider'] = provider
    if model is not None:
        config['model'] = model
    if config.get('provider') not in providers.NAMES:
        raise Failure('Choose an explicit provider: claude, grok, codex, or agy')
    if config.get('model') is not None and (not isinstance(config['model'], str) or not config['model'].strip()):
        raise Failure('model must be a nonempty model ID')
    if (not isinstance(config.get('read_hosts', []), list) or
            any(not isinstance(host, str) or '/' in host or not host for host in config.get('read_hosts', []))):
        raise Failure('read_hosts must be an array of exact hostnames')
    approvals = config.get('approvals', {})
    if (not isinstance(approvals, dict) or set(approvals) - {'workspace_trust', 'artifact_submission'}
            or any(type(value) is not bool for value in approvals.values())):
        raise Failure('approvals supports only boolean workspace_trust and artifact_submission')
    rid = uuid.uuid4().hex[:16]
    run = dict(id=rid, task=task, config=config, project=str(project), state='created',
               version=0, accepted=None, token='am_' + secrets.token_urlsafe(32),
               agent='am-' + rid, pane=None, feedback=[], cleanup='pending',
               label=label or task[:48], handshake=True)
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
    if identity.get('pane_id') != run['pane'] or identity.get('agent') != run['config'].get('provider', 'claude'):
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
    message = ('Call get_assignment now for revision generation ' + str(run['generation']) +
               '. Follow the returned feedback, then submit and END YOUR TURN.') if run.get('handshake') else (
               'Revise your submitted artifact: ' + feedback['reason'] + '\n' + submission_instructions(run['generation']))
    herdr('agent', 'prompt', run['agent'], message)
    with db:
        db.execute('BEGIN IMMEDIATE')
        current = get(db, rid)
        if (current['state'] == 'revision_requested' and current['feedback'][-1].get('id') == delivery_id
                and current.get('delivery_attempt') == attempt and current.get('delivery') == 'uncertain'):
            current['delivery'] = 'sent'
            save(db, current, 'revision_delivered', {'id': delivery_id})
    return public(current)


def manager_workspace(db, project, label='Agent Manager', adopt=None, retry=False):
    """One workspace per durable manager store; uncertain creation needs inspection."""
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT data FROM manager_layout WHERE id=1').fetchone()
        layout = json.loads(row[0]) if row else {}
        if layout.get('workspace'):
            herdr('workspace', 'get', layout['workspace'])
            return layout
        if layout and not (adopt or retry):
            raise Failure('Workspace creation uncertain. Inspect Herdr, then workspace --adopt ID or --retry-create.')
        layout = {'label': layout.get('label', label), 'state': 'creating'}
        db.execute('INSERT OR REPLACE INTO manager_layout VALUES (1,?)', (json.dumps(layout),))
    if adopt:
        result = herdr('workspace', 'get', adopt)['result']
        if result['workspace'].get('label') != layout['label']:
            raise Failure('Recovered workspace label does not match this manager')
    else:
        result = herdr('workspace', 'create', '--cwd', project, '--label', layout['label'], '--no-focus')['result']
    layout.update(state='ready', workspace=result['workspace']['workspace_id'])
    with db:
        db.execute('UPDATE manager_layout SET data=? WHERE id=1', (json.dumps(layout),))
    return layout


def start(db, root, rid, direction=None):
    run = get(db, rid)
    if run['state'] != 'created':
        raise Failure('Session already attempted. Inspect saved pane before recovery; do not duplicate it.')
    if os.environ.get('HERDR_ENV') != '1':
        raise Failure('Start the manager inside Herdr to launch a visible worker')
    config = run['config']
    command([sys.executable, str(Path(__file__).with_name('auth.py')), '--check', config['provider']])
    if config.get('preflight'):
        command(config['preflight'], cwd=run['project'])
    directory = Path(root).resolve() / rid
    directory.mkdir(mode=0o700, exist_ok=True)
    worker_cwd = providers.prepare(directory, config.get('provider', 'claude'), {
        'command': sys.executable, 'args': [str(Path(__file__).resolve()), '--root',
        str(Path(root).resolve()), 'worker-server', rid, '--token=' + run['token']]}) or run['project']
    layout = manager_workspace(db, run['project'], config.get('manager_label', 'Agent Manager')) if direction is None else None
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if run['state'] != 'created':
            raise Failure('Another process already started this run')
        run['state'] = 'starting'
        run['worker_cwd'] = worker_cwd
        if layout:
            run['workspace'] = layout['workspace']
        save(db, run, 'starting')
    # Herdr can inherit a different environment from the coordinating process.
    environment = [value for name in API_ENV for value in ('--env', name + '=')]
    if layout:
        label = f"{run['label']} | {config['provider']} | {config.get('model') or 'default'} | {rid[:6]}"
        response = herdr('tab', 'create', '--workspace', layout['workspace'], '--label', label,
                         '--cwd', worker_cwd, '--no-focus', *environment)
        pane = response['result']['root_pane']['pane_id']
        run['tab'] = response['result']['tab']['tab_id']
    else:
        response = herdr('pane', 'split', '--current', '--direction', direction,
                         '--cwd', worker_cwd, '--no-focus', *environment)
        pane = response['result']['pane']['pane_id']
    with db:
        run['pane'] = pane
        save(db, run, 'pane_created')
    return launch(db, root, rid)


def bare_pane(run, pane):
    info = herdr('pane', 'get', pane)['result']['pane']
    cwd = info.get('foreground_cwd') or info.get('cwd')
    if (info.get('pane_id') != pane or info.get('agent') is not None or not cwd
            or Path(cwd).resolve() != Path(run.get('worker_cwd', run['project'])).resolve()):
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
    provider = run['config'].get('provider', 'claude')
    bootstrap = ('[external-model-orchestrator child] You are a managed worker. '
                 'Your FIRST action must be calling the submission MCP get_assignment tool with no arguments. '
                 'It returns your authorized assignment and generation. Do that work directly. '
                 'Do not spawn agents. If the tool is absent, report that blocker and END YOUR TURN. '
                 'Use native tool discovery if needed, never shell commands to locate tools.') if run.get('handshake') else None
    if bootstrap and provider == 'agy':
        bootstrap += (' Agy exposes MCP tools lazily through CallMcpTool. Read the tool schema at '
                      '~/.gemini/antigravity-cli/mcp/submission/get_assignment.json, then use CallMcpTool '
                      'with server submission, tool get_assignment, and empty arguments {}. '
                      'The tool is not a named built-in; that does not mean it is absent.')
    if bootstrap and provider == 'codex':
        bootstrap += (' In Codex code mode, MCP tools are nested tools, not top-level functions. '
                      'Use functions.exec to inspect ALL_TOOLS for the submission get_assignment entry, '
                      'then invoke its actual normalized name on tools and print its result. '
                      'Use the same mechanism for submit_artifact. Otherwise use native MCP tool discovery. '
                      'Do not declare the tool unavailable merely because it is not top-level.')
    args = providers.arguments(provider, mcp_path.parent, run['config'].get('model'), bootstrap,
                               run['config'].get('approvals', {}))
    if bootstrap:
        # Initialize the visible CLI before dispatch. Native initial prompts can
        # race Agy's MCP discovery and keep Herdr's startup waiter working forever.
        args = providers.arguments(provider, mcp_path.parent, run['config'].get('model'), None,
                                   run['config'].get('approvals', {}))
        with db:
            current = get(db, rid)
            current.update(bootstrap_text=bootstrap, bootstrap_phase='pending', bootstrap_after=time.time() + 3)
            save(db, current, 'bootstrap_prepared')
    try:
        herdr('agent', 'start', run['agent'], '--kind', provider, '--pane', run['pane'], '--timeout', '180000', '--', *args)
    except Failure as error:
        if not run.get('handshake'):
            raise
        with db:
            current = get(db, rid)
            current['launch_error'] = str(error)
            try:
                code = json.loads(str(error)).get('error', {}).get('code')
            except ValueError:
                code = None
            if code == 'agent_pane_busy':
                # Herdr positively refused before launching, unlike a timeout
                # with an unknown outcome. Retry only this refusal, boundedly.
                current['launch_state'] = 'shell_not_ready'
                current['shell_retries'] = current.get('shell_retries', 0) + 1
                current['shell_retry_after'] = time.time() + 2
            save(db, current, 'launch_needs_inspection')
        return public(current)
    return public(get(db, rid)) if run.get('handshake') else assign(db, rid)


def get_assignment(db, rid, token):
    """Worker handshake and durable acknowledgment, idempotent within a generation."""
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = get(db, rid)
        if not secrets.compare_digest(run['token'], token):
            raise Failure('Invalid worker capability')
        if run['state'] not in ('starting', 'working', 'revision_requested'):
            return {'state': run['state'], 'instruction': 'No work pending. END YOUR TURN. Do not poll.'}
        generation = run['generation']
        if run.get('acknowledged_generation') != generation:
            run['acknowledged_generation'] = generation
            if generation == 0:
                run.update(state='working', initial_delivery='acknowledged', launch_state='ready')
            else:
                run['delivery'] = 'acknowledged'
            save(db, run, 'assignment_acknowledged', {'generation': generation})
        prompt = (run['assignment_prompt'] if generation == 0 else
                  'Revise the previous artifact: ' + run['feedback'][-1]['reason'] + '\n' + submission_instructions(generation))
        return {'assignment_id': f'{rid}:{generation}', 'generation': generation, 'instruction': prompt}


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
            'After submission END YOUR TURN and leave the CLI open. Manager feedback arrives as a new '
            'user message. Do not poll, sleep, call agent-wait tools, or search for other agents.')


def assignment_prompt(run):
    return ('[external-model-orchestrator child]\nDo the assigned work directly. Do not spawn agents. '
            'Read the skill at ' + run['config']['skill'] + ' completely.\nTask: ' + run['task'] +
            '\nProject root: ' + run['project'] +
            '\nWork read-only. Do not write project files. You do not approve your own work. '
            'Use native file and web tools, not shell commands. If a web fetch is incomplete, '
            'report that limitation in the packet instead of trying shell pipelines.\n' +
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


def service(db, root, rid):
    """One supervision tick. No arbitrary prompt approval or automatic artifact acceptance."""
    run = get(db, rid)
    if run['state'] == 'accepted':
        return close(db, root, rid)
    if run['state'] == 'starting' and run.get('launch_state') == 'shell_not_ready':
        if run.get('shell_retries', 0) >= 3:
            raise Failure('Herdr shell did not become ready after three refused launches; inspect the saved pane')
        if time.time() < run.get('shell_retry_after', 0):
            return public(run)
        bare_pane(run, run['pane'])
        return launch(db, root, rid, retry=True)
    identity = herdr('agent', 'get', run['agent'])['result']['agent']
    if (identity.get('pane_id') == run['pane'] and identity.get('agent') is None
            and identity.get('launch_pending') and run['state'] == 'starting'):
        return public(run)
    identity = worker_identity(run)
    screen = herdr('agent', 'read', run['agent'], '--source', 'detection', '--lines', '120')['text']
    action = approvals.decision(run, screen)
    if action:
        # Recheck both occupant and unchanged menu immediately before typing.
        worker_identity(run)
        current_screen = herdr('agent', 'read', run['agent'], '--source', 'detection', '--lines', '120')['text']
        if approvals.decision(run, current_screen) != action:
            raise Failure('Approval menu changed; inspect again')
        herdr('agent', 'send-keys', run['agent'], *action[1])
        with db:
            db.execute('INSERT INTO events(run,kind,detail,created) VALUES (?,?,?,?)',
                       (rid, 'routine_approval', json.dumps({'kind': action[0]}), time.time()))
        return public(get(db, rid))
    if run['state'] == 'starting' and run.get('bootstrap_text'):
        phase = run.get('bootstrap_phase')
        if phase == 'mcp_open':
            if ('MCP Servers' in screen and 'submission' in screen and
                    'get_assignment' in screen and 'submit_artifact' in screen):
                herdr('agent', 'send-keys', run['agent'], 'esc')
                with db:
                    current = get(db, rid)
                    current['bootstrap_phase'] = 'mcp_ready'
                    save(db, current, 'mcp_panel_verified')
            return public(get(db, rid))
        if (phase in ('pending', 'mcp_ready') and time.time() >= run.get('bootstrap_after', 0)
                and identity.get('agent_status') in ('idle', 'done')):
            if run['config']['provider'] == 'codex' and not db.execute(
                    "SELECT 1 FROM events WHERE run=? AND kind='mcp_tools_ready' LIMIT 1", (rid,)).fetchone():
                return public(run)
            with db:
                db.execute('BEGIN IMMEDIATE')
                current = get(db, rid)
                if current.get('bootstrap_phase') != phase or current['state'] != 'starting':
                    return public(current)
                open_mcp = phase == 'pending' and run['config']['provider'] == 'agy'
                current['bootstrap_phase'] = 'mcp_open' if open_mcp else 'dispatched'
                current['initial_delivery'] = 'uncertain'
                save(db, current, 'bootstrap_dispatched')
            herdr('agent', 'prompt', run['agent'], '/mcp' if open_mcp else run['bootstrap_text'])
            return public(get(db, rid))
    if (run['state'] == 'revision_requested' and run.get('delivery') == 'pending'
            and identity.get('agent_status') in ('idle', 'done')):
        return deliver(db, rid)
    return public(get(db, rid))


def close(db, root, rid, retry=False):
    run = get(db, rid)
    if run['state'] != 'accepted' or not run['pane']:
        raise Failure('Cleanup requires an accepted run with an owned session')
    if run['cleanup'] == 'confirmed':
        return remove_worker_tab(db, rid)
    active = ('snapshot_in_progress', 'exit_uncertain', 'exit_requested')
    if run['cleanup'] in active:
        run = confirm_close(db, rid)
        if run['cleanup'] == 'confirmed' or not retry:
            return public(run)
    identity = worker_identity(run)
    if identity.get('agent_status') in ('working', 'blocked'):
        # Submission can commit before the provider has finished its turn.
        # In particular Grok cannot snapshot alternate-screen history while busy.
        return public(run)
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
        interrupt, exit_command = providers.exit_controls(run['config'].get('provider', 'claude'))
        herdr('agent', 'send-keys', run['agent'], interrupt)
        herdr('agent', 'prompt', run['agent'], exit_command)
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
    return remove_worker_tab(db, rid) if run['cleanup'] == 'confirmed' else public(run)


def remove_worker_tab(db, rid):
    run = get(db, rid)
    if not run.get('tab') or run.get('tab_removed'):
        return public(run)
    # Never close a tab that has acquired another pane or another occupant.
    try:
        tab = herdr('tab', 'get', run['tab'])['result']['tab']
    except Failure as error:
        try:
            absent = json.loads(str(error)).get('error', {}).get('code') == 'tab_not_found'
        except ValueError:
            absent = False
        if not absent:
            raise
    else:
        pane = herdr('pane', 'get', run['pane'])['result']['pane']
        if (tab.get('pane_count') != 1 or pane.get('tab_id') != run['tab']
                or pane.get('agent') is not None):
            raise Failure('Worker tab changed; leaving it open for inspection')
        herdr('tab', 'close', run['tab'])
    with db:
        run['tab_removed'] = True
        save(db, run, 'worker_tab_removed')
    return public(run)


def worker_server(db, rid, token):
    """Newline-delimited MCP transport with scoped assignment and submission tools."""
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
                with db:
                    db.execute('INSERT INTO events(run,kind,detail,created) VALUES (?,?,?,?)',
                               (rid, 'mcp_tools_ready', '{}', time.time()))
                result = {'tools': [{'name': 'get_assignment', 'description': 'Acknowledge and retrieve your assignment. Call once when starting or when manager sends revision notice. Not a polling tool.',
                          'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
                         {'name': 'submit_artifact', 'description': 'Durably submit candidate JSON and notify manager. Then END YOUR TURN; do not poll or wait on agents.',
                          'inputSchema': {'type': 'object', 'properties': {
                              'packet': {'type': 'string'},
                              'operation_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                              'generation': {'type': 'integer', 'minimum': 0}},
                              'required': ['packet', 'operation_id', 'generation'], 'additionalProperties': False}}]}
            elif method == 'tools/call':
                try:
                    if req['params']['name'] not in ('submit_artifact', 'get_assignment'):
                        raise Failure('Unknown tool')
                    arguments = req['params']['arguments']
                    receipt = (get_assignment(db, rid, token) if req['params']['name'] == 'get_assignment' else
                               submit(db, rid, token, arguments['packet'], arguments['operation_id'], arguments['generation']))
                    result = {'content': [{'type': 'text', 'text': json.dumps(receipt)}]}
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
    c.add_argument('--provider', choices=providers.NAMES); c.add_argument('--model'); c.add_argument('--label')
    c = sub.add_parser('workspace'); c.add_argument('--label', default='Agent Manager')
    c.add_argument('--project', default=os.getcwd()); c.add_argument('--adopt'); c.add_argument('--retry-create', action='store_true')
    for name in ('status', 'inspect', 'service', 'start', 'recover-start', 'launch', 'assign', 'validate', 'revise', 'deliver', 'accept', 'artifact', 'close', 'worker-server'):
        c = sub.add_parser(name); c.add_argument('run')
        if name in ('accept', 'artifact', 'revise'): c.add_argument('--version', type=int, required=True)
        if name in ('accept', 'revise'): c.add_argument('--reason', required=True)
        if name == 'start': c.add_argument('--direction', choices=['right', 'down'], help='Legacy split override; default is a worker tab in the manager workspace')
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
        if a == 'create': result = create(db, args.config, args.task, args.provider, args.model, args.label)
        elif a == 'workspace': result = manager_workspace(db, args.project, args.label, args.adopt, args.retry_create)
        elif a == 'list': result = [public(json.loads(r[0])) for r in db.execute('SELECT data FROM runs')]
        elif a == 'status': result = public(get(db, args.run))
        elif a == 'inspect': result = inspect_worker(db, args.run)
        elif a == 'service': result = service(db, args.root, args.run)
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
