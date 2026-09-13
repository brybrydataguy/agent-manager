"""Repo-local subscription preflight. Never prints credentials or CLI diagnostics."""
import argparse
import json
import os
import re
import subprocess
import sys

API_ENV = (
    'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'CLAUDE_API_KEY',
    'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_USE_VERTEX',
    'XAI_API_KEY', 'XAI_BASE_URL', 'GROK_API_KEY', 'GROK_BASE_URL',
    'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS',
    'GOOGLE_GENAI_USE_VERTEXAI', 'OPENAI_API_KEY', 'CODEX_API_KEY',
    'OPENAI_BASE_URL', 'OPENAI_API_BASE', 'AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_ENDPOINT',
)
COMMANDS = {'claude': ['claude', 'auth', 'status', '--json'],
            'grok': ['grok', 'models'], 'agy': ['agy', 'models'],
            'codex': ['codex', 'login', 'status']}


class AuthError(Exception):
    pass


def check(provider):
    if provider not in COMMANDS:
        raise AuthError('Unsupported provider; choose claude, grok, codex, or agy')
    blocked = [name for name in API_ENV if os.environ.get(name)]
    if blocked:
        raise AuthError('API billing/routing is disabled. Unset: ' + ', '.join(blocked))
    try:
        result = subprocess.run(COMMANDS[provider], capture_output=True, text=True,
                                encoding='utf-8', timeout=30)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise AuthError(provider + ': authentication check unavailable; install/sign in to its CLI') from error
    output = result.stdout + '\n' + result.stderr
    valid = False
    if result.returncode == 0:
        if provider == 'claude':
            try:
                status = json.loads(result.stdout)
                valid = (isinstance(status, dict) and status.get('loggedIn') is True and
                         status.get('authMethod') == 'oauth_token' and status.get('apiProvider') == 'firstParty')
            except ValueError:
                pass
        elif provider == 'codex':
            valid = 'logged in using chatgpt' in output.lower()
        elif provider == 'agy':
            valid = bool(re.search(r'^gemini-[\w.-]+', output, re.MULTILINE))
        else:
            valid = bool(re.search(r'\bgrok-[\w.-]+', output))
        if re.search(r'not authenticated|no auth credentials|api.?key', output, re.I):
            valid = False
    if not valid:
        raise AuthError(provider + ': subscription authentication not established; sign in with OAuth and retry')
    return {'provider': provider, 'ready': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', required=True, choices=[*COMMANDS, 'all'])
    args = parser.parse_args()
    failed = False
    for provider in COMMANDS if args.check == 'all' else [args.check]:
        try:
            print(json.dumps(check(provider)))
        except AuthError as error:
            print(str(error), file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
