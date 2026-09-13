"""Recognize narrowly scoped native approval menus. Unknown menus stay blocked."""
from pathlib import Path
import re
from urllib.parse import urlparse


def decision(run, screen):
    provider = run['config']['provider']
    grants = run['config'].get('approvals', {})
    # Match the native menu at the bottom, not arbitrary earlier transcript text.
    tail = screen[-4500:]
    if provider == 'agy' and grants.get('workspace_trust'):
        match = re.search(r'Accessing workspace:\n\n([^\n]+)\n\nDo you trust the contents of this project\?', tail)
        if (match and match[1] == run.get('worker_cwd', run['project']) and
                '> Yes, I trust this folder\n  No, exit' in tail and 'enter Confirm' in tail):
            return ('workspace_trust', ('enter',))
    if provider == 'grok' and grants.get('workspace_trust'):
        lines = [line.strip() for line in screen.splitlines()]
        marker = 'Do you trust the contents of this directory?'
        if marker in lines:
            index = lines.index(marker)
            if (index + 1 < len(lines) and lines[index + 1] == run.get('worker_cwd', run['project']) and
                    any(re.fullmatch(r'Yes, proceed\s+y', line) for line in lines[index:]) and
                    any(re.fullmatch(r'No, quit\s+n', line) for line in lines[index:])):
                return ('workspace_trust', ('y',))
    if provider == 'codex' and 'Press enter to continue' in tail:
        match = re.search(r'^> You are in (.+)\n', tail, re.M)
        if (match and grants.get('workspace_trust') and
                Path(match[1]).resolve() == Path(run.get('worker_cwd', run['project'])).resolve() and
                '1. Yes, continue' in tail and '2. No, quit' in tail):
            return ('workspace_trust', ('enter',))
    if provider == 'codex' and 'enter to submit | esc to cancel' in tail:
        for tool in ('get_assignment', 'submit_artifact'):
            if (f'Allow the submission MCP server to run tool "{tool}"?' in tail and
                    (tool == 'get_assignment' or grants.get('artifact_submission')) and
                    '› 1. Allow ' in tail and '2. Allow for this session' in tail):
                return ('scoped_mcp', ('down', 'enter'))
    if provider == 'agy' and 'esc to cancel' in tail:
        # A header-only fetch is equivalent to an authorized source read. Never
        # match redirection, shell operators, arbitrary curl flags, or a bypass.
        command_menu = tail.rsplit('\nCommand\n', 1)[-1]
        header = re.search(r'Requesting permission for:\n   curl -sI (https://[A-Za-z0-9._/-]+)\n\nRun this command\?\n> 1. Yes, run command', command_menu)
        if header and urlparse(header[1]).hostname in run['config'].get('read_hosts', []):
            return ('configured_source_headers', ('enter',))
        menu = tail.rsplit('\nMCP\n', 1)[-1]
        for tool in ('get_assignment', 'submit_artifact'):
            if (f'\nsubmission/{tool}\n\nAllow calling this tool?\n> 1. Yes, allow tool call' in menu and
                    (tool == 'get_assignment' or grants.get('artifact_submission')) and
                    f"2. Yes, and always allow tool 'submission/{tool}' in this conversation" in menu):
                return ('scoped_mcp', ('down', 'enter'))
        menu = tail.rsplit('\nFile access\n', 1)[-1]
        match = re.search(r'\nRead: (.+)\nReason: outside workspace\n', menu)
        if (match and Path(match[1]).resolve() == Path(run['config']['skill']).resolve() and
                'Allow access to this file?\n> 1. Yes, allow access' in menu):
            return ('assigned_skill_read', ('enter',))
        menu = tail.rsplit('\nRead URL\n', 1)[-1]
        match = re.search(r'\n(https://[^\s]+)\n\nAllow access to this URL\?\n> 1. Yes, allow access', menu)
        if match and urlparse(match[1]).hostname in run['config'].get('read_hosts', []):
            return ('configured_source_read', ('enter',))
    if provider == 'grok' and '1/5:select' in tail:
        match = re.search(r'Allow Fetch: (https://[^\s]+)\?', tail)
        if (match and urlparse(match[1]).hostname in run['config'].get('read_hosts', []) and
                '3 (○) Yes, allow once' in tail):
            return ('configured_source_read', ('3', 'enter'))
    return None
