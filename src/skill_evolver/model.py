"""Fixed-host, no-retry OpenAI Responses JSON broker (stdlib only)."""
from __future__ import annotations

import json
import math
import os
import signal
import threading
import time
import urllib.error
import urllib.request

ENDPOINT = 'https://api.openai.com/v1/responses'
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEVELOPER_INSTRUCTION = ('Return exactly one JSON object matching the requested envelope in the user context. '
                         'Do not include Markdown or text outside the JSON object.')


class ModelError(RuntimeError):
    def __init__(self, message, tokens=None):
        super().__init__(message)
        self.tokens = tokens


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelError('model HTTP redirect rejected', tokens=None)


def _integer(value):
    return type(value) is int and value >= 0


def _json_loads(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('nonfinite JSON value')
    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)


class OpenAIJSONModel:
    def __init__(self, model_config):
        if model_config.get('provider') != 'openai':
            raise ModelError('provider must be openai', tokens=0)
        if not isinstance(model_config.get('id'), str) or not model_config['id']:
            raise ModelError('model id required', tokens=0)
        if not isinstance(model_config.get('api_key_env'), str) or not model_config['api_key_env']:
            raise ModelError('api_key_env required', tokens=0)
        if model_config.get('options', {}) != {}:
            raise ModelError('model options are not supported', tokens=0)
        if model_config.get('count_fee_acknowledged') is not True:
            raise ModelError('count_fee_acknowledged=true required; token ledger is not an invoice cap', tokens=0)
        if any(k in model_config for k in ('endpoint', 'base_url', 'api_key', 'tools', 'conversation', 'previous_response_id')):
            raise ModelError('custom endpoint, credentials or conversation unsupported', tokens=0)
        self.model_id = model_config['id']
        self.api_key_env = model_config['api_key_env']

    def _post(self, url, payload, key, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModelError('model timeout', tokens=None)
        request = urllib.request.Request(url, data=payload, method='POST',
                                         headers={'Authorization': 'Bearer ' + key,
                                                  'Content-Type': 'application/json'})
        # Ignore ambient proxy configuration; never forward credentials to redirects.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=remaining) as response:
                if response.geturl() != url or response.status != 200:
                    raise ModelError('unexpected model HTTP response', tokens=None)
                chunks = []
                size = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ModelError('model timeout', tokens=None)
                    # HTTPResponse.read1 avoids waiting to fill an entire requested buffer.
                    sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    chunk = getattr(response, 'read1', response.read)(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                    if time.monotonic() > deadline:
                        raise ModelError('model timeout', tokens=None)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ModelError('model response byte limit exceeded', tokens=None)
                parsed = _json_loads(b''.join(chunks))
                if not isinstance(parsed, dict):
                    raise ModelError('invalid model response object', tokens=None)
                return parsed
        except ModelError:
            raise
        except (OSError, ValueError, urllib.error.URLError):
            # Never copy HTTP bodies, headers, keys or request data into errors/logs.
            raise ModelError('model HTTP or JSON failure', tokens=None) from None

    def complete(self, context: dict, *, max_input_tokens: int, max_output_tokens: int,
                 timeout_seconds: float) -> dict:
        # CLI is Unix/main-thread. SIGALRM also bounds DNS/connect and slow reads;
        # urllib's socket timeout alone is not a whole-call wall deadline.
        if (threading.current_thread() is not threading.main_thread() or
                not hasattr(signal, 'setitimer') or signal.getitimer(signal.ITIMER_REAL)[0] != 0):
            raise ModelError('model requires Unix main thread without an active interval timer', tokens=0)
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or
                not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ModelError('invalid model timeout', tokens=0)
        previous = signal.getsignal(signal.SIGALRM)
        def expired(signum, frame):
            raise ModelError('model wall timeout', tokens=None)
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        try:
            return self._complete(context, max_input_tokens=max_input_tokens,
                                  max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def _complete(self, context: dict, *, max_input_tokens: int, max_output_tokens: int,
                  timeout_seconds: float) -> dict:
        if (not isinstance(context, dict) or not _integer(max_input_tokens) or max_input_tokens == 0 or
                not _integer(max_output_tokens) or max_output_tokens == 0 or
                isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or
                not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ModelError('invalid model call bounds', tokens=0)
        deadline = time.monotonic() + timeout_seconds
        try:
            request = {'model': self.model_id,
                       'input': [{'role': 'developer', 'content': DEVELOPER_INSTRUCTION},
                                 {'role': 'user', 'content': json.dumps(context, ensure_ascii=False, allow_nan=False)}],
                       'max_output_tokens': max_output_tokens,
                       'text': {'format': {'type': 'json_object'}}, 'stream': False, 'store': False}
            payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError):
            raise ModelError('context must be JSON serializable', tokens=0) from None
        if len(payload) > MAX_REQUEST_BYTES:
            raise ModelError('model request byte limit exceeded', tokens=0)
        key = os.environ.get(self.api_key_env)
        if not key or '\r' in key or '\n' in key:
            raise ModelError('API key environment variable missing or invalid', tokens=0)
        # The exact same bytes are counted and generated: no hidden conversation state.
        count = self._post(ENDPOINT + '/input_tokens', payload, key, deadline)
        if not _integer(count.get('input_tokens')):
            raise ModelError('invalid input token count', tokens=0)
        if count['input_tokens'] > max_input_tokens:
            raise ModelError('max_input_tokens exceeded', tokens=0)
        response = self._post(ENDPOINT, payload, key, deadline)
        usage = response.get('usage')
        if not isinstance(usage, dict) or not all(_integer(usage.get(k)) for k in ('input_tokens', 'output_tokens', 'total_tokens')):
            raise ModelError('unknown or invalid model usage', tokens=None)
        inputs, outputs, total = (usage[k] for k in ('input_tokens', 'output_tokens', 'total_tokens'))
        if total != inputs + outputs:
            raise ModelError('inconsistent model usage', tokens=None)
        if inputs > max_input_tokens or outputs > max_output_tokens:
            raise ModelError('usage envelope anomaly; retain reservation', tokens=None)
        try:
            if response.get('status') != 'completed':
                raise ValueError('incomplete')
            texts = []
            for item in response['output']:
                if not isinstance(item, dict):
                    raise ValueError('invalid output item')
                if item.get('type') == 'reasoning':
                    continue
                if item.get('type') != 'message' or item.get('role') != 'assistant':
                    raise ValueError('unexpected output item')
                for content in item['content']:
                    if content.get('type') != 'output_text' or not isinstance(content.get('text'), str):
                        raise ValueError('refusal or invalid text')
                    texts.append(content['text'])
            if len(texts) != 1:
                raise ValueError('expected one JSON text')
            content = _json_loads(texts[0])
            if not isinstance(content, dict):
                raise ValueError('expected JSON object')
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ModelError('invalid, incomplete or refused model content', tokens=total) from None
        return {'content': content, 'input_tokens': inputs, 'output_tokens': outputs, 'tokens': total}
