import json
import os
import unittest
from unittest.mock import patch

import test_improve as base
from skill_evolver.improve import ImproveStore, load_bundle, run
from skill_evolver.improve_files import check_references, snapshot


MENTOR = ('---\nname: mentor\ndescription: Review plans\n---\n\n'
          'Read `references/principles.md` first. Use [the template](assets/template.md).\n'
          'Then run `scripts/validate.py review.md`.\n')


class CompanionFileTests(unittest.TestCase):
    # Reuse only the fake-CLI fixture; subclassing would rerun every inherited test.
    tearDown = base.ImprovementTests.tearDown
    calls = base.ImprovementTests.calls
    prepared = base.ImprovementTests.prepared

    def setUp(self):
        base.ImprovementTests.setUp(self)
        self.source.write_text(MENTOR)
        root = self.source.parent
        for relative, text in {'references/principles.md': 'Principle one.\n', 'assets/template.md': '# Review\n',
                               'scripts/validate.py': 'print("ok")\n'}.items():
            (root / relative).parent.mkdir(exist_ok=True)
            (root / relative).write_text(text)

    def test_companions_are_frozen_and_reach_every_model_context(self):
        prepared = self.prepared()
        self.assertEqual(prepared['companion_files'],
                         ['assets/template.md', 'references/principles.md', 'scripts/validate.py'])
        bundle = load_bundle(self.output)[1]
        self.assertEqual(bundle['companions']['scripts/validate.py'], 'print("ok")\n')
        self.assertIn('Bundled files', (self.output / 'evaluation.md').read_text())
        result = run(self.output, prepared['approval_hash'])
        self.assertEqual(result['status'], 'observed_improvement')
        contexts = self.calls()
        self.assertTrue(all(c['bundled_files'] == bundle['companions'] for c in contexts))
        self.assertTrue(all('never executed' in c['task'] for c in contexts))
        self.assertEqual(self.source.read_text(), MENTOR)

    def test_changed_companion_stops_before_any_run_call(self):
        prepared = self.prepared()
        (self.source.parent / 'references/principles.md').write_text('Changed.\n')
        before = len(self.calls())
        with self.assertRaisesRegex(ValueError, 'original skill changed'):
            run(self.output, prepared['approval_hash'])
        self.assertEqual(len(self.calls()), before)

    def test_candidate_may_not_reference_new_files(self):
        prepared = self.prepared()
        with patch.dict(os.environ, {'FAKE_PROPOSAL_EXTRA': 'See references/new.md.\n'}):
            result = run(self.output, prepared['approval_hash'])
        self.assertEqual(result['status'], 'no_observed_improvement')
        reasons = [e['reason'] for e in ImproveStore(self.output).events() if e['kind'] == 'candidate_rejected']
        self.assertEqual(reasons, ['invalid_candidate'] * 3)

    def test_unusable_companions_are_refused_before_any_call(self):
        def relink(root):
            (root / 'references/principles.md').unlink()
            (root / 'references/principles.md').symlink_to(root / 'assets/template.md')
        cases = [('missing', lambda root: (root / 'assets/template.md').unlink()),
                 ('symlinks', relink),
                 ('hidden', lambda root: self.source.write_text(MENTOR + 'Also references/.env.\n')),
                 ('unlinked', lambda root: os.link(root / 'assets/template.md', root / 'assets/copy.md')),
                 ('outside', lambda root: self.source.write_text(MENTOR + 'See [x](../secret.md).\n')),
                 ('regular', lambda root: os.mkfifo(root / 'references/pipe.md')),
                 ('UTF-8', lambda root: (root / 'references/latin.md').write_bytes('caf\xe9'.encode('latin-1'))),
                 ('64 KiB', lambda root: (root / 'references/big.md').write_text('x' * (64 * 1024 + 1)))]
        for message, damage in cases:
            with self.subTest(message=message):
                self.tearDown(); self.setUp()
                damage(self.source.parent)
                with self.assertRaisesRegex(ValueError, message):
                    self.prepared()
                self.assertFalse(self.output.exists())
                self.assertEqual(self.calls(), [])

    def test_markdown_forms_resolve_to_the_same_file(self):
        root = self.source.parent
        text = (MENTOR + 'See [auth](references/principles.md#auth), **references/principles.md**, '
                '`./references/principles.md`, references/*.md and [refs](references/).\n')
        files, missing = snapshot(root, text)
        self.assertEqual(sorted(files), ['assets/template.md', 'references/principles.md', 'scripts/validate.py'])
        self.assertEqual(missing, [])
        self.source.write_text(text + 'Also [same](references/x/../principles.md).\n')
        self.assertEqual(self.prepared()['state'], 'draft')

    def test_generated_and_binary_files_do_not_block_preparation(self):
        root = self.source.parent
        (root / 'scripts/__pycache__').mkdir()
        (root / 'scripts/__pycache__/validate.cpython-312.pyc').write_bytes(b'\x00\xff')
        (root / 'assets/logo.png').write_bytes(b'\x89PNG\r\n\x1a\n\xff\xfe')
        self.source.write_text(MENTOR + 'Save the review to assets/review.md.\n')
        prepared = self.prepared()
        bundle = load_bundle(self.output)[1]
        self.assertNotIn('scripts/__pycache__/validate.cpython-312.pyc', bundle['companions'])
        self.assertTrue(bundle['companions']['assets/logo.png'].startswith('[binary file not sent: 10 bytes'))
        self.assertTrue(any('assets/review.md' in item for item in prepared['warnings']))
        # The original's own output path stays allowed; a new path is not.
        check_references(MENTOR + 'Save the review to assets/review.md.\n', bundle['companions'], self.source.read_text())
        with self.assertRaisesRegex(ValueError, 'assets/other.md'):
            check_references(MENTOR + 'Save to assets/other.md.\n', bundle['companions'], self.source.read_text())

    def test_too_many_companions_are_refused(self):
        for index in range(33):
            (self.source.parent / 'references' / ('r{}.md'.format(index))).write_text('x\n')
        with self.assertRaisesRegex(ValueError, '32 files'):
            self.prepared()

    def test_paths_inside_longer_paths_are_not_companions(self):
        for path in ('references', 'assets', 'scripts'):
            for item in (self.source.parent / path).iterdir():
                item.unlink()
            (self.source.parent / path).rmdir()
        self.source.write_text('---\nname: rails\ndescription: Rails help\n---\n\nEdit app/assets/app.css when asked.\n')
        prepared = self.prepared()
        self.assertEqual(prepared['companion_files'], [])
        self.assertNotIn('companions', load_bundle(self.output)[1])
        self.assertNotIn('companions_hash', json.loads((self.output / 'manifest.json').read_text()))


if __name__ == '__main__':
    unittest.main()
