"""Command line entry point."""
import argparse
import json
from pathlib import Path
import sys
from .config import load_config, mock_only
from .evolution import Experiment
from .report import build_report, render_markdown
from .store import Store, verify_experiment_directory


def main(argv=None):
    parser = argparse.ArgumentParser(prog='skill-evolver')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('validate','smoke','run'):
        sub = commands.add_parser(name)
        sub.add_argument('config')
        if name != 'validate':
            sub.add_argument('--output', required=True)
    for name in ('resume','report'):
        commands.add_parser(name).add_argument('directory')
    improve_parser = commands.add_parser('improve', help='Improve a text skill with an existing CLI login')
    improvement = improve_parser.add_subparsers(dest='improve_command', required=True)
    prepare = improvement.add_parser('prepare', help='Generate evaluation tasks for your review')
    prepare.add_argument('skill', help='Path to the SKILL.md to improve (never modified)')
    prepare.add_argument('--provider', choices=('claude', 'codex'), required=True)
    prepare.add_argument('--model', required=True, help='Claude: alias such as sonnet or a full model ID; Codex: full model ID from /model')
    prepare.add_argument('--output', required=True, help='New directory for this attempt (must not exist)')
    prepare.add_argument('--allow-untested-cli', action='store_true',
                         help='Allow a CLI version other than the tested one (recorded in the report)')
    run = improvement.add_parser('run', help='Run only the evaluation bundle you reviewed')
    run.add_argument('directory', help='Directory created by improve prepare')
    run.add_argument('--approve-evaluation', metavar='SHA256',
                     help='Approval hash from improve report; omit it to confirm interactively')
    report_command = improvement.add_parser('report', help='Render an offline evaluation/comparison report')
    report_command.add_argument('directory', help='Directory created by improve prepare')
    for sub in (prepare, run, report_command):
        sub.add_argument('--json', action='store_true', help='Print the machine-readable summary')
    args = parser.parse_args(argv)
    try:
        if args.command == 'improve':
            result = improve(args)
            if result is None:
                return 1
        elif args.command == 'validate':
            config = load_config(args.config)
            result = {'valid':True,'experiment_id':config['experiment_id'],'mock_only':mock_only(config)}
        elif args.command in ('run','smoke'):
            config = load_config(args.config)
            if args.command == 'smoke':
                if len(config['splits']['search']) < 3:
                    raise ValueError('smoke requires at least three search cases')
                config['experiment_id'] += '-smoke'
                config['splits']['search'] = config['splits']['search'][:3]
                from .genome import candidate
                experiment = Experiment(config, args.output)
                with experiment.store.lock():
                    experiment.initialize()
                    result = experiment.score(candidate(experiment.original), 'smoke', 0, 'search')
                    experiment.verify_original()
                    result = {'complete':result is not None,'mock_only':mock_only(config),'claim':'Three-case smoke; no improvement claim.'}
            else:
                result = Experiment(config, args.output).run()
        elif args.command == 'resume':
            verify_experiment_directory(args.directory)
            saved = json.loads((Path(args.directory) / 'experiment.json').read_text())
            result = Experiment(saved['config'], args.directory).run()
        else:
            verify_experiment_directory(args.directory)
            with Store(args.directory).lock():
                result = build_report(args.directory)
                Store(args.directory).write('report.json', result)
                (Path(args.directory) / 'report.md').write_text(render_markdown(result))
        if args.command == 'improve' and not args.json:
            from .improve_text import render, safe
            print(safe(render(result)))
        else:
            print(json.dumps(result, sort_keys=True))
        return 1 if result.get('unsupported') else (0 if result.get('complete', True) else 2)
    except KeyboardInterrupt:
        print('skill-evolver: interrupted', file=sys.stderr)
        return 130
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        print(f'skill-evolver: {error}', file=sys.stderr)
        if args.command == 'improve':
            from .improve_text import hint
            advice = hint(error)
            if advice:
                print('  ' + advice, file=sys.stderr)
        return 1


def improve(args):
    """Return the improvement summary, or None when the user declines to run."""
    from .improve import ImproveStore, fingerprint, guard_directory, load_bundle, prepare, run
    from .improve_report import evaluation_markdown, report, report_locked
    from .improve_text import approval_header, cancelled, confirm_prompt, progress, safe
    def show(*values):
        try:
            print(progress(*values), file=sys.stderr, flush=True)
        except OSError:
            pass
    if args.improve_command == 'prepare':
        return prepare(args.skill, args.provider, args.model, args.output,
                       allow_untested=args.allow_untested_cli, progress=show)
    if args.improve_command == 'report':
        return report(args.directory)
    approval = args.approve_evaluation
    if approval is None:
        store = ImproveStore(guard_directory(args.directory))
        with store.lock():
            summary = report_locked(store)
            if summary['state'] != 'draft':
                return summary
            # Display and approve one in-memory value; the file may be edited concurrently.
            bundle = load_bundle(store.path)[1]
        approval = fingerprint(bundle)
        from .improve_eval import too_weak
        if too_weak(bundle['evaluation']) and not (store.path / 'approved.json').exists():
            # Refuse before asking, rather than after the user answers y.
            raise ValueError('evaluation requires three to twelve criteria with at least one required; '
                             'edit evaluation.json, then run improve report')
        if args.json or not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise ValueError('approval required: run improve run in a terminal to confirm, '
                             'or pass --approve-evaluation with approval_hash from improve report --json')
        print(safe(evaluation_markdown(bundle['evaluation'], approval)))
        print(safe(approval_header(summary, bundle, approval)))
        try:
            answer = input(confirm_prompt(summary, bundle['limits']))
        except (EOFError, KeyboardInterrupt):
            answer = ''
        if answer.strip().lower() not in ('y', 'yes'):
            print(cancelled(), file=sys.stderr)
            return None
    return run(args.directory, approval, progress=show)
