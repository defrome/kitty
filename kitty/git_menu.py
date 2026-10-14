#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

# The Git panel: shows the state of the repository of the active window and
# runs git operations either as commands in a terminal, or by asking Claude
# Code running in the current tab to do them.

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .boss import Boss
    from .tabs import Tab
    from .window import Window


def is_claude(w: 'Window') -> bool:
    return os.path.basename(w.child.get_foreground_exe() or '') == 'claude'


def claude_window(tab: 'Tab') -> 'Window | None':
    ' The window running Claude Code in the tab, preferring the active one '
    if (w := tab.active_window) is not None and is_claude(w):
        return w
    for w in tab:
        if is_claude(w):
            return w
    return None


def send_to_claude(boss: 'Boss', w: 'Window', prompt: str) -> None:
    from .fast_data_types import add_timer
    tab = w.tabref()
    if tab is not None:
        tab.set_active_window(w)
    w.paste_text(prompt)
    # Submit after the paste has been processed, a newline inside the paste would only add a line
    add_timer(lambda timer_id: w.write_to_child(b'\r'), 0.15, False)


def run_in_terminal(boss: 'Boss', target: 'Window | None', cwd: str, commands: str, execute: bool) -> None:
    ' Type commands into the target window when it is at a shell prompt, else into a new split in cwd '
    from .fast_data_types import add_timer
    if target is not None and target.at_prompt:
        target.paste_text(commands)
        if execute:
            add_timer(lambda timer_id: target.write_to_child(b'\r'), 0.1, False)
        return
    tab = boss.active_tab
    if tab is None:
        return
    w = tab.new_window(cwd=cwd or None, location='hsplit')
    # Wait for the shell to start before typing into it
    def type_commands(timer_id: int | None) -> None:
        w.paste_text(commands)
        if execute:
            add_timer(lambda timer_id: w.write_to_child(b'\r'), 0.1, False)
    add_timer(type_commands, 0.6, False)


def show_git_panel(boss: 'Boss') -> None:
    from .workspaces import show_pixel_panel
    tab = boss.active_tab
    target = boss.active_window
    if tab is None or target is None:
        return
    cwd = target.cwd_of_child or ''
    cw = claude_window(tab)
    data = {'cwd': cwd, 'has_claude': cw is not None}

    def on_result(result: dict[str, Any]) -> None:
        action = result.get('action')
        if action == 'claude':
            if (w := claude_window(tab)) is not None:
                send_to_claude(boss, w, result.get('prompt', ''))
        elif action in ('run', 'insert'):
            run_in_terminal(boss, target, result.get('cwd', cwd), result.get('commands', ''), execute=action == 'run')

    show_pixel_panel(boss, 'git', data, on_result)
