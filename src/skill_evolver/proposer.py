"""Proposal context boundary: only parent bodies, fixed metadata and search failures."""
from .genome import split_skill


def proposal_context(original, parent, donor, failures):
    metadata, _ = split_skill(original)
    context = {
        'task': 'Improve only the skill body. Preserve the exact original frontmatter. Return JSON {"skill_text": "full SKILL.md"}. Do not introduce local file references. Use the search failure observations, if any, to clarify the procedure.',
        'original_frontmatter': metadata,
        'current_body': split_skill(parent['text'])[1],
        'search_failures': failures,
    }
    if donor:
        context['donor_body'] = split_skill(donor['text'])[1]
        context['task'] += ' Section crossover has already been applied to current_body. Make exactly one body edit to that crossed body.'
    return context


def search_failures(events, method, search):
    return [{key:event.get(key) for key in ('case_id', 'status', 'termination_reason', 'transcript')}
            for event in events if event['kind'] == 'trial' and event.get('method') == method
            and event.get('search') == search and event.get('phase') == 'search'
            and event.get('evaluation', {}).get('score') == 0]
