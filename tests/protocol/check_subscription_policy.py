"""Offline regression through the installed Router. No plugin imports or auth state."""
import os
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
import sys

def offline(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.bind', 'socket.sendto',
                 'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
        raise PermissionError('Offline subscription policy regression')
sys.addaudithook(offline)

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import litellm
from litellm import Router
from litellm.llms.chatgpt.authenticator import Authenticator
from litellm.types.router import Deployment

from litellm import woow_subscription_policy as helper

class SubscriptionPolicyContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = Path(self.tmp.name)/'policy'
        self.policy.write_text('disabled\n')
        self.enterContext(patch.object(helper, 'POLICY_PATH', self.policy))
        self.auth_init = self.enterContext(patch.object(Authenticator, '__init__', return_value=None))
        self.enterContext(patch.object(Authenticator, '_read_auth_file', return_value=None))
        self.login = self.enterContext(patch.object(Authenticator, '_login_device_code',
            return_value={'access_token': 'synthetic-no-network-token'}))
        self.enterContext(patch.object(Authenticator, 'get_api_base', return_value='https://offline.invalid'))
        self.enterContext(patch.object(Authenticator, '_write_auth_file', side_effect=AssertionError('auth write forbidden')))
        self.models = [self.model(i) for i in range(17)]
        self.saved = copy.deepcopy(self.models)

    @staticmethod
    def model(i, provider='chatgpt'):
        return {'model_name': f'public-{i}', 'litellm_params': {
            'model': f'{provider}/gpt-5', 'api_key': f'woow-chatgpt:synthetic-{i % 2}' if provider == 'chatgpt' else 'synthetic-key'},
            'model_info': {'id': f'fixture-{i}', 'db_model': True}}

    def test_disabled_initial_load_preserves_input_and_other_provider(self):
        other = self.model(99, 'openai')
        # A display alias resembling ChatGPT is not sufficient to suppress BYOK.
        other['model_name'] = 'chatgpt/display-only'
        router = Router(model_list=self.models+[other], ignore_invalid_deployments=True)
        self.assertEqual(self.login.call_count, 0)
        self.assertEqual(self.auth_init.call_count, 0)
        self.assertEqual([m['model_name'] for m in router.model_list], ['chatgpt/display-only'])
        self.assertEqual(router.model_names, {'chatgpt/display-only'})
        self.assertEqual(self.models, self.saved)
        self.assertFalse(router._provider_unresolved_deployments)

    def test_disabled_db_periodic_upsert_and_admin_add(self):
        router = Router(model_list=[])
        for model in self.models:
            self.assertIsNone(router.upsert_deployment(Deployment(**model)))
            self.assertIsNone(router.add_deployment(Deployment(**model)))
        self.assertEqual(self.login.call_count, 0)
        self.assertEqual(self.auth_init.call_count, 0)
        self.assertEqual(router.model_list, [])
        self.assertFalse(router.model_names)
        self.assertEqual(self.models, self.saved)
        self.assertIsNotNone(router.upsert_deployment(Deployment(**self.model(99, 'openai'))))

    def test_enabled_restart_loads_retained_models(self):
        self.policy.write_text('enabled\n')
        router = Router(model_list=self.models, ignore_invalid_deployments=True)
        self.assertEqual(len(router.model_list), 17)
        self.assertGreater(self.login.call_count, 0)  # unmodified upstream enabled path
        self.assertEqual(self.models, self.saved)

    def test_missing_and_invalid_policy_fail_closed(self):
        for value in (None, 'true\n', 'enabled', ''):
            with self.subTest(value=value):
                if value is None:
                    self.policy.unlink(missing_ok=True)
                else:
                    self.policy.write_text(value)
                self.assertEqual(Router(model_list=self.models).model_list, [])
        self.assertEqual(self.login.call_count, 0)
        self.assertEqual(self.auth_init.call_count, 0)

    def test_explicit_provider_and_environment_model(self):
        variants = [
            {'model': 'gpt-5', 'custom_llm_provider': 'chatgpt'},
            {'model': 'os.environ/FIXTURE_MODEL'},
            {'model': 'gpt-5', 'custom_llm_provider': 'os.environ/FIXTURE_PROVIDER'},
        ]
        with patch.dict(os.environ, {'FIXTURE_MODEL':'chatgpt/gpt-5', 'FIXTURE_PROVIDER':'chatgpt'}):
            for params in variants:
                with self.subTest(params=params):
                    model = self.model(0)
                    model['litellm_params'].update(params)
                    self.assertEqual(Router(model_list=[model]).model_list, [])
        self.assertEqual(self.login.call_count, 0)
        self.assertEqual(self.auth_init.call_count, 0)

if __name__ == '__main__':
    unittest.main(verbosity=2)
