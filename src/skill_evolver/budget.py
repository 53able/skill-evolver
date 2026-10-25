"""Reserve before calling; unknown usage never releases a reservation."""
class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, store, scope, limits, per_token=0):
        self.store, self.scope, self.limits, self.rate = store, scope, limits, per_token

    def records(self):
        records = {}
        for event in self.store.events():
            if event.get('scope') == self.scope and event['kind'] in ('reserve', 'settle'):
                records[event['id']] = event
        return records

    def usage(self):
        records = self.records().values()
        return {'tokens': sum(e['tokens'] for e in records),
                'money': sum(e['money'] for e in records)}

    def reserve(self, identity, tokens, metadata=None):
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError('reservation must be a nonnegative integer')
        if identity in self.records():
            return False
        usage = self.usage()
        if usage['tokens'] + tokens > self.limits['tokens'] or usage['money'] + tokens * self.rate > self.limits['money']:
            raise BudgetExceeded(self.scope)
        self.store.append({'kind': 'reserve', 'scope': self.scope, 'id': identity,
                           'tokens': tokens, 'money': tokens * self.rate, 'rate':self.rate, 'metadata':metadata or {}})
        return True

    def settle(self, identity, tokens):
        reserved = self.records()[identity]
        if tokens is None:
            return
        if reserved['kind'] == 'settle':
            if tokens != reserved['tokens']:
                raise ValueError('cannot change an already settled charge')
            return
        if not isinstance(tokens, int) or isinstance(tokens, bool) or not 0 <= tokens <= reserved['tokens']:
            raise ValueError('reported usage exceeds reservation or is invalid')
        self.store.append({'kind': 'settle', 'scope': self.scope, 'id': identity,
                           'tokens': tokens, 'money': tokens * reserved.get('rate', self.rate), 'rate':reserved.get('rate',self.rate)})
