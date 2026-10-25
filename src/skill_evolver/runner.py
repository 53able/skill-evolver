"""Host-side, bounded agent loop; credentials never enter Docker."""
from __future__ import annotations

import copy
import json
import math
import shlex
import time
from collections.abc import Callable

from .model import ModelError, OpenAIJSONModel
from .sandbox import ArtifactError, DockerSandbox, SandboxError, relative_path, resolve_image, recover_allocation, copy_event

PUBLIC_CASE_FIELDS = ('id', 'group_id', 'prompt', 'files', 'output_paths', 'operation')


def reservation_tokens(config: dict) -> int:
    limits = config['limits']
    return limits['max_steps'] * (limits['max_input_tokens'] + limits['max_output_tokens'])


def _size(value):
    # Synthetic mock token unit is one UTF-8 byte, a conservative input envelope.
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8'))


class TrialRunner:
    def __init__(self, config: dict):
        self.config = copy.deepcopy(config)
        self.resource_recorder: Callable[[dict], object] | None = None
        self._pinned_image: str | None = None
        provider = config['model']['provider']
        if provider not in ('mock', 'openai'):
            raise ValueError('unsupported model provider')
        self.model = OpenAIJSONModel(config['model']) if provider == 'openai' else None
        for field in ('max_steps', 'max_input_tokens', 'max_output_tokens'):
            value = config['limits'][field]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError('invalid ' + field)
        for field in ('max_file_bytes', 'max_total_bytes', 'max_files'):
            value = config['sandbox'][field]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError('invalid ' + field)
        for section in ('limits', 'sandbox'):
            if (isinstance(config[section]['timeout_seconds'], bool) or
                    not isinstance(config[section]['timeout_seconds'], (int, float)) or
                    not math.isfinite(config[section]['timeout_seconds']) or
                    config[section]['timeout_seconds'] <= 0):
                raise ValueError('invalid timeout')

    def prepare_environment(self, pinned_image_id: str | None = None) -> str:
        if self._pinned_image is not None:
            if pinned_image_id is not None and pinned_image_id != self._pinned_image:
                raise SandboxError('frozen environment image mismatch')
            return self._pinned_image
        if pinned_image_id is not None:
            import re
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', pinned_image_id):
                raise SandboxError('invalid pinned image ID')
        resolved = resolve_image(pinned_image_id or self.config['sandbox']['image'])
        self._pinned_image = resolved
        self.config['sandbox']['image'] = resolved
        return resolved

    def recover_resources(self, events: list[dict]) -> None:
        allocations = {}
        for event in events:
            kind = event.get('kind')
            if kind == 'resource_allocated':
                resource_id = event.get('resource_id')
                if not isinstance(resource_id, str) or resource_id in allocations:
                    raise SandboxError('duplicate or invalid resource allocation')
                allocations[resource_id] = event
            elif kind == 'resource_released':
                resource_id = event.get('resource_id')
                if not isinstance(resource_id, str):
                    raise SandboxError('invalid resource release identity')
                original = allocations.get(resource_id)
                if original is None or original.get('resources') != event.get('resources'):
                    raise SandboxError('unmatched resource release')
                del allocations[event['resource_id']]
        for event in allocations.values():
            recover_allocation(event)
            if self.resource_recorder:
                released = copy_event(event)
                released['kind'] = 'resource_released'
                self.resource_recorder(released)

    def _model_response(self, public_case, skill_text, transcript, step):
        """Host broker seam. Mock has no answer access and uses real Docker tools."""
        if step == 0:
            return {'tool': 'read_file', 'arguments': {'path': '/SKILL.md'}}
        if step == 1:
            operation = public_case.get('operation', 'uppercase')
            if operation not in ('uppercase', 'copy'):
                return {'finish': True, 'failure': 'unsupported mock operation'}
            files = public_case.get('files', {})
            paths = public_case.get('output_paths', [])
            source = 'input.txt' if 'input.txt' in files else next(iter(files), None)
            if source is None or len(paths) != 1:
                return {'finish': True, 'failure': 'mock requires input and one output path'}
            src = shlex.quote('/input/' + source)
            dst = shlex.quote('/output/' + paths[0])
            command = ('tr \'[:lower:]\' \'[:upper:]\' < ' + src + ' > ' + dst
                       if operation == 'uppercase' else 'cp ' + src + ' ' + dst)
            return {'tool': 'run_command', 'arguments': {'command': command}}
        return {'finish': True}

    @staticmethod
    def _validate_action(response):
        if not isinstance(response, dict):
            raise ValueError('model action must be an object')
        if response.get('finish') is True:
            if set(response) - {'finish', 'failure'} or ('failure' in response and not isinstance(response['failure'], str)):
                raise ValueError('invalid finish action')
            return
        schemas = {'read_file': {'path'}, 'list_files': {'path'},
                   'write_file': {'path', 'content'}, 'run_command': {'command'}}
        name, arguments = response.get('tool'), response.get('arguments')
        if (set(response) != {'tool', 'arguments'} or not isinstance(name, str) or name not in schemas or
                not isinstance(arguments, dict) or set(arguments) != schemas[name] or
                not all(isinstance(value, str) for value in arguments.values())):
            raise ValueError('invalid model tool action')

    def run(self, skill_text: str, case: dict, *, trial_id: str) -> dict:
        started = time.monotonic()
        result: dict = {'status': 'infrastructure_error', 'outputs': {}, 'tokens': 0,
                  'duration_seconds': 0.0, 'termination_reason': '',
                  'model': {k: copy.deepcopy(self.config['model'].get(k)) for k in ('provider', 'id', 'options')},
                  'environment_hash': '', 'skill_loaded': False, 'transcript': []}
        sandbox = None
        stage = 'execution'

        def failure(status, reason, *, error_stage):
            if result['tokens'] is None:
                result.setdefault('secondary_errors', []).append({'stage': error_stage, 'reason': reason})
                result['outputs'] = {}
            else:
                result.update(status=status, termination_reason=reason)

        try:
            # Never copy or serialize case.expected, even temporarily.
            public = {k: copy.deepcopy(case[k]) for k in PUBLIC_CASE_FIELDS if k in case}
            for path, content in public.get('files', {}).items():
                relative_path(path)
                if not isinstance(content, str):
                    raise ValueError('input content must be text')
            for path in public.get('output_paths', []):
                relative_path(path)
            if not isinstance(skill_text, str):
                raise ValueError('skill must be text')
            limits = self.config['limits']
            if self.model is None and _size({'case': public, 'skill': skill_text, 'transcript': []}) > limits['max_input_tokens']:
                raise ValueError('model input envelope exceeded')
            self.prepare_environment()
            sandbox_config = copy.deepcopy(self.config['sandbox'])
            sandbox_config['timeout_seconds'] = min(sandbox_config['timeout_seconds'], limits['timeout_seconds'])
            sandbox = DockerSandbox(sandbox_config, skill_text, public.get('files', {}),
                                    trial_id=trial_id, resource_recorder=self.resource_recorder)
            sandbox.start()
            result['environment_hash'] = sandbox.environment_hash
            deadline = min(started + limits['timeout_seconds'], sandbox.deadline)
            sandbox.deadline = deadline
            for step in range(limits['max_steps']):
                if time.monotonic() >= deadline:
                    raise TimeoutError('trial deadline exceeded')
                request = {'case': public, 'skill': skill_text, 'transcript': result['transcript']}
                if self.model is None:
                    input_tokens = _size(request)
                    if input_tokens > limits['max_input_tokens']:
                        result.update(status='task_failure', termination_reason='max_input_tokens')
                        break
                    response = self._model_response(public, skill_text, copy.deepcopy(result['transcript']), step)
                    output_tokens = _size(response)
                    if output_tokens > limits['max_output_tokens']:
                        result.update(status='task_failure', termination_reason='max_output_tokens')
                        break
                    result['tokens'] += input_tokens + output_tokens
                else:
                    request['envelope'] = {
                        'actions': 'Return {"tool":name,"arguments":object} OR {"finish":true,"failure":optional string}.',
                        'tools': {'read_file': {'path': 'string'}, 'list_files': {'path': 'string'},
                                  'write_file': {'path': 'string', 'content': 'string'},
                                  'run_command': {'command': 'string'}},
                        'instructions': 'Read /SKILL.md first using read_file. Inputs are under /input; write final artifacts under /output.'}
                    try:
                        completion = self.model.complete(request, max_input_tokens=limits['max_input_tokens'],
                                                         max_output_tokens=limits['max_output_tokens'],
                                                         timeout_seconds=deadline - time.monotonic())
                    except ModelError as exc:
                        if exc.tokens is None:
                            result['tokens'] = None
                        else:
                            result['tokens'] += exc.tokens
                        result.update(status='indeterminate' if exc.tokens is None else 'task_failure',
                                      termination_reason=str(exc))
                        break
                    result['tokens'] += completion['tokens']
                    response = completion['content']
                self._validate_action(response)
                if response.get('finish'):
                    result.update(status='task_failure' if response.get('failure') else 'success',
                                  termination_reason=response.get('failure', 'completed'))
                    break
                name, arguments = response['tool'], response['arguments']
                try:
                    observation = sandbox.tool(name, arguments)
                except SandboxError as exc:
                    result['transcript'].append({'tool': name, 'arguments': arguments, 'error': str(exc)})
                    result.update(status='task_failure', termination_reason='tool_failure')
                    break
                result['transcript'].append({'tool': name, 'arguments': arguments, 'result': observation})
                if name == 'read_file' and arguments.get('path') == '/SKILL.md' and observation == skill_text:
                    result['skill_loaded'] = True
            else:
                result.update(status='task_failure', termination_reason='max_steps')
            stage = 'stop'
            sandbox.stop()
            if result['status'] == 'success':
                stage = 'collection'
                result['outputs'] = sandbox.collect(public.get('output_paths', []))
        except TimeoutError as exc:
            failure('timeout', str(exc) if result['tokens'] is None else 'timeout', error_stage=stage)
        except ArtifactError as exc:
            failure('task_failure', str(exc), error_stage=stage)
        except ValueError as exc:
            failure('task_failure', str(exc), error_stage=stage)
        except (SandboxError, OSError) as exc:
            failure('infrastructure_error', str(exc), error_stage=stage)
        finally:
            if sandbox:
                try:
                    sandbox.close()
                except (SandboxError, OSError, TimeoutError, ValueError) as exc:
                    failure('infrastructure_error', str(exc) if result['tokens'] is None else 'sandbox cleanup failed',
                            error_stage='cleanup')
                    result['outputs'] = {}
            # Unknown inference cannot be resolved by a secondary error or another attempt.
            if result['tokens'] is None:
                result['status'] = 'indeterminate'
                result['outputs'] = {}
            result['duration_seconds'] = time.monotonic() - started
        return result
