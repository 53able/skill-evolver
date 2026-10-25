"""External exact-output evaluator; artifact contents are never executed."""
VALID_STATUSES = {'success', 'task_failure', 'timeout', 'infrastructure_error', 'indeterminate'}


def evaluate(result, case, sandbox):
    status = result['status']
    if status not in VALID_STATUSES:
        raise ValueError('unknown runner status')
    if status in ('infrastructure_error', 'indeterminate'):
        return {'score': None, 'complete': False, 'reason': status}
    outputs = result.get('outputs', {})
    valid = isinstance(outputs, dict) and set(outputs) == set(case['output_paths'])
    if valid:
        valid = (len(outputs) <= sandbox['max_files'] and
                 all(isinstance(value, str) and len(value.encode()) <= sandbox['max_file_bytes'] for value in outputs.values()) and
                 sum(len(value.encode()) for value in outputs.values()) <= sandbox['max_total_bytes'])
    success = status == 'success' and result.get('skill_loaded') is True and valid and outputs == case['expected']
    return {'score': int(success), 'complete': True,
            'reason': 'exact_match' if success else 'task_or_artifact_failure'}
