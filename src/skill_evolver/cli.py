"""Command line entry point."""
import argparse
import json
from pathlib import Path
import sys
from .config import load_config, mock_only
from .evolution import Experiment
from .report import build_report, render_markdown
from .store import Store


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
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
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
            saved = json.loads((Path(args.directory) / 'experiment.json').read_text())
            result = Experiment(saved['config'], args.directory).run()
        else:
            with Store(args.directory).lock():
                result = build_report(args.directory)
                Store(args.directory).write('report.json', result)
                (Path(args.directory) / 'report.md').write_text(render_markdown(result))
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get('complete', True) else 2
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        print(f'skill-evolver: {error}', file=sys.stderr)
        return 1
