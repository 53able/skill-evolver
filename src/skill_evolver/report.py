"""Conditional paired group bootstrap reports; mock results make no quality claim."""
import json
import random
import statistics
from .store import Store, latest_trials, event_scope, method_validity, runtime_validity
from .config import mock_only


def paired_interval(rows, left, right, search, seed=0, iterations=10000):
    # First average repeats per case, preserving cases as the estimand.
    per_case: dict[tuple[str, str], list[float | None]] = {}
    for row in rows:
        if row.get('search') != search or row.get('method') not in (left, right):
            continue
        key = row['method'], row['case_id']
        per_case.setdefault(key, []).append(row['evaluation']['score'])
    groups: dict[str, list[float]] = {}
    cases = {row['case_id']: row['group_id'] for row in rows if row.get('search') == search}
    for case, group in cases.items():
        a, b = per_case.get((left, case)), per_case.get((right, case))
        if not a or not b or any(x is None for x in a + b):
            return {'complete':False,'difference':None,'interval':None,'groups':len(set(cases.values()))}
        groups.setdefault(group, []).append(statistics.mean(x for x in a if x is not None) - statistics.mean(x for x in b if x is not None))
    if not groups:
        return {'complete':False,'difference':None,'interval':None,'groups':0}
    values = list(groups.values())
    difference = statistics.mean([x for group in values for x in group])
    rng = random.Random(seed)
    samples = []
    for _ in range(iterations):
        sampled = [x for group in rng.choices(values, k=len(values)) for x in group]
        samples.append(statistics.mean(sampled))
    samples.sort()
    interval = [samples[int(iterations * .025)], samples[min(iterations-1, int(iterations * .975))]]
    return {'complete':True,'difference':difference,'interval':interval,'groups':len(groups),
            'exploratory':len(groups)<10,'conditional_on_search':True}


def build_report(directory):
    store = Store(directory)
    manifest = json.loads((store.path / 'experiment.json').read_text())
    config = manifest['config']
    events = store.events()
    trials = [e for e in events if e['kind'] == 'trial']
    latest = latest_trials(events)
    finals = []
    final_cases = {case['id']:case for case in config['splits']['final']}
    for trial in latest:
        scope = event_scope(trial).split('/')
        if len(scope) != 3 or scope[2] != 'final' or not scope[1].isdigit():
            continue
        # Early journals recovered interrupted calls with only id/scope metadata.
        parts = trial['id'].split('/')
        case_id = trial.get('case_id', parts[-3] if len(parts) == 7 else None)
        group_id = trial.get('group_id', final_cases.get(case_id, {}).get('group_id'))
        row = dict(trial, method=scope[0], search=int(scope[1]), phase='final',
                   case_id=case_id, group_id=group_id)
        if case_id not in final_cases or group_id is None:
            row['evaluation'] = {'score':None,'complete':False,'reason':'ambiguous_historical_trial'}
        finals.append(row)
    runtime_state = runtime_validity(events, manifest)
    searches: list[dict] = []
    for search in range(config['search']['independent_searches']):
        methods = {}
        expected = len(config['splits']['final']) * config['repeats']['final']
        for method in ('original','sequential','ga'):
            rows = [r for r in finals if r['search'] == search and r['method'] == method]
            state = method_validity(events, manifest, method, search)
            valid = state['valid'] and len(rows) == expected and all(r['evaluation']['complete'] for r in rows)
            methods[method] = {'complete':valid, 'planned_trials':expected, 'observed_trials':len(rows), 'validity':state,
                               'success_rate':statistics.mean(r['evaluation']['score'] for r in rows) if valid else None,
                               'cases':rows}
        comparisons = {other: paired_interval(finals,'ga',other,search,config['search']['seed'])
                       for other in ('original','sequential')} if all(m['complete'] for m in methods.values()) else {}
        evidence = (not mock_only(config) and bool(comparisons) and
                    all(c.get('complete') and not c.get('exploratory') and c['interval'][0] > 0
                        for c in comparisons.values()))
        judgment = ('Mock plumbing only; superiority unverified.' if mock_only(config) else
                    ('Evidence of improvement conditional on this search, runner, skill and models.' if evidence else
                     'Superiority unverified; inconclusive does not establish equivalence.'))
        searches.append({'search':search,'methods':methods,'ga_minus':comparisons,'judgment':judgment})
    costs = {}
    for event in store.events():
        if event['kind'] in ('reserve','settle'):
            costs[event['id']] = event
    scopes: dict[str, dict[str, float]] = {}
    for event in costs.values():
        scope = scopes.setdefault(event['scope'], {'tokens':0,'money':0,'unsettled_reservations':0})
        scope['tokens'] += event['tokens']
        scope['money'] += event['money']
        scope['unsettled_reservations'] += event['kind'] == 'reserve'
    for key, value in scopes.items():
        limit = config['budgets'][key.split('/')[-1]]
        value['remaining_tokens'] = limit['tokens'] - value['tokens']
        value['remaining_money'] = limit['money'] - value['money']
    candidates = [json.loads(p.read_text()) for p in sorted((store.path / 'candidates').glob('*.json'))]
    return {'experiment_id':config['experiment_id'],'mock_only':mock_only(config),
            'claim':('Mock model results are plumbing tests only; no skill-improvement or GA superiority claim.' if mock_only(config) else 'Results are conditional on each search and fixed runner/skill/models; no general GA superiority claim.'),
            'accounting_caveat':'Configured token-rate accounting only. Token-count endpoint fees, provider pricing changes and actual invoices are not guaranteed by this ledger.',
            'search_reproducibility':'unverified' if len(searches)==1 else ('mock plumbing only' if mock_only(config) else 'All independent searches reported; no across-search inferential claim'),
            'bootstrap':'10,000 paired group resamples; case equal weighting; conditional on each search; <10 groups exploratory only',
            'runtime_validity':runtime_state, 'complete':all(method['complete'] for search in searches for method in search['methods'].values()),
            'searches':searches,'budgets':scopes,'all_trials':trials,'candidates':candidates,
            'cache_hits':sum(e.get('cache_hits',0) for e in store.events() if e['kind']=='completed')}


def render_markdown(report):
    lines = ['# skill-evolver experiment report', '', report['claim'], '', report['bootstrap'], '']
    for search in report['searches']:
        lines += [f'## Independent search {search["search"]}', '', search['judgment'], '']
        for method, result in search['methods'].items():
            lines.append(f'- {method}: complete={result["complete"]}; success={result["success_rate"]}; trials={result["observed_trials"]}/{result["planned_trials"]}')
        for other, comparison in search['ga_minus'].items():
            lines.append(f'- GA minus {other}: {comparison}')
    lines += ['', '## Budgets', '', report['accounting_caveat'], '']
    lines.extend(f'- {key}: {value}' for key,value in report['budgets'].items())
    lines += ['', 'Full case outcomes, unsuccessful cases, uncertainty, transcripts and diffs are in report.json.', '']
    return '\n'.join(lines)
