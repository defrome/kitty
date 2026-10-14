#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

# The sidebar: the tab bar, when it is on the left edge, drawn as a list of
# workspaces. The current workspace is expanded to show its tabs, the others
# show their git branch and the state of Claude Code running in them.

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from .fast_data_types import Screen
    from .tab_bar import TabBar, TabExtent
    from .tabs import Tab

# Clickable rows that are not tabs get tab ids at or below this, see handle_click()
ACTION_BASE = -1000
NEW_WORKSPACE_ID = ACTION_BASE
FIRST_WORKSPACE_ID = ACTION_BASE - 1


def rgb(color: str) -> int:
    return (int(color.lstrip('#'), 16) << 8) | 2


BG = '#0e0f14'
CURRENT_BG = '#1d1220'
TEXT = '#ffffff'
TEXT2 = '#e4e6ea'
MUTED = '#6e7383'
DIM = '#8a8f9c'
ACCENT = '#ff8cc6'
STATES = {  # claude state -> label and color
    'working': ('◐ думает', '#2de2e6'),
    'waiting': ('● ждёт ответа', '#f9e05c'),
    'done': ('✓ готово', '#5af78e'),
}


class WorkspaceRow(NamedTuple):
    name: str
    is_current: bool
    is_loaded: bool
    branch: str
    changes: int
    ahead: int
    claude: str
    tabs: tuple['Tab', ...]


def claude_state_of(tabs: Sequence['Tab']) -> str:
    ' The most important state of the Claude Code instances running in the windows of the given tabs '
    from .claude_status import status_for_window
    states = set()
    for tab in tabs:
        for w in tab:
            if os.path.basename(w.child.get_foreground_exe() or '') == 'claude':
                states.add(status_for_window(w.id) or 'idle')
    for state in ('waiting', 'working', 'done'):
        if state in states:
            return state
    return ''


def collect(os_window_id: int) -> list[WorkspaceRow]:
    from .fast_data_types import get_boss
    from .titlebar import git_info
    from .workspaces import saved_workspaces, workspace_dir
    boss = get_boss()
    tm = boss.os_window_map.get(os_window_id)
    if tm is None:
        return []
    active_tab = tm.active_tab
    current = active_tab.created_in_session_name if active_tab else ''
    if active_tab is not None and (w := active_tab.active_window) is not None and w.created_in_session_name:
        current = w.created_in_session_name
    saved = saved_workspaces()
    names = list(dict.fromkeys([current] + [t.created_in_session_name for t in tm] + sorted(saved, key=str.lower)))
    rows = []
    for name in names:
        tabs = tuple(t for t in tm if t.created_in_session_name == name)
        if not name and not tabs:
            continue
        cwd = ''
        if tabs:
            cwd = tabs[0].get_cwd_of_active_window() or ''
        elif name in saved:
            cwd = workspace_dir(boss, name, saved[name])
        gi = git_info(cwd) if cwd else None
        rows.append(WorkspaceRow(
            name=name, is_current=name == current, is_loaded=bool(tabs), branch=gi.branch if gi else '',
            changes=gi.changes if gi else 0, ahead=gi.ahead if gi else 0, claude=claude_state_of(tabs), tabs=tabs))
    return rows


def signature(os_window_id: int) -> tuple[object, ...]:
    ' What the sidebar shows, used to redraw it only when that changes '
    return tuple((r.name, r.is_current, r.is_loaded, r.branch, r.changes, r.ahead, r.claude, tuple(tab_label(t) for t in r.tabs)) for r in collect(os_window_id))


class Painter:
    def __init__(self, s: 'Screen'):
        self.s = s
        self.cols = s.columns

    def fill(self, row: int, bg: str, start: int = 0, end: int = -1) -> None:
        if row >= self.s.lines:
            return
        end = self.cols if end < 0 else end
        self.s.cursor.x, self.s.cursor.y = start, row
        self.s.cursor.bg = rgb(bg)
        self.s.draw(' ' * max(0, end - start))

    def text(self, row: int, col: int, text: str, fg: str, bg: str, bold: bool = False, max_width: int = 0) -> int:
        ' Draw text, returns the column after it '
        if row >= self.s.lines or col >= self.cols:
            return col
        max_width = max_width or (self.cols - col)
        if len(text) > max_width:
            text = text[:max(0, max_width - 1)] + '…'
        s = self.s
        s.cursor.x, s.cursor.y = col, row
        s.cursor.fg, s.cursor.bg = rgb(fg), rgb(bg)
        s.cursor.bold = bold
        s.draw(text)
        s.cursor.bold = False
        return col + len(text)


def tab_label(tab: 'Tab') -> str:
    title = tab.name or tab.title or ''
    exe = ''
    if (w := tab.active_window) is not None:
        exe = os.path.basename(w.child.get_foreground_exe() or '')
    is_path = not title or title.startswith(('~', '/'))
    # Prefer the running program, like "claude", over a shell title that is just the directory
    if exe and exe not in ('bash', 'zsh', 'fish', 'sh') and is_path:
        return exe
    if is_path and title:
        return title if title == '~' else os.path.basename(title.rstrip('/')) or title
    return title or exe or 'терминал'


