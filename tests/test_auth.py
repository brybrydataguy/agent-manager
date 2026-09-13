import json
import subprocess
import unittest
from unittest.mock import patch

import auth


class Authentication(unittest.TestCase):
    def test_each_provider_success(self):
        replies = {'claude': json.dumps({'loggedIn': True, 'authMethod': 'oauth_token', 'apiProvider': 'firstParty'}),
                   'grok': 'grok-4.6', 'agy': 'gemini-3.8-flash-high', 'codex': 'Logged in using ChatGPT'}
        for provider, reply in replies.items():
            with self.subTest(provider=provider), patch.dict('os.environ', {}, clear=True), patch(
                    'auth.subprocess.run', return_value=subprocess.CompletedProcess([], 0, reply, '')):
                self.assertTrue(auth.check(provider)['ready'])

    def test_billing_environment_rejected_without_running_cli(self):
        for name in auth.API_ENV:
            with patch.dict('os.environ', {name: 'secret-value'}, clear=True), patch('auth.subprocess.run') as run:
                with self.assertRaises(auth.AuthError) as error: auth.check('claude')
                self.assertNotIn('secret-value', str(error.exception))
                run.assert_not_called()

    def test_bad_auth_and_unknown_output_fail_closed(self):
        for provider in auth.COMMANDS:
            for output in ('', 'not authenticated', 'API key: secret-value'):
                with patch.dict('os.environ', {}, clear=True), patch('auth.subprocess.run',
                        return_value=subprocess.CompletedProcess([], 0, output, '')):
                    with self.assertRaises(auth.AuthError) as error: auth.check(provider)
                    self.assertNotIn('secret-value', str(error.exception))

    def test_cli_failure_and_timeout(self):
        with patch.dict('os.environ', {}, clear=True):
            with patch('auth.subprocess.run', return_value=subprocess.CompletedProcess([], 1, 'grok-4.6', '')):
                with self.assertRaises(auth.AuthError): auth.check('grok')
            with patch('auth.subprocess.run', side_effect=subprocess.TimeoutExpired('codex', 30)):
                with self.assertRaises(auth.AuthError): auth.check('codex')
