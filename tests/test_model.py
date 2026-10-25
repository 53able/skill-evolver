import io
import json
import os
import unittest
from unittest.mock import patch
import urllib.error

from skill_evolver.model import ENDPOINT, MAX_REQUEST_BYTES, ModelError, OpenAIJSONModel, _NoRedirect


def config():
    return {'provider': 'openai', 'id': 'configured-model', 'options': {},
            'api_key_env': 'SKILL_EVOLVER_TEST_KEY', 'count_fee_acknowledged': True}


def generated(text='{"finish":true}', usage=None, status='completed'):
    return {'status': status, 'usage': usage or {'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10},
            'output': [{'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': text}]}]}


class Response(io.BytesIO):
    status = 200
    def __init__(self, payload, url):
        super().__init__(json.dumps(payload).encode() if not isinstance(payload, bytes) else payload)
        self.url = url
    def geturl(self):
        return self.url


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'SKILL_EVOLVER_TEST_KEY': 'fake-test-key'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.opener_patch = patch('skill_evolver.model.urllib.request.build_opener')
        self.opener = self.opener_patch.start().return_value
        self.addCleanup(self.opener_patch.stop)

    def responses(self, *items):
        self.opener.open.side_effect = [Response(item, ENDPOINT + '/input_tokens' if i == 0 else ENDPOINT)
                                      for i, item in enumerate(items)]

    def complete(self, **changes):
        args = {'max_input_tokens': 20, 'max_output_tokens': 10, 'timeout_seconds': 2}
        args.update(changes)
        return OpenAIJSONModel(config()).complete({'envelope': 'return JSON action'}, **args)

    def test_exact_preflight_request_and_actual_usage(self):
        self.responses({'input_tokens': 7}, generated())
        self.assertEqual(self.complete(), {'content': {'finish': True}, 'input_tokens': 7,
                                           'output_tokens': 3, 'tokens': 10})
        count, infer = self.opener.open.call_args_list
        first, second = count.args[0], infer.args[0]
        self.assertEqual(first.full_url, ENDPOINT + '/input_tokens')
        self.assertEqual(second.full_url, ENDPOINT)
        self.assertEqual(first.data, second.data)
        body = json.loads(second.data)
        self.assertEqual(body['model'], 'configured-model')
        self.assertEqual(body['text'], {'format': {'type': 'json_object'}})
        self.assertFalse(body['stream'])
        self.assertFalse(body['store'])
        self.assertEqual(set(body), {'model', 'input', 'text', 'max_output_tokens', 'stream', 'store'})
        self.assertIn('JSON', body['input'][0]['content'])
        self.assertEqual(second.get_header('Authorization'), 'Bearer fake-test-key')

    def test_preflight_no_generation(self):
        for count in (True, -1, 1.5, None, 21):
            self.opener.open.reset_mock()
            self.responses({'input_tokens': count})
            with self.subTest(count=count), self.assertRaises(ModelError) as error:
                self.complete()
            self.assertEqual(error.exception.tokens, 0)
            self.assertEqual(self.opener.open.call_count, 1)

    def test_known_usage_retained_on_bad_content(self):
        for text in ('bad', '[]', '{"x":NaN}', '{"x":1,"x":2}'):
            self.responses({'input_tokens': 7}, generated(text))
            with self.subTest(text=text), self.assertRaises(ModelError) as error:
                self.complete()
            self.assertEqual(error.exception.tokens, 10)
        for body in (generated(status='incomplete'), generated()):
            if body['status'] == 'completed':
                body['output'][0]['content'] = [{'type': 'refusal', 'refusal': 'no'}]
            self.responses({'input_tokens': 7}, body)
            with self.assertRaises(ModelError) as error:
                self.complete()
            self.assertEqual(error.exception.tokens, 10)

    def test_unknown_and_excess_usage_retain_reservation(self):
        for usage in ({}, {'input_tokens': True, 'output_tokens': 3, 'total_tokens': 4},
                      {'input_tokens': 7.0, 'output_tokens': 3, 'total_tokens': 10},
                      {'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 9},
                      {'input_tokens': 21, 'output_tokens': 3, 'total_tokens': 24},
                      {'input_tokens': 7, 'output_tokens': 11, 'total_tokens': 18}):
            body = generated()
            body['usage'] = usage
            self.responses({'input_tokens': 7}, body)
            with self.subTest(usage=usage), self.assertRaises(ModelError) as error:
                self.complete()
            self.assertIsNone(error.exception.tokens)

    def test_reasoning_usage_not_double_counted(self):
        body = generated()
        body['usage']['output_tokens_details'] = {'reasoning_tokens': 2}
        body['output'].insert(0, {'type': 'reasoning', 'summary': []})
        self.responses({'input_tokens': 7}, body)
        self.assertEqual(self.complete()['tokens'], 10)

    def test_local_validation_and_secrets(self):
        for changes in ({'options': {'temperature': 0}}, {'api_key_env': ''},
                        {'count_fee_acknowledged': False}, {'base_url': 'https://evil.test'}):
            c = config()
            c.update(changes)
            with self.assertRaises(ModelError) as error:
                OpenAIJSONModel(c)
            self.assertEqual(error.exception.tokens, 0)
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ModelError) as error:
            self.complete()
        self.assertEqual(error.exception.tokens, 0)
        self.opener.open.assert_not_called()
        with self.assertRaises(ModelError) as error:
            OpenAIJSONModel(config()).complete({'x': 'x' * MAX_REQUEST_BYTES}, max_input_tokens=20,
                                              max_output_tokens=10, timeout_seconds=2)
        self.assertEqual(error.exception.tokens, 0)
        self.opener.open.assert_not_called()

    def test_transport_no_retry_no_secret_error(self):
        http_error = urllib.error.HTTPError(ENDPOINT, 401, 'fake-test-key', {}, None)
        self.addCleanup(http_error.close)
        for failure in (TimeoutError('fake-test-key'), http_error):
            self.opener.open.reset_mock()
            self.opener.open.side_effect = failure
            with self.assertRaises(ModelError) as error:
                self.complete()
            self.assertIsNone(error.exception.tokens)
            self.assertNotIn('fake-test-key', str(error.exception))
            self.assertEqual(self.opener.open.call_count, 1)

    def test_redirect_and_byte_bounds(self):
        with self.assertRaises(ModelError):
            _NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test')
        self.responses({'input_tokens': 7}, b'x' * 11)
        with patch('skill_evolver.model.MAX_RESPONSE_BYTES', 10), self.assertRaises(ModelError) as error:
            self.complete()
        self.assertIsNone(error.exception.tokens)

    def test_wall_timeout_interrupts_transport(self):
        import time
        self.opener.open.side_effect = lambda *args, **kwargs: time.sleep(1)
        started = time.monotonic()
        with self.assertRaises(ModelError) as error:
            self.complete(timeout_seconds=.02)
        self.assertIsNone(error.exception.tokens)
        self.assertLess(time.monotonic() - started, .5)

    def test_shared_deadline(self):
        self.responses({'input_tokens': 7}, generated())
        with patch('skill_evolver.model.time.monotonic', side_effect=[0, 0, 0, 3]):
            with self.assertRaises(ModelError) as error:
                self.complete()
        self.assertIsNone(error.exception.tokens)
        self.assertEqual(self.opener.open.call_count, 1)
