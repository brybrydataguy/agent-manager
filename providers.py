"""Provider-specific interactive launch configuration, never global config edits."""
import json
from pathlib import Path

NAMES = ('claude', 'grok', 'codex', 'agy')


def prepare(directory, provider, server):
    """Return the worker cwd override, if project-scoped config needs isolation."""
    directory = Path(directory)
    mcp = {'mcpServers': {'submission': server}}
    private_write(directory / 'mcp.json', json.dumps(mcp))
    if provider not in ('grok', 'agy'):
        return None
    workspace = directory / 'workspace'
    workspace.mkdir(mode=0o700, exist_ok=True)
    if provider == 'grok':
        private_write(workspace / '.grok/config.toml',
                      '[mcp_servers.submission]\ncommand = ' + json.dumps(server['command']) +
                      '\nargs = ' + json.dumps(server['args']) + '\nenabled = true\n')
    else:
        private_write(workspace / '.agents/mcp_config.json', json.dumps(mcp))
    return str(workspace)


def private_write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    path.chmod(0o600)


def arguments(provider, directory, model=None, bootstrap=None, approvals=None):
    path = Path(directory) / 'mcp.json'
    if provider == 'claude':
        reads = 'Read,Glob,Grep,WebFetch,WebSearch'
        args = ['--permission-mode', 'default', '--strict-mcp-config', '--mcp-config', str(path),
                '--tools', reads, '--allowedTools', reads + ',mcp__submission__get_assignment' +
                (',mcp__submission__submit_artifact' if (approvals or {}).get('artifact_submission', False) else '')]
    elif provider == 'grok':
        args = ['--permission-mode', 'default', '--no-plan', '--no-subagents',
                '--tools', 'read_file,list_dir,grep,web_search',
                '--allow', 'submission__get_assignment']
        if (approvals or {}).get('artifact_submission', False):
            args += ['--allow', 'submission__submit_artifact']
    elif provider == 'codex':
        server = json.loads(path.read_text(encoding='utf-8'))['mcpServers']['submission']
        args = ['--sandbox', 'read-only', '--ask-for-approval', 'on-request', '--search',
                '-c', 'mcp_servers.submission.command=' + json.dumps(server['command']),
                '-c', 'mcp_servers.submission.args=' + json.dumps(server['args']),
                '-c', 'mcp_servers.submission.required=true', '-c', 'agents.enabled=false',
                '-c', 'mcp_servers.submission.tools.get_assignment.approval_mode="approve"']
        if (approvals or {}).get('artifact_submission', False):
            args += ['-c', 'mcp_servers.submission.tools.submit_artifact.approval_mode="approve"']
    elif provider == 'agy':
        args = ['--mode', 'accept-edits', '--sandbox']
    else:
        raise ValueError('Unsupported provider: ' + str(provider))
    if model:
        args += ['--model', model]
    if bootstrap:
        args += ['--prompt-interactive', bootstrap] if provider == 'agy' else [bootstrap]
    return args


def exit_controls(provider):
    return ('ctrl+c' if provider in ('grok', 'agy') else 'esc',
            '/quit' if provider == 'codex' else '/exit')
