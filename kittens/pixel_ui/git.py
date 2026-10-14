#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

# The data behind the Git panel: the state of the repository, and commit
# messages and commands written by Claude Code running headless.

import os
import shlex
import subprocess
from typing import NamedTuple

GIT_ENV = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'}


class FileChange(NamedTuple):
    status: str  # one letter: M, A, D, R, ?
    path: str
    added: int = -1  # -1 when unknown, for instance for binary or untracked files
    deleted: int = -1


class RepoState(NamedTuple):
    root: str = ''
    branch: str = ''
    upstream: str = ''
    ahead: int = 0
    behind: int = 0
    files: tuple[FileChange, ...] = ()

    @property
    def is_repo(self) -> bool:
        return bool(self.root)

    @property
    def name(self) -> str:
        return os.path.basename(self.root.rstrip('/'))


def git(cwd: str, *args: str, timeout: float = 5) -> str:
    cp = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, timeout=timeout, env=GIT_ENV)
    if cp.returncode != 0:
        raise subprocess.CalledProcessError(cp.returncode, args, cp.stdout, cp.stderr)
    return cp.stdout


def repo_state(cwd: str) -> RepoState:
    try:
        root = git(cwd, 'rev-parse', '--show-toplevel').strip()
    except Exception:
        return RepoState()
    branch = upstream = ''
    ahead = behind = 0
    files: list[FileChange] = []
    try:
        status = git(root, 'status', '--porcelain=v1', '--branch', '--untracked-files=all')
    except Exception:
        status = ''
    for line in status.splitlines():
        if line.startswith('## '):
            head = line[3:]
            if head.startswith('No commits yet on '):
                branch = head[len('No commits yet on '):]
                continue
            branch, _, rest = head.partition('...')
            branch = branch.split(' ')[0]
            upstream = rest.split(' ')[0]
            if '[' in head:
                for part in head[head.index('[') + 1:].rstrip(']').split(', '):
                    kind, _, num = part.partition(' ')
                    if num.isdigit():
                        if kind == 'ahead':
                            ahead = int(num)
                        elif kind == 'behind':
                            behind = int(num)
            continue
        if len(line) < 4:
            continue
        xy, path = line[:2], line[3:]
        if ' -> ' in path:
            path = path.split(' -> ', 1)[1]
        path = path.strip('"')
        letter = '?' if xy == '??' else next((c for c in (xy[1], xy[0]) if c not in ' '), 'M')
        files.append(FileChange(letter, path))
    # Line counts of tracked changes
    counts: dict[str, tuple[int, int]] = {}
    for args in (('diff', 'HEAD', '--numstat'), ('diff', '--cached', '--numstat')):
        try:
            out = git(root, *args)
        except Exception:
            continue
        for line in out.splitlines():
            parts = line.split('\t')
            if len(parts) == 3:
                a, d, p = parts
                counts[p] = (int(a) if a.isdigit() else -1, int(d) if d.isdigit() else -1)
        break
    files = [f._replace(added=counts[f.path][0], deleted=counts[f.path][1]) if f.path in counts else f for f in files]
    return RepoState(root, branch, upstream, ahead, behind, tuple(files))


def run_claude(prompt: str, stdin: str = '', timeout: float = 45) -> str:
    ' Run Claude Code headless with no tools and without saving the session, returns its answer '
    # The prompt goes through stdin: --tools takes a list of values, so it would swallow a prompt passed after it
    text = prompt + ('\n\n' + stdin if stdin else '')
    cp = subprocess.run(
        ['claude', '-p', '--model', 'haiku', '--no-session-persistence', '--tools', ''],
        # Without a controlling terminal: given one (that of the panel), claude tries to interact with it and hangs
        input=text, capture_output=True, text=True, timeout=timeout, start_new_session=True)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or f'claude exited with code {cp.returncode}')
    return cp.stdout.strip()


def diff_for_message(state: RepoState, limit: int = 14000) -> str:
    try:
        diff = git(state.root, 'diff', 'HEAD', timeout=10)
    except Exception:
        try:
            diff = git(state.root, 'diff', '--cached', timeout=10)
        except Exception:
            diff = ''
    untracked = [f.path for f in state.files if f.status == '?']
    if untracked:
        diff += '\nНовые файлы:\n' + '\n'.join(untracked[:50])
    return diff[:limit]


def fallback_message(state: RepoState) -> str:
    ' A commit message for when Claude is not available '
    if len(state.files) == 1:
        f = state.files[0]
        verb = {'A': 'add', '?': 'add', 'D': 'remove', 'R': 'rename'}.get(f.status, 'update')
        return f'{verb} {os.path.basename(f.path)}'
    return f'update {len(state.files)} files'


def generate_message(state: RepoState) -> str:
    prompt = (
        'Напиши сообщение коммита для этих изменений в стиле Conventional Commits: тип на английском '
        '(feat, fix, refactor, docs, chore, test, style), необязательная область в скобках, затем краткое описание по-русски. '
        'Одна строка, не длиннее 72 символов. Ответь только самим сообщением, без кавычек и пояснений.')
    msg = run_claude(prompt, diff_for_message(state)).splitlines()
    msg = [line.strip().strip('`"«»') for line in msg if line.strip()]
    return msg[0][:100] if msg else fallback_message(state)


def generate_command(state: RepoState, wish: str) -> str:
    prompt = (
        'Ты помогаешь с git в терминале bash. Преобразуй пожелание пользователя в одну строку команд bash, '
        'которую можно сразу выполнить в корне репозитория (несколько команд соединяй через &&). '
        f'Текущая ветка: {state.branch or "неизвестна"}, upstream: {state.upstream or "нет"}. '
        'Ответь только командой, без пояснений и без markdown.\n\n'
        f'Пожелание: {wish}')
    out = run_claude(prompt).strip()
    if out.startswith('```'):
        out = '\n'.join(line for line in out.splitlines() if not line.startswith('```'))
    return ' '.join(out.split('\n')).strip()


# Commands for the buttons, run in a shell in the root of the repository {{{
def q(x: str) -> str:
    return shlex.quote(x)


def push_command(state: RepoState) -> str:
    return 'git push' if state.upstream else f'git push -u origin {q(state.branch or "HEAD")}'


def commit_command(message: str) -> str:
    return f'git add -A && git commit -m {q(message)}'


def commit_and_push_command(state: RepoState, message: str) -> str:
    return f'{commit_command(message)} && {push_command(state)}'


def new_branch_command(name: str) -> str:
    return f'git switch -c {q(name)}'
# }}}


# Prompts for Claude Code running in the tab {{{
def claude_commit_prompt(message: str, push: bool) -> str:
    ans = f'Закоммить все текущие изменения в git с сообщением «{message}» (если оно не отражает изменения, поправь его)'
    return ans + (' и запушь в удалённый репозиторий.' if push else '. Пушить не нужно.')


CLAUDE_PULL_PROMPT = 'Сделай git pull и, если будут конфликты, разреши их и расскажи, что поменялось.'
CLAUDE_PUSH_PROMPT = 'Запушь неотправленные коммиты текущей ветки в удалённый репозиторий.'


def claude_branch_prompt(name: str) -> str:
    return f'Создай новую git-ветку «{name}» от текущей и переключись на неё.'
# }}}
