"""Deterministic search orchestration; trial execution is injected for tests."""
import difflib
import json
import re
from pathlib import Path
import random
from .budget import Budget, BudgetExceeded
from .config import fingerprint, public_case, trial_reservation, validate_config, mock_only
from .evaluator import evaluate
from .genome import candidate, crossover, digest, mutate, split_skill, validate_skill
from .store import Store, method_validity, runtime_validity
from .proposer import proposal_context, search_failures

METHODS = ('original', 'sequential', 'ga')


def rank(item):
    record, scores = item
    known_tokens = [s['tokens'] for s in scores]
    tokens = sum(known_tokens) if all(t is not None for t in known_tokens) else float('inf')
    return (-sum(s['evaluation']['score'] for s in scores) / len(scores), tokens,
            len(split_skill(record['text'])[1]), record['id'])


def tournament(population, rng):
    return min(rng.sample(population, min(2, len(population))), key=rank)[0]


class Experiment:
    def __init__(self, config, directory, runner=None, proposal_broker=None):
        validate_config(config)
        self.config, self.store = config, Store(directory)
        if runner is None:
            from .runner import TrialRunner
            runner = TrialRunner(config)
        self.runner = runner
        self.proposal_broker = proposal_broker
        self.original = Path(config['skill_path']).read_bytes().decode('utf-8')
        self.original_hash = digest(self.original)
        self.cache_hits = 0

    def verify_original(self):
        if digest(Path(self.config['skill_path']).read_bytes().decode('utf-8')) != self.original_hash:
            raise ValueError('original skill changed during experiment')

    def budget(self, method, search, phase, *, proposal=False):
        role = 'proposal_model' if proposal else 'model'
        rate = self.config['pricing']['per_token'] if self.config[role]['provider'] == 'openai' else 0
        return Budget(self.store, f'{method}/{search}/{phase}', self.config['budgets'][phase], rate)

    def initialize(self):
        manifest = self.store.path / 'experiment.json'
        identity = {'config_hash': fingerprint(self.config), 'original_hash': self.original_hash}
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            if any(saved[key] != value for key, value in identity.items()):
                raise ValueError('resume config or original hash differs from frozen experiment')
            pinned = saved.get('runtime_image_id')
            if not pinned:
                raise ValueError('cannot resume historical experiment without frozen runtime_image_id; start a new run')
        else:
            saved = dict(identity, config=self.config, version='0.2.0', mock_only=mock_only(self.config))
            pinned = None
        self.runner.resource_recorder = self.store.append
        image_id = self.runner.prepare_environment(pinned)
        if not isinstance(image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
            raise ValueError('runner did not prepare an immutable runtime image ID')
        if pinned is not None and image_id != pinned:
            raise ValueError('runner runtime image differs from frozen image')
        self.manifest = dict(saved, runtime_image_id=image_id)
        if not manifest.exists():
            self.store.write('experiment.json', self.manifest)
            self.store.write('original.json', candidate(self.original))
        self.runner.recover_resources(self.store.events())
        environment_state = runtime_validity(self.store.events(), self.manifest)
        if 'mixed_environment_hashes' in environment_state['reasons'] or 'trial_environment_hash_missing' in environment_state['reasons']:
            raise ValueError('historical trial environments violate the frozen runtime contract')
        # Crash after a result but before settlement: settle from the durable result.
        results = {e['id']: e for e in self.store.events() if e['kind'] in ('trial','proposal_result')}
        settled = {e['id'] for e in self.store.events() if e['kind'] == 'settle'}
        for event in list(self.store.events()):
            if event['kind'] != 'reserve' or event['id'] in settled:
                continue
            result = results.get(event['id'])
            if result and result['tokens'] is not None:
                Budget(self.store, event['scope'], {'tokens':float('inf'),'money':float('inf')}, self.config['pricing']['per_token']).settle(event['id'], result['tokens'])
            elif not result and event['id'].startswith('proposal/'):
                self.store.append({'kind':'proposal_result', 'id':event['id'], 'scope':event['scope'],
                                   'candidate':None, 'status':'indeterminate', 'tokens':None, 'cost':None,
                                   'termination_reason':'interrupted_after_reservation'})
            elif not result:
                self.store.append(dict(event.get('metadata', {}), kind='trial', id=event['id'], scope=event['scope'],
                                   status='indeterminate', tokens=None, cost=None,
                                   evaluation={'score':None,'complete':False,'reason':'interrupted_after_reservation'},
                                   termination_reason='interrupted_after_reservation'))

    def register(self, record):
        target = self.store.path / 'candidates' / (record['id'] + '.json')
        if not target.exists():
            record = dict(record, diff=''.join(difflib.unified_diff(self.original.splitlines(True), record['text'].splitlines(True), fromfile='original', tofile=record['id'])))
            self.store.write('candidates/' + record['id'] + '.json', record)
        return record

    def trial(self, record, case, repeat, method, search, phase):
        budget = self.budget(method, search, phase)
        for attempt in range(self.config['infrastructure_retries'] + 1):
            identity = f'{budget.scope}/{record["id"]}/{case["id"]}/{repeat}/{attempt}'
            previous = next((e for e in self.store.events() if e['kind'] == 'trial' and e['id'] == identity), None)
            if previous:
                result = previous
                self.cache_hits += 1
            else:
                self.verify_original()
                reservation = trial_reservation(self.config)
                metadata = {'experiment_id':self.config['experiment_id'], 'candidate_id':record['id'],
                            'case_id':case['id'], 'group_id':case['group_id'], 'repeat':repeat,
                            'attempt':attempt, 'method':method, 'search':search, 'phase':phase,
                            'environment_hash':None, 'duration_seconds':None,
                            'model':self.config['model'], 'pricing_version':self.config['pricing']['version']}
                budget.reserve(identity, reservation, metadata)
                # Public case is built from an allowlist before crossing the runner boundary.
                outcome = self.runner.run(record['text'], public_case(case), trial_id=identity)
                self.verify_original()
                tokens = outcome.get('tokens')
                if tokens is not None and (not isinstance(tokens, int) or isinstance(tokens, bool) or not 0 <= tokens <= reservation):
                    outcome = dict(outcome, status='indeterminate', tokens=None, termination_reason='usage_exceeds_reservation')
                    tokens = None
                observed = outcome.get('environment_hash')
                expected = self.manifest.get('environment_hash')
                if outcome['status'] in ('success','task_failure','timeout') and not observed:
                    outcome = dict(outcome, status='indeterminate', termination_reason='environment_hash_missing')
                elif observed:
                    if expected and observed != expected:
                        outcome = dict(outcome, status='indeterminate', termination_reason='environment_hash_mismatch')
                    elif not expected and outcome['status'] in ('success','task_failure','timeout'):
                        self.manifest['environment_hash'] = observed
                        self.store.write('experiment.json', self.manifest)
                result = dict(outcome, kind='trial', id=identity, experiment_id=self.config['experiment_id'],
                              scope=budget.scope, method=method, search=search, phase=phase,
                              candidate_id=record['id'], case_id=case['id'], group_id=case['group_id'],
                              repeat=repeat, attempt=attempt, cost=tokens * budget.rate if tokens is not None else None,
                              pricing_version=self.config['pricing']['version'],
                              evaluation=evaluate(outcome, case, self.config['sandbox']))
                self.store.append(result)
                budget.settle(identity, tokens)
            if result['status'] != 'infrastructure_error':
                return result
        return result

    def score(self, record, method, search, phase):
        scores = []
        try:
            for case in self.config['splits'][phase]:
                for repeat in range(self.config['repeats'][phase]):
                    result = self.trial(record, case, repeat, method, search, phase)
                    scores.append(result)
                    if not result['evaluation']['complete']:
                        return None
        except BudgetExceeded:
            return None
        return (record, scores)

    def propose(self, parent, rng, method, search, index, donor=None):
        if self.config['proposal_model']['provider'] == 'openai':
            return self.real_propose(parent, method, search, index, donor)
        text, operation = parent['text'], ''
        if donor is not None:
            text, operation = crossover(text, donor['text'])
        text, mutation = mutate(text, rng)
        operation = operation + '+' + mutation if operation else mutation
        identity = f'proposal/{method}/{search}/{index}'
        budget = self.budget(method, search, 'search', proposal=True)
        if budget.reserve(identity, 0):
            budget.settle(identity, 0)
            self.store.append({'kind':'proposal', 'id':identity, 'scope':budget.scope,
                               'tokens':0,'cost':0,'synthetic':True,'operation':operation,
                               'parents':[parent['id']] + ([donor['id']] if donor else [])})
        try:
            validate_skill(text, self.original, Path(self.config['skill_path']).parent)
        except ValueError:
            return None
        return self.register(candidate(text, [parent['id']] + ([donor['id']] if donor else []), operation))

    def real_propose(self, parent, method, search, index, donor=None):
        from .model import ModelError, OpenAIJSONModel
        identity = f'proposal/{method}/{search}/{index}'
        previous = next((event for event in self.store.events()
                         if event['kind'] == 'proposal_result' and event['id'] == identity), None)
        if previous:
            return self.register(previous['candidate']) if previous['candidate'] else None
        budget = self.budget(method, search, 'search', proposal=True)
        limits = self.config['limits']
        reservation = limits['max_input_tokens'] + limits['max_output_tokens']
        try:
            if not budget.reserve(identity, reservation):
                return None
        except BudgetExceeded:
            return None
        crossed, operation = parent['text'], 'mutation_only'
        if donor:
            crossed, operation = crossover(parent['text'], donor['text'])
        context = proposal_context(self.original, dict(parent, text=crossed), donor,
                                   search_failures(self.store.events(), method, search))
        broker = self.proposal_broker
        if broker is None:
            broker = self.proposal_broker = OpenAIJSONModel(self.config['proposal_model'])
        record, status, tokens, reason = None, 'invalid', None, 'invalid_proposal'
        try:
            response = broker.complete(context, max_input_tokens=limits['max_input_tokens'],
                                       max_output_tokens=limits['max_output_tokens'],
                                       timeout_seconds=limits['timeout_seconds'])
        except ModelError as exc:
            tokens = exc.tokens
            if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 <= tokens <= reservation:
                tokens = None
            status, reason = 'indeterminate' if tokens is None else 'model_error', 'ModelError'
        else:
            try:
                tokens = response['tokens']
                if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 <= tokens <= reservation:
                    tokens = None
                    raise ValueError('usage outside reservation')
                text = response['content']['skill_text']
                if not isinstance(text, str):
                    raise ValueError('skill_text must be text')
                validate_skill(text, self.original, Path(self.config['skill_path']).parent)
                record = candidate(text, [parent['id']] + ([donor['id']] if donor else []), operation + '+openai_body_mutation')
                status, reason = 'success', 'validated_body_proposal'
            except (ValueError, KeyError, TypeError):
                reason = 'invalid_response_or_candidate'
        # The exact response/candidate is durable before it can affect search.
        self.store.append({'kind':'proposal_result', 'id':identity,'scope':budget.scope,
                           'candidate':record,'status':status,'tokens':tokens,
                           'operation':operation + '+openai_body_mutation',
                           'parents':[parent['id']] + ([donor['id']] if donor else []),
                           'cost':tokens * budget.rate if tokens is not None else None,
                           'termination_reason':reason,'model':self.config['proposal_model'],
                           'pricing_version':self.config['pricing']['version']})
        budget.settle(identity, tokens)
        return self.register(record) if record else None

    def search(self, method, search):
        rng = random.Random(self.config['search']['seed'] + search)
        original = self.register(candidate(self.original))
        evaluated: dict[str, tuple[dict, list[dict]]] = {}

        def score(record):
            if record['id'] in evaluated:
                self.cache_hits += 1
                return evaluated[record['id']]
            result = self.score(record, method, search, 'search')
            if result is not None:
                evaluated[record['id']] = result
            return result

        incumbent = score(original)
        if incumbent is None:
            return []
        cfg = self.config['search']
        if method == 'sequential':
            for index in range(cfg['population'] - 1 + (cfg['generations'] - 1) * (cfg['population'] - cfg['elites'])):
                child = self.propose(incumbent[0], rng, method, search, index)
                result = score(child) if child else None
                if child and result is None:
                    break
                if result and rank(result) < rank(incumbent):
                    incumbent = result
        elif method == 'ga':
            population = [incumbent]
            for index in range(cfg['population'] - 1):
                child = self.propose(original, rng, method, search, f'initial-{index}')
                result = score(child) if child else None
                if result:
                    population.append(result)
            for generation in range(1, cfg['generations']):
                population.sort(key=rank)
                next_population = population[:cfg['elites']]
                for index in range(cfg['population'] - cfg['elites']):
                    parent = tournament(population, rng)
                    donor = tournament(population, rng) if rng.random() < cfg['crossover_probability'] else None
                    child = self.propose(parent, rng, method, search, f'{generation}-{index}', donor)
                    result = score(child) if child else None
                    if result:
                        next_population.append(result)
                population = next_population
        return [item[0] for item in sorted(evaluated.values(), key=rank)[:3]]

    def run(self):
        with self.store.lock():
            self.initialize()
            searches = range(self.config['search']['independent_searches'])
            frozen = {}
            validity = {}
            for search in searches:
                for method in METHODS:
                    key = f'{method}/{search}'
                    prior = next((e for e in reversed(self.store.events()) if e['kind'] == 'frozen' and e['id'] == key), None)
                    records = prior['candidates'] if prior else self.search(method, search)
                    state = method_validity(self.store.events(), self.manifest, method, search)
                    validity[key] = state
                    if not prior or prior.get('validity') != state:
                        self.store.append({'kind':'frozen','id':key,'candidates':records,'validity':state})
                    frozen[key] = records
            # All search results are frozen before opening any selection data.
            winners = {}
            for key, records in frozen.items():
                method, search_id = key.split('/')
                prior = next((e for e in reversed(self.store.events()) if e['kind'] == 'winner' and e['id'] == key), None)
                state = method_validity(self.store.events(), self.manifest, method, int(search_id))
                if not state['valid']:
                    winner = None
                elif prior:
                    winner = prior['candidate']
                else:
                    scored = [self.score(record, method, int(search_id), 'selection') for record in records]
                    winner = min(scored, key=rank)[0] if scored and all(scored) else None
                if not prior or prior.get('validity') != state or prior['candidate'] != winner:
                    self.store.append({'kind':'winner','id':key,'candidate':winner,'validity':state})
                winners[key] = winner
            finals = {}
            for key, winner in winners.items():
                method, search_id = key.split('/')
                finals[key] = bool(winner and self.score(winner, method, int(search_id), 'final'))
            for key in frozen:
                method, search_id = key.split('/')
                validity[key] = method_validity(self.store.events(), self.manifest, method, int(search_id))
            self.verify_original()
            result = {'kind':'completed','complete':all(finals.values()) and all(state['valid'] for state in validity.values()),
                      'finals':finals, 'validity':validity,
                      'runtime_validity':runtime_validity(self.store.events(), self.manifest),
                      'cache_hits':self.cache_hits,'mock_only':mock_only(self.config),
                      'claim':('Plumbing tests only; no evidence of skill quality improvement.' if mock_only(self.config)
                               else 'Conditional results require report review; no general GA superiority claim.')}
            self.store.append(result)
            self.store.write('completion.json', result)
            return result
