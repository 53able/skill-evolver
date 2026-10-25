"""Human-readable terminal text for the improvement workflow (English or Japanese)."""
import os
import shlex
import unicodedata


def japanese():
    locale = os.environ.get('LC_ALL') or os.environ.get('LC_MESSAGES') or os.environ.get('LANG') or ''
    return locale.startswith('ja')


def _t(en, ja):
    return ja if japanese() else en


def safe(text):
    """Escape control and format characters so model text cannot hide or rewrite terminal output."""
    return ''.join(c if c in '\n\t' or unicodedata.category(c) not in ('Cc', 'Cf', 'Zl', 'Zp') else repr(c)[1:-1]
                   for c in text)


STATES = {'preparing': ('preparing', '準備中'), 'draft': ('waiting for your review', '評価案の確認待ち'),
          'running': ('running', '実行中'), 'incomplete': ('incomplete', '未完了'),
          'completed': ('completed', '完了'), 'unsupported': ('unsupported skill', '対象外のスキル')}

# Ordered (fragment, English, Japanese); the first fragment found in the error text wins.
HINTS = [
    ('cli_not_installed', 'Install the CLI and make sure it is on PATH.',
     'CLIをインストールし、PATHから実行できるようにしてください。'),
    ('unsupported_cli_version', 'Tested versions: Claude Code {claude} / Codex {codex}. To try another version, '
     'run improve prepare again with a new --output and --allow-untested-cli.',
     '検証済みバージョンは Claude Code {claude}／Codex {codex} です。別バージョンで試す場合は、'
     '新しい --output を指定し、improve prepare に --allow-untested-cli を付けてください。'),
    ('unsupported_cli_capability', 'The CLI lacks a required option. Update the CLI or use a tested version.',
     'CLIに必要なオプションがありません。CLIを更新するか、検証済みバージョンを使ってください。'),
    ('login_required', 'Log in with your subscription first: run `claude` and /login, or `codex login`. '
     'API-key logins are not used.',
     '先にサブスクリプションでログインしてください（`claude` で /login、または `codex login`）。'
     'APIキーによるログインは使いません。'),
    ('authentication_required', 'Log in with your subscription first: run `claude` and /login, or `codex login`.',
     '先にサブスクリプションでログインしてください（`claude` で /login、または `codex login`）。'),
    ('unresolved_model_alias', 'The CLI did not report which model the alias selected. Pass a full model ID '
     '(for Codex, the ID shown by /model).',
     'CLIが別名の実モデル名を返しませんでした。完全なモデルIDを指定してください（Codexでは /model に表示されるID）。'),
    ('bundled file references', 'Relative links to other files are not supported; only URLs and #heading links.',
     'ほかのファイルへの相対リンクは未対応です（URLと #見出し へのリンクのみ使えます）。'),
    ('provider_and_explicit_model_required', 'Pass --provider claude|codex and a model name with --model.',
     '--provider claude|codex と --model のモデル名を指定してください。'),
    ('rate_limit', 'The subscription usage limit was reached. Wait, then prepare again in a new directory.',
     'サブスクリプションの利用上限に達しました。時間をおいて、新しい出力先で prepare からやり直してください。'),
    ('active writer', 'Another command is using this directory, usually a running improve run. Wait for it to finish.',
     '別のコマンドがこの保存先を使用中です（通常は実行中の improve run）。終了を待ってください。'),
    ('three cases per split', 'Keep exactly three development and three held-out cases; replace cases instead of adding.',
     '改善用・最終確認用の課題は各3件ちょうどにしてください。追加ではなく差し替えで編集します。'),
    ('already exists', 'Choose a new --output directory; each attempt uses its own directory.',
     '別の --output を指定してください。試行ごとに新しい保存先を使います。'),
    ('approval hash differs', 'The evaluation changed after you reviewed it. Run improve report and review it again.',
     '確認後に評価案が変わりました。improve report を実行し、もう一度確認してください。'),
    ('original skill changed', 'The original SKILL.md changed. Run improve prepare again in a new directory.',
     '元の SKILL.md が変更されました。新しい出力先で improve prepare からやり直してください。'),
    ('provider_profile_changed', 'The CLI version or settings differ from prepare. Prepare again in a new directory.',
     'CLIのバージョンや設定が prepare 時と異なります。新しい出力先で prepare からやり直してください。'),
    ('companion files', 'Skills that reference scripts/, references/ or assets/ are not supported yet.',
     'scripts/・references/・assets/ を参照するスキルはまだ対応していません。'),
    ('timeout', 'A model call exceeded its time limit. Prepare again in a new directory.',
     'モデル呼び出しが時間上限を超えました。新しい出力先で prepare からやり直してください。'),
]