def render(tab_bar: 'TabBar') -> tuple['TabExtent', ...]:
    from .tab_bar import CellRange, TabExtent
    s = tab_bar.screen
    p = Painter(s)
    cols, lines = s.columns, s.lines
    for row in range(lines):
        p.fill(row, BG)
    extents: list[TabExtent] = []
    rows = collect(tab_bar.os_window_id)
    # header
    p.text(0, 1, 'ВОРКСПЕЙСЫ', MUTED, BG)
    p.text(0, cols - 2, '+', DIM, BG)
    extents.append(TabExtent(NEW_WORKSPACE_ID, CellRange(cols - 3, cols - 1), CellRange(0, 0)))
    row = 2
    footer_rows = 3
    limit = max(row, lines - footer_rows)
    for i, ws in enumerate(rows):
        if row >= limit:
            break
        name = ws.name or 'без воркспейса'
        status = STATES.get(ws.claude)
        bg = CURRENT_BG if ws.is_current else BG
        block_start = row
        if ws.is_current:
            p.fill(row, bg, 0, cols)
        right = (status[0], status[1]) if status else (('○', MUTED) if not ws.is_loaded else ('', MUTED))
        name_w = cols - 3 - (len(right[0]) + 1 if right[0] else 0)
        p.text(row, 1, name, TEXT if ws.is_current else (TEXT2 if ws.is_loaded else DIM), bg, bold=ws.is_current, max_width=name_w)
        if right[0]:
            p.text(row, cols - 1 - len(right[0]), right[0], right[1], bg)
        row += 1
        if ws.branch or ws.changes:
            if ws.is_current:
                p.fill(row, bg, 0, cols)
            line = f'⎇ {ws.branch}' if ws.branch else ''
            if ws.changes:
                line += f'  ✚{ws.changes}'
            if ws.ahead:
                line += f' ↑{ws.ahead}'
            p.text(row, 1, line, '#a28aa0' if ws.is_current else MUTED, bg)
            row += 1
        if ws.is_current and ws.tabs:
            p.fill(row, bg, 0, cols)
            row += 1
            for tab in ws.tabs:
                if row >= limit:
                    break
                p.fill(row, bg, 0, cols)
                is_active = tab is tab_bar_active_tab(tab_bar)
                index = next((j for j, t in enumerate(ws.tabs) if t is tab), 0) + 1
                label = f'{"▸" if is_active else " "} {index} {tab_label(tab)}'
                p.text(row, 1, label, ACCENT if is_active else TEXT2, bg, bold=is_active)
                extents.append(TabExtent(tab.id, CellRange(0, cols - 1), CellRange(row, row)))
                row += 1
            p.fill(row, bg, 0, cols)
            row += 1
        if not ws.is_current:
            extents.append(TabExtent(FIRST_WORKSPACE_ID - i, CellRange(0, cols - 1), CellRange(block_start, row - 1)))
        elif not ws.tabs:
            extents.append(TabExtent(FIRST_WORKSPACE_ID - i, CellRange(0, cols - 1), CellRange(block_start, row - 1)))
        row += 1  # spacing between workspaces
    # footer
    if lines > footer_rows + 4:
        sep = lines - footer_rows
        p.text(sep, 1, '─' * (cols - 2), '#1c1e26', BG)
        p.text(sep + 1, 1, 'ctrl+shift+b', DIM, BG)
        p.text(sep + 1, 14, 'скрыть панель', MUTED, BG)
        p.text(sep + 2, 1, 'клик по имени', DIM, BG)
        p.text(sep + 2, 15, '— перейти', MUTED, BG)
    tab_bar.sidebar_rows = rows
    return tuple(extents)


def tab_bar_active_tab(tab_bar: 'TabBar') -> 'Tab | None':
    from .fast_data_types import get_boss
    tm = get_boss().os_window_map.get(tab_bar.os_window_id)
    return tm.active_tab if tm is not None else None


def handle_click(tab_bar: 'TabBar', action_id: int) -> None:
    ' A click on a row of the sidebar that is not a tab '
    from .fast_data_types import get_boss
    from .workspaces import open_workspace, show_workspaces_panel
    boss = get_boss()
    if action_id == NEW_WORKSPACE_ID:
        show_workspaces_panel(boss)
        return
    idx = FIRST_WORKSPACE_ID - action_id
    rows = getattr(tab_bar, 'sidebar_rows', [])
    if 0 <= idx < len(rows):
        ws = rows[idx]
        if ws.tabs:
            tm = boss.os_window_map.get(tab_bar.os_window_id)
            if tm is not None:
                tm.set_active_tab(ws.tabs[0])
        elif ws.name:
            open_workspace(boss, ws.name)
