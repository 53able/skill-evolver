"""Fail-closed JSON experiment contracts."""
import copy
import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath
from .genome import validate_skill


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def relative_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or value == '.':
        raise ValueError('unsafe relative artifact path: ' + value)
    return value


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' must be positive')


def identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value):
        raise ValueError(name + ' must be a safe nonempty string identifier')


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    config['_config_path'] = str(path)
    config['skill_path'] = str((path.parent / config['skill_path']).resolve())
    validate_config(config)
    return config


def _validate_config(config):
    for key in ('experiment_id', 'skill_path', 'model', 'proposal_model', 'sandbox', 'limits', 'budgets', 'splits', 'search'):
        if key not in config:
            raise ValueError('missing required config: ' + key)
    identifier(config['experiment_id'], 'experiment_id')
    for role in ('model', 'proposal_model'):
        model = config[role]
        if model.get('provider') not in ('mock', 'openai') or not isinstance(model.get('id'), str) or not model['id'] or not isinstance(model.get('options'), dict):
            raise ValueError('model requires supported provider, id, and options')
        if model['provider'] == 'openai':
            from .model import OpenAIJSONModel, ModelError
            try:
                OpenAIJSONModel(model)  # Contract validation only; no key read or API call.
            except ModelError as exc:
                raise ValueError(str(exc)) from exc
    for name in ('max_steps', 'max_output_tokens', 'timeout_seconds', 'max_input_tokens'):
        positive(config['limits'].get(name), 'limits.' + name)
    for name in ('max_steps', 'max_input_tokens', 'max_output_tokens'):
        if not isinstance(config['limits'][name], int):
            raise ValueError('limits.' + name + ' must be an integer')
    for name in ('image', 'timeout_seconds', 'max_file_bytes', 'max_total_bytes', 'max_files'):
        if not config['sandbox'].get(name):
            raise ValueError('missing sandbox.' + name)
    for name in ('timeout_seconds', 'max_file_bytes', 'max_total_bytes', 'max_files'):
        positive(config['sandbox'][name], 'sandbox.' + name)
    for name in ('max_file_bytes', 'max_total_bytes', 'max_files'):
        if not isinstance(config['sandbox'][name], int) or isinstance(config['sandbox'][name], bool):
            raise ValueError('sandbox.' + name + ' must be an integer')
    for phase in ('search', 'selection', 'final'):
        budget = config['budgets'].get(phase, {})
        positive(budget.get('tokens'), phase + ' token budget')
        positive(budget.get('money'), phase + ' money budget')
        if not isinstance(budget['tokens'], int) or isinstance(budget['tokens'], bool):
            raise ValueError('token budget must be an integer')
    pricing = config.get('pricing', {})
    real = any(config[role]['provider'] == 'openai' for role in ('model','proposal_model'))
    rate = pricing.get('per_token')
    if not pricing.get('version') or isinstance(rate, bool) or not isinstance(rate, (int,float)) or not math.isfinite(rate) or (rate <= 0 if real else rate != 0):
        raise ValueError('pricing requires a version and positive real / zero mock token rate')
    search = config['search']
    for name in ('population', 'generations', 'independent_searches'):
        positive(search.get(name), 'search.' + name)
        if not isinstance(search[name], int):
            raise ValueError('search counts must be integers')
    for name in ('elites', 'tournament_size'):
        if type(search.get(name)) is not int:
            raise ValueError('search.' + name + ' must be an integer')
    if search['population'] < 3 or search.get('elites') != 2 or search.get('tournament_size') != 2 or search.get('crossover_probability') != 0.5:
        raise ValueError('search requires population >=3, 2 elites, tournament 2, crossover 0.5')
    if config.get('repeats') != {'search': 1, 'selection': 3, 'final': 3} or config.get('infrastructure_retries') != 1:
        raise ValueError('fixed repeats are search=1 selection=3 final=3; infrastructure_retries=1')
    if (any(type(value) is not int for value in config['repeats'].values()) or
            type(config['infrastructure_retries']) is not int):
        raise ValueError('repeats and infrastructure_retries must be integers')
    ids, groups, tasks = set(), set(), set()
    for phase in ('search', 'selection', 'final'):
        cases = config['splits'].get(phase)
        if not cases:
            raise ValueError('empty split: ' + phase)
        current_groups, current_tasks = set(), set()
        for case in cases:
            identifier(case.get('id'), 'case id')
            identifier(case.get('group_id'), 'group id')
            if not all(case.get(key) for key in ('id', 'group_id', 'prompt', 'output_paths', 'expected')) or not isinstance(case.get('files'), dict):
                raise ValueError('case missing required fields')
            if case['id'] in ids or case['group_id'] in groups:
                raise ValueError('case IDs or groups overlap splits')
            task_hash = fingerprint({key:case.get(key) for key in ('prompt','files','operation','output_paths')})
            if task_hash in tasks:
                raise ValueError('exact public task overlaps splits')
            current_tasks.add(task_hash)
            ids.add(case['id'])
            current_groups.add(case['group_id'])
            for artifact in [*case['files'], *case['output_paths'], *case['expected']]:
                relative_path(artifact)
            if set(case['expected']) != set(case['output_paths']):
                raise ValueError('expected paths must match output whitelist')
            if not all(isinstance(v, str) for v in [*case['files'].values(), *case['expected'].values()]):
                raise ValueError('fixture contents must be text')
        groups.update(current_groups)
        tasks.update(current_tasks)
    source = Path(config['skill_path'])
    text = source.read_bytes().decode('utf-8')
    validate_skill(text, text, source.parent)
    return config


def public_case(case):
    return copy.deepcopy({key: case[key] for key in ('id', 'group_id', 'prompt', 'files', 'output_paths', 'operation') if key in case})


def trial_reservation(config):
    return config['limits']['max_steps'] * (config['limits']['max_input_tokens'] + config['limits']['max_output_tokens'])


def validate_config(config):
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('configuration numbers must be finite')
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        elif isinstance(value, list):
            for item in value:
                finite(item)
    try:
        if not isinstance(config, dict):
            raise ValueError('configuration must be an object')
        finite(config)
        return _validate_config(config)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('malformed or missing configuration fields: ' + str(exc)) from exc


def mock_only(config):
    return any(config[role]['provider'] == 'mock' for role in ('model','proposal_model'))