def hint(error):
    from .cli_provider import SUPPORTED
    text = str(error)
    for fragment, en, ja in HINTS:
        if fragment in text:
            return _t(en, ja).format(**SUPPORTED)
    return None


def progress(number, limit, identity):
    return _t('  model call {} (limit {}): {}', '  モデル呼び出し {}回目（上限{}回）: {}').format(number, limit, identity)


def cancelled():
    return _t('Cancelled; nothing was run.', '中止しました。何も実行していません。')


def approval_header(summary, bundle, approval):
    profile = bundle['provider_profile']
    lines = ['', 'CLI: {} {} / {}'.format(profile['provider'], profile['cli_version'], profile['model']),
             _t('Skill: ', '対象スキル: ') + bundle['source_path'],
             _t('Approval SHA256: ', '承認ハッシュ: ') + approval]
    lines += [_t('Warning: ', '注意: ') + item for item in summary['warnings']]
    return '\n'.join(lines)


def confirm_prompt(summary, limits):
    return _t('Run the improvement with this evaluation? Up to {} more model calls and {} minutes. [y/N] ',
              'この評価案で改善を実行しますか？ モデル呼び出しは残り最大{}回、最大{}分かかります。 [y/N] ').format(
        limits['max_calls'] - summary['calls_started'], limits['run_deadline'] // 60)


def _passed(rows):
    checks = [item['passed'] for row in rows for item in row['criteria']]
    return sum(checks), len(checks)


def _required_failures(rows, required):
    return {(row['case_id'], item['id']) for row in rows for item in row['criteria']
            if item['id'] in required and not item['passed']}


def _scores(summary):
    """Criteria passed per split, plus why a higher count may still not be an improvement."""
    comparison = summary['comparison']
    if 'original_heldout' not in comparison:
        return []
    dev = [_passed(comparison[k]) for k in ('baseline_development', 'best_development')]
    held = [_passed(comparison[k]) for k in ('original_heldout', 'best_heldout')]
    harder = _t('  Prepare again in a new directory and make the cases or criteria harder in evaluation.json '
                'before run (see "What to check in the evaluation" in the README).',
                '  新しい保存先で prepare し、run の前に evaluation.json の課題や基準を難しくしてください'
                '（README の「評価案で確認すること」を参照）。')
    if not summary.get('revision_selected'):
        if dev[0][0] == dev[0][1]:
            return [_t('  Development: original {}/{}', '  改善用の課題: 元 {}/{}').format(*dev[0]),
                    _t('  Held-out:    original {}/{}', '  最終確認用:   元 {}/{}').format(*held[0]),
                    _t('  The original already passes every development check, so revisions were skipped.',
                       '  元のスキルが改善用の基準をすべて満たしていたため、改訂は省略しました。'), harder]
        return [_t('  Development: original {}/{}; no revision scored higher',
                   '  改善用の課題: 元 {}/{}。これを上回る改訂案はありませんでした').format(*dev[0]),
                _t('  Held-out:    original {}/{}', '  最終確認用:   元 {}/{}').format(*held[0])]
    lines = [_t('  Development: original {}/{} -> best revision {}/{}',
                '  改善用の課題: 元 {}/{} → 最良の改訂案 {}/{}').format(*dev[0], *dev[1]),
             _t('  Held-out:    original {}/{} -> best revision {}/{}',
                '  最終確認用:   元 {}/{} → 最良の改訂案 {}/{}').format(*held[0], *held[1])]
    required = set(summary.get('required_criteria', ()))
    new = sorted(_required_failures(comparison['best_heldout'], required)
                 - _required_failures(comparison['original_heldout'], required))
    if new:
        lines.append(_t('  Not an improvement: the revision newly fails required checks: ',
                        '  改善とみなさない理由: 改訂案が必須基準で新たに不合格: ')
                     + ', '.join(case + '/' + criterion for case, criterion in new))
    elif held[0][0] == held[0][1]:
        lines += [_t('  The original already passes every held-out check, so this evaluation cannot show '
                     'an improvement.', '  元のスキルが最終確認用の基準をすべて満たしているため、この評価では改善を示せません。'),
                  harder]
    return lines


def render(summary):
    command = 'skill-evolver improve'
    directory = shlex.quote(summary['directory'])
    state = summary.get('state')
    label = STATES.get(state, (str(state), str(state)))
    lines = [_t('State: ', '状態: ') + _t(*label)]
    if 'provider' in summary:
        lines.append('CLI: {} {} / {}'.format(summary['provider'], summary['cli_version'], summary['model']))
    if summary.get('reason'):
        lines.append(_t('Reason: ', '理由: ') + summary['reason'])
        # An unsupported reason is model prose, not a stable code.
        advice = hint(summary['reason']) if state != 'unsupported' else None
        if advice:
            lines.append(_t('Hint: ', 'ヒント: ') + advice)
    lines.append('')
    if state == 'draft':
        lines += [_t('Next steps:', '次の手順:'),
                  _t('  1. Read the evaluation: ', '  1. 評価案を読む: ') + summary['evaluation'],
                  _t('     To change it, edit evaluation.json, then run: ',
                     '     変更する場合は evaluation.json を編集してから実行: ')
                  + command + ' report ' + directory,
                  _t('  2. Start the improvement: ', '  2. 改善を実行: ') + command + ' run ' + directory]
    elif state == 'completed':
        comparison = summary.get('comparison', {})
        improved = comparison.get('status') == 'observed_improvement'
        report_path = os.path.join(summary['directory'], 'comparison.md')
        lines += [_t('Result: ', '結果: ') + (_t('improvement observed on the reviewed evaluation',
                                                 '確認した評価で改善を観測しました') if improved else
                                              _t('no improvement observed; the original skill was not changed',
                                                 '改善は確認できませんでした。元のスキルは変更していません'))]
        lines += _scores(summary)
        lines.append(_t('Comparison report: ', '比較レポート: ') + report_path)
        if improved:
            source = shlex.quote(summary['source_path'])
            candidate = shlex.quote(summary['candidate_path'])
            lines += [_t('Improved skill: ', '改善版のスキル: ') + summary['candidate_path'],
                      '', _t('The original skill was not changed. To adopt the candidate:',
                             '元のスキルは変更していません。採用する場合:'),
                      '  diff -u ' + source + ' ' + candidate,
                      '  cp ' + source + ' ' + source + '.bak-$(date +%Y%m%d-%H%M%S)',
                      '  cp ' + candidate + ' ' + source]
    elif state == 'unsupported':
        lines.append(_t('Choose a self-contained text skill.', '自己完結したテキスト系スキルを選んでください。'))
    else:
        lines.append(_t('Calls are never retried. Start improve prepare again in a new --output directory.',
                        '呼び出しは再試行しません。新しい --output を指定して improve prepare からやり直してください。'))
    if summary.get('warnings'):
        lines += ['', _t('Warnings:', '注意:')] + ['  - ' + item for item in summary['warnings']]
    return '\n'.join(lines)
