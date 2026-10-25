"""Body-only mutations with immutable metadata and local references."""
import hashlib
import re


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def split_skill(text):
    match = re.match(r'\A---\r?\n.*?\r?\n---(?:\r?\n|$)', text, re.S)
    if not match:
        raise ValueError('SKILL.md requires YAML frontmatter')
    metadata = match.group()
    if not re.search(r'^name:\s*\S', metadata, re.M) or not re.search(r'^description:\s*\S', metadata, re.M):
        raise ValueError('frontmatter requires name and description')
    return metadata, text[match.end():]


def validate_skill(text, original, root):
    metadata, body = split_skill(text)
    if metadata != split_skill(original)[0]:
        raise ValueError('frontmatter is immutable')
    if not body.strip() or body.count('```') % 2:
        raise ValueError('empty body or unclosed code fence')
    anchors = {re.sub(r'[^\w -]', '', h.lower()).replace(' ', '-')
               for h in re.findall(r'^#+\s+(.+)$', body, re.M)}
    definitions = dict(re.findall(r'^\[([^]]+)\]:\s*(\S+)', body, re.M))
    targets = re.findall(r'\[[^]]*\]\(([^)]+)\)', body)
    for label in re.findall(r'\[[^]]+\]\[([^]]+)\]', body):
        if label not in definitions:
            raise ValueError('undefined reference: ' + label)
    targets.extend(definitions.values())
    for target in targets:
        target = target.split()[0].strip('<>')
        if target.startswith('#'):
            if target[1:] not in anchors:
                raise ValueError('broken anchor: ' + target)
        elif '://' not in target and not target.startswith('mailto:'):
            raise ValueError('bundled file references unsupported until runner snapshot/staging is implemented: ' + target)
    return text


def candidate(text, parents=(), operation='original'):
    return {'id': digest(text), 'hash': digest(text), 'text': text,
            'parents': list(parents), 'operation': operation}


def mutate(text, rng):
    header, body = split_skill(text)
    operations = ['clarify', 'reorder', 'delete', 'example']
    operation = rng.choice(operations)
    # Only whole paragraphs outside fences are removed/reordered; validation follows.
    paragraphs = body.split('\n\n')
    if operation == 'delete' and len(paragraphs) > 1:
        index = rng.randrange(len(paragraphs))
        if '[' not in paragraphs[index] and '```' not in paragraphs[index]:
            del paragraphs[index]
        body = '\n\n'.join(paragraphs)
    elif operation == 'reorder' and len(paragraphs) > 1 and '```' not in body:
        index = rng.randrange(len(paragraphs) - 1)
        paragraphs[index:index + 2] = reversed(paragraphs[index:index + 2])
        body = '\n\n'.join(paragraphs)
    elif operation == 'example':
        body += '\n\nExample: read the input, transform its contents, and save the requested output.\n'
    else:
        body += '\n\nCheck the requested output format before finishing.\n'
    return header + body, operation


def crossover(left, right):
    header, body = split_skill(left)
    other = split_skill(right)[1]
    sections = re.split(r'(?=^## )', body, flags=re.M)
    donor = re.split(r'(?=^## )', other, flags=re.M)
    titles = {part.split('\n')[0]: part for part in donor if part.startswith('## ')}
    for index, part in enumerate(sections):
        title = part.split('\n')[0]
        if title in titles:
            sections[index] = titles[title]
            return header + ''.join(sections), 'section_crossover'
    return left, 'crossover_skipped_no_matching_sections'
