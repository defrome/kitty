#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

# Panels with a real GUI look (popovers, menus, sheets) drawn as images with
# PIL and shown in an overlay window using the kitty graphics protocol. The
# mouse is reported in pixels, so the panels respond to hovering and clicks
# like ordinary GUI widgets. The overlay shows a blurred snapshot of the
# window it covers behind the panel.

import json
import os
import sys
import tempfile
import time
from base64 import standard_b64encode
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from kitty.key_encoding import KeyEvent
from kitty.titlebar import PALETTE, Canvas, icon_plus, icon_save
from kitty.typing_compat import BossType

from ..tui.handler import Handler, kitten_ui, result_handler
from ..tui.loop import EventType, Loop, MouseButton, MouseEvent
from ..tui.operations import MouseTracking, clear_screen, set_cursor_visible, set_window_title

if TYPE_CHECKING:
    from PIL import Image

P = PALETTE
BG_IMAGE_ID, PANEL_IMAGE_ID = 1, 2
APPEAR_STEPS = ((0.25, 10), (0.55, 6), (0.8, 3), (0.95, 1))  # opacity and upward offset of the first frames


class Rect(NamedTuple):
    x: float
    y: float
    w: float
    h: float

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


Hit = tuple[Rect, Any]  # a clickable area and the action it triggers


def icon_close(c: Canvas, x: float, y: float, color: str) -> None:
    c.line([(x + 4.5, y + 4.5), (x + 11.5, y + 11.5)], color, 1.6)
    c.line([(x + 4.5, y + 11.5), (x + 11.5, y + 4.5)], color, 1.6)


def icon_trash(c: Canvas, x: float, y: float, color: str) -> None:
    c.line([(x + 3, y + 4.5), (x + 13, y + 4.5)], color, 1.4)
    c.line([(x + 6.5, y + 4.5), (x + 6.5, y + 2.8), (x + 9.5, y + 2.8), (x + 9.5, y + 4.5)], color, 1.4)
    c.line([(x + 4.5, y + 4.5), (x + 5.3, y + 13.2), (x + 10.7, y + 13.2), (x + 11.5, y + 4.5)], color, 1.4)


def icon_search(c: Canvas, x: float, y: float, color: str) -> None:
    c.draw.ellipse((c.p(x + 2.5), c.p(y + 2.5), c.p(x + 10.5), c.p(y + 10.5)), outline=color, width=max(1, c.p(1.5)))
    c.line([(x + 9.5, y + 9.5), (x + 13.5, y + 13.5)], color, 1.6)


def icon_check(c: Canvas, x: float, y: float, color: str) -> None:
    c.line([(x + 3, y + 8.5), (x + 6.5, y + 12), (x + 13, y + 4.5)], color, 1.8)


# Views {{{
class View:
    ' A panel. Coordinates are in logical pixels relative to the panel. '
    width: float = 460
    title: str = ''
    centered = False  # vertically centered, rather than hanging from the top like a popover
    avail: tuple[float, float] = (1000, 1000)  # the size of the window, in logical pixels, set before drawing

    def on_press(self, action: Any, x: float, y: float) -> bool:
        ' Return True to start dragging, on_drag() is then called on mouse motion until the button is released '
        return False

    def on_drag(self, x: float, y: float) -> None:
        pass

    def on_release(self) -> None:
        pass

    def height(self) -> float:
        raise NotImplementedError()

    def draw(self, c: Canvas, hover: Any) -> list[Hit]:
        raise NotImplementedError()

    def on_click(self, action: Any) -> dict[str, Any] | None:
        ' Return a result to close the panel with it, None to redraw '
        return None

    def on_text(self, text: str) -> dict[str, Any] | None:
        return None

    def on_key(self, key: KeyEvent) -> dict[str, Any] | None:
        return None

    def on_escape(self) -> bool:
        return False

    def state_key(self) -> Any:
        ' Everything other than the hovered item that the drawing depends on, rendered frames are cached by it '
        return None


class WorkspacesView(View):
    title = 'Воркспейсы'
    row_h = 54
    max_rows = 7

    def __init__(self, data: dict[str, Any]):
        self.workspaces: list[dict[str, Any]] = data.get('workspaces', [])
        self.on_close_policy: str = data.get('on_close', 'ask')
        self.policies: dict[str, str] = data.get('policies', {})
        self.query = ''
        self.selected = 0
        self.confirm_delete = ''
        self.last_caret_toggle = time.monotonic()

    def matches(self) -> list[dict[str, Any]]:
        q = self.query.strip().lower()
        return [w for w in self.workspaces if q in w['name'].lower()] if q else list(self.workspaces)

    def state_key(self) -> Any:
        return self.query, self.selected, self.confirm_delete, self.on_close_policy

    def can_create(self) -> bool:
        q = self.query.strip()
        return bool(q) and all(w['name'].lower() != q.lower() for w in self.workspaces)

    def rows(self) -> list[tuple[str, Any]]:
        ans: list[tuple[str, Any]] = [('workspace', w) for w in self.matches()[:self.max_rows]]
        if self.can_create():
            ans.append(('create', self.query.strip()))
        return ans

    def height(self) -> float:
        n = max(1, len(self.rows()))
        return 16 + 32 + 10 + 38 + 12 + n * self.row_h + 12 + 1 + 56

    def draw(self, c: Canvas, hover: Any) -> list[Hit]:
        hits: list[Hit] = []
        W = self.width
        # header
        c.text(18, 32, self.title, 15, '#ffffff', 'SemiBold')
        close = Rect(W - 16 - 28, 18, 28, 28)
        c.circle(close.x + 14, close.y + 14, 14, P.pressed if hover == 'close' else '#262833')
        icon_close(c, close.x + 6, close.y + 6, P.text2)
        hits.append((close, 'close'))
        # search field
        y = 58
        c.rounded(16, y, W - 32, 38, 10, '#121319', '#2c2e3a')
        icon_search(c, 28, y + 11, P.muted)
        if self.query:
            tw = c.text(52, y + 19, c.fit_text(self.query, 13.5, W - 32 - 52), 13.5, P.text)
            c.line([(52 + tw + 2, y + 10), (52 + tw + 2, y + 28)], P.cyan, 1.5)
        else:
            c.line([(52, y + 10), (52, y + 28)], P.cyan, 1.5)
            c.text(58, y + 19, 'Найти или создать воркспейс…', 13.5, P.muted)
        # rows
        y += 38 + 12
        rows = self.rows()
        if not rows:
            c.text(W / 2, y + self.row_h / 2, 'Пока нет сохранённых воркспейсов', 13, P.muted, anchor='mm')
            y += self.row_h
        for i, (kind, item) in enumerate(rows):
            r = Rect(10, y, W - 20, self.row_h - 4)
            is_hover = hover == ('row', i) or (hover is None and i == self.selected) or (
                kind == 'workspace' and hover == ('ask_delete', item['name']))
            if kind == 'create':
                if is_hover:
                    c.rounded(r.x, r.y, r.w, r.h, 11, P.hover)
                icon_plus(c, 22, r.y + r.h / 2 - 8, P.accent)
                c.text(48, r.y + r.h / 2, c.fit_text(f'Создать «{item}»', 14, r.w - 60, 'Medium'), 14, P.accent, 'Medium')
                hits.append((r, ('create', item)))
                y += self.row_h
                continue
            name = item['name']
            if self.confirm_delete == name:
                c.rounded(r.x, r.y, r.w, r.h, 11, '#2e1417')
                c.text(22, r.y + r.h / 2, c.fit_text(f'Удалить «{name}»?', 13.5, r.w - 190, 'Medium'), 13.5, '#ffb3ba', 'Medium')
                cancel = Rect(r.x + r.w - 10 - 80, r.y + 10, 80, r.h - 20)
                delete = Rect(cancel.x - 8 - 84, r.y + 10, 84, r.h - 20)
                c.rounded(delete.x, delete.y, delete.w, delete.h, 8, '#ff7a85' if hover == ('delete', name) else '#ff4d5e')
                c.text(delete.x + delete.w / 2, delete.y + delete.h / 2, 'Удалить', 13, '#1a0b0d', 'SemiBold', anchor='mm')
                c.rounded(cancel.x, cancel.y, cancel.w, cancel.h, 8, P.pressed if hover == 'cancel_delete' else '#2b2d38')
                c.text(cancel.x + cancel.w / 2, cancel.y + cancel.h / 2, 'Отмена', 13, P.text, 'Medium', anchor='mm')
                hits += [(delete, ('delete', name)), (cancel, 'cancel_delete')]
                y += self.row_h
                continue
            if item.get('current'):
                c.rounded(r.x, r.y, r.w, r.h, 11, '#3a1a2e' if is_hover else P.accent_bg)
            elif is_hover:
                c.rounded(r.x, r.y, r.w, r.h, 11, P.hover)
            dot = P.accent if item.get('current') else (P.cyan if item.get('loaded') else '#4a4f5c')
            c.circle(28, r.y + 18, 4, dot)
            name_color = '#ffffff' if item.get('current') or item.get('loaded') else P.text2
            right_w = 150
            c.text(44, r.y + 18, c.fit_text(name, 14, r.w - 44 - right_w, 'SemiBold' if item.get('current') else 'Medium'), 14, name_color,
                   'SemiBold' if item.get('current') else 'Medium')
            if detail := item.get('detail', ''):
                c.text(44, r.y + 36, c.fit_text(detail, 12, r.w - 44 - 20), 12, P.muted)
            trash = Rect(r.x + r.w - 10 - 30, r.y + 4, 30, 30)
            hits.append((r, ('row', i)))
            if is_hover and hover is not None:
                c.rounded(trash.x, trash.y, trash.w, trash.h, 8, '#3a2026' if hover == ('ask_delete', name) else None)
                icon_trash(c, trash.x + 7, trash.y + 7, '#ff7a85' if hover == ('ask_delete', name) else P.muted)
                hits.append((trash, ('ask_delete', name)))
            else:
                status = item.get('status', '')
                if item.get('current'):
                    icon_check(c, r.x + r.w - 10 - 16, r.y + 10, P.accent)
                    status_x = r.x + r.w - 10 - 16 - 8
                else:
                    status_x = r.x + r.w - 12
                c.text(status_x, r.y + 18, status, 12, P.muted, anchor='rm')
            y += self.row_h
        # footer
        y += 12
        c.line([(16, y), (W - 16, y)], '#262833', 1)
        y += 12
        save = Rect(16, y, 196, 32)
        c.rounded(save.x, save.y, save.w, save.h, 9, P.pressed if hover == 'save_current' else '#20222b', '#2c2e3a')
        icon_save(c, save.x + 10, save.y + 8, P.text2)
        c.text(save.x + 34, save.y + 16, 'Сохранить текущий', 13, P.text, 'Medium')
        hits.append((save, 'save_current'))
        label = 'При закрытии: ' + self.policies.get(self.on_close_policy, self.on_close_policy)
        lw = c.text_width(label, 12.5) + 24
        policy = Rect(W - 16 - lw, y, lw, 32)
        if hover == 'policy':
            c.rounded(policy.x, policy.y, policy.w, policy.h, 9, P.hover)
        c.text(policy.x + policy.w - 12, policy.y + 16, label, 12.5, P.text2 if hover == 'policy' else P.muted, anchor='rm')
        hits.append((policy, 'policy'))
        return hits

    def activate_row(self, i: int) -> dict[str, Any] | None:
        rows = self.rows()
        if not 0 <= i < len(rows):
            return None
        kind, item = rows[i]
        if kind == 'create':
            return {'action': 'new', 'name': item}
        return {'action': 'open', 'name': item['name']}

    def on_click(self, action: Any) -> dict[str, Any] | None:
        self.confirm_delete = '' if action not in ('cancel_delete',) and not (isinstance(action, tuple) and action[0] == 'delete') else self.confirm_delete
        if action == 'close':
            return {'action': 'cancel'}
        if action == 'save_current':
            return {'action': 'save_current'}
        if action == 'policy':
            keys = list(self.policies) or ['ask']
            self.on_close_policy = keys[(keys.index(self.on_close_policy) + 1) % len(keys)] if self.on_close_policy in keys else keys[0]
            from kitty.workspaces import read_state, write_state
            write_state({**read_state(), 'on_close': self.on_close_policy})
            return None
        if action == 'cancel_delete':
            self.confirm_delete = ''
            return None
        if isinstance(action, tuple):
            kind, val = action
            if kind == 'row':
                return self.activate_row(val)
            if kind == 'create':
                return {'action': 'new', 'name': val}
            if kind == 'ask_delete':
                self.confirm_delete = val
                return None
            if kind == 'delete':
                return {'action': 'delete', 'name': val}
        return None

    def on_text(self, text: str) -> dict[str, Any] | None:
        self.query += text
        self.selected = 0
        self.confirm_delete = ''
        return None

    def on_escape(self) -> bool:
        ' Return True if the escape key was used up by the view '
        if self.confirm_delete:
            self.confirm_delete = ''
            return True
        return False

    def on_key(self, key: KeyEvent) -> dict[str, Any] | None:
        n = len(self.rows())
        if self.confirm_delete and key.matches('enter'):
            return {'action': 'delete', 'name': self.confirm_delete}
        if key.matches('backspace'):
            self.query = self.query[:-1]
            self.selected = 0
        elif key.matches('ctrl+backspace') or key.matches('ctrl+u'):
            self.query = ''
            self.selected = 0
        elif key.matches('down') or key.matches('tab'):
            self.selected = (self.selected + 1) % max(1, n)
        elif key.matches('up') or key.matches('shift+tab'):
            self.selected = (self.selected - 1) % max(1, n)
        elif key.matches('enter'):
            return self.activate_row(self.selected)
        elif key.matches('delete'):
            rows = self.rows()
            if 0 <= self.selected < len(rows) and rows[self.selected][0] == 'workspace':
                self.confirm_delete = rows[self.selected][1]['name']
        return None


class SettingsView(View):
    title = 'Настройки'
    centered = True
    sections = (('appearance', 'Внешний вид'), ('terminal', 'Терминал'), ('cursor', 'Курсор и анимации'),
                ('workspaces', 'Воркспейсы'), ('keys', 'Горячие клавиши'))
    nav_w = 214
    row_h = 48
    shortcuts = (
        ('ctrl+shift+b', 'Показать или скрыть сайдбар'),
        ('ctrl+shift+t', 'Новая вкладка в текущем воркспейсе'),
        ('ctrl+shift+\\', 'Разделить вправо'),
        ('ctrl+shift+/', 'Разделить вниз'),
        ('alt+стрелки', 'Перейти в соседнюю панель'),
        ('ctrl+shift+z', 'Развернуть панель на всю вкладку'),
        ('ctrl+shift+=  /  -', 'Крупнее / мельче шрифт'),
        ('клик по имени в шапке', 'Список воркспейсов'),
    )

    def __init__(self, data: dict[str, Any]):
        from . import settings as S
        self.S = S
        self.s: dict[str, Any] = S.load()
        self.fonts: list[str] = data.get('fonts') or S.monospace_families() or [self.s['font_family']]
        if self.s['font_family'] not in self.fonts:
            self.fonts.insert(0, self.s['font_family'])
        from kitty.workspaces import ON_CLOSE_POLICIES, read_state
        self.ws: dict[str, Any] = read_state()
        self.policies = ON_CLOSE_POLICIES
        self.section = data.get('section', 'appearance')
        self.sliders: dict[str, tuple[float, float, float, float, float]] = {}  # key -> x, width, lo, hi, step
        self.dragging = ''
        self.apply: Callable[[str, bool], None] = lambda key, live: None  # set by the host

    @property
    def width(self) -> float:  # type: ignore[override]
        return max(560, min(880, self.avail[0] - 32))

    def height(self) -> float:
        return max(420, min(640, self.avail[1] - 32))

    def state_key(self) -> Any:
        return self.section, tuple(sorted(self.s.items())), tuple(sorted((k, str(v)) for k, v in self.ws.items()))

    # widgets {{{
    def label(self, c: Canvas, x: float, y: float, text: str, sub: str = '') -> None:
        if sub:
            c.text(x, y + self.row_h / 2 - 8, text, 13.5, P.text)
            c.text(x, y + self.row_h / 2 + 9, sub, 11.5, P.muted)
        else:
            c.text(x, y + self.row_h / 2, text, 13.5, P.text)

    def slider(self, c: Canvas, hits: list[Hit], hover: Any, key: str, x: float, y: float, w: float, lo: float, hi: float, step: float, fmt: str) -> None:
        val = float(self.s[key])
        frac = 0 if hi == lo else (min(hi, max(lo, val)) - lo) / (hi - lo)
        cy = y + self.row_h / 2
        tw = w - 64
        c.rounded(x, cy - 2, tw, 4, 2, '#2b2d38')
        c.rounded(x, cy - 2, max(4, tw * frac), 4, 2, P.accent)
        active = self.dragging == key or hover == ('slider', key)
        c.circle(x + tw * frac, cy, 10 if active else 9, '#ffffff')
        c.text(x + w, cy, fmt.format(val), 12.5, P.text2 if active else P.muted, anchor='rm')
        self.sliders[key] = (x, tw, lo, hi, step)
        hits.append((Rect(x - 12, y, tw + 24, self.row_h), ('slider', key)))

    def toggle(self, c: Canvas, hits: list[Hit], hover: Any, key: str, x: float, y: float, on: bool) -> None:
        cy = y + self.row_h / 2
        c.rounded(x - 42, cy - 12, 42, 24, 12, P.accent if on else ('#383a46' if hover == ('toggle', key) else '#2b2d38'))
        c.circle(x - 12 if on else x - 30, cy, 10, '#ffffff' if on else '#a7abb6')
        hits.append((Rect(x - 50, y, 50, self.row_h), ('toggle', key)))

    def segmented(self, c: Canvas, hits: list[Hit], hover: Any, key: str, right: float, y: float, options: tuple[tuple[Any, str], ...], current: Any) -> None:
        widths = [c.text_width(label, 12.5, 'Medium') + 24 for _, label in options]
        total = sum(widths) + 4
        x = right - total
        cy = y + self.row_h / 2
        c.rounded(x, cy - 15, total, 30, 9, '#121319', '#23252e')
        x += 2
        for (val, label), w in zip(options, widths):
            selected = val == current
            if selected:
                c.rounded(x, cy - 13, w, 26, 7, '#2b2d38')
            elif hover == ('seg', key, val):
                c.rounded(x, cy - 13, w, 26, 7, '#20222b')
            c.text(x + w / 2, cy, label, 12.5, '#ffffff' if selected else P.muted, 'Medium', anchor='mm')
            hits.append((Rect(x, cy - 13, w, 26), ('seg', key, val)))
            x += w

    def group(self, c: Canvas, x: float, y: float, w: float, rows: int) -> None:
        c.rounded(x, y, w, rows * self.row_h + 8, 14, '#121319', '#23252e')
        for i in range(1, rows):
            ly = y + 4 + i * self.row_h
            c.line([(x + 16, ly), (x + w - 16, ly)], '#1f2129', 1)
    # }}}

    def draw(self, c: Canvas, hover: Any) -> list[Hit]:
        hits: list[Hit] = []
        W, H = self.width, self.height()
        nw = self.nav_w
        # navigation
        c.draw.rounded_rectangle(c.box(0, 0, nw, H), radius=c.p(16), fill='#121319', corners=(True, False, False, True))
        c.line([(nw, 0), (nw, H)], '#23252e', 1)
        c.text(24, 34, 'Настройки', 16, '#ffffff', 'SemiBold')
        y = 62
        for key, label in self.sections:
            r = Rect(12, y, nw - 24, 34)
            if key == self.section:
                c.rounded(r.x, r.y, r.w, r.h, 9, P.accent_bg)
            elif hover == ('section', key):
                c.rounded(r.x, r.y, r.w, r.h, 9, '#1c1e26')
            c.text(r.x + 12, r.y + 17, label, 13.5, '#ffffff' if key == self.section else P.text2, 'Medium' if key == self.section else 'Regular')
            hits.append((r, ('section', key)))
            y += 38
        c.text(24, H - 34, 'Применяется сразу', 11.5, P.muted)
        c.text(24, H - 18, 'и сохраняется в settings.conf', 11.5, P.muted)
        # content
        x0, cw = nw + 28, W - nw - 56
        title = dict(self.sections)[self.section]
        c.text(x0, 36, title, 18, '#ffffff', 'SemiBold')
        close = Rect(W - 20 - 28, 20, 28, 28)
        c.circle(close.x + 14, close.y + 14, 14, P.pressed if hover == 'close' else '#262833')
        icon_close(c, close.x + 6, close.y + 6, P.text2)
        hits.append((close, 'close'))
        self.sliders = {}
        getattr(self, 'draw_' + self.section)(c, hits, hover, x0, 70, cw)
        return hits

    def rows_group(self, c: Canvas, x: float, y: float, w: float, n: int) -> list[float]:
        self.group(c, x, y, w, n)
        return [y + 4 + i * self.row_h for i in range(n)]

    def draw_appearance(self, c: Canvas, hits: list[Hit], hover: Any, x: float, y: float, w: float) -> None:
        c.text(x, y + 8, 'Тема', 12, P.muted)
        y += 22
        n = len(self.S.THEMES)
        gap = 10
        tw = (w - gap * (n - 1)) / n
        for i, theme in enumerate(self.S.THEMES):
            tx = x + i * (tw + gap)
            selected = self.s['theme'] == theme.name
            border = P.accent if selected else ('#3a3c48' if hover == ('theme', theme.name) else '#262833')
            c.rounded(tx, y, tw, 92, 12, '#121319', border)
            if selected:
                c.rounded(tx + 1, y + 1, tw - 2, 90, 11, None, P.accent)
            bg, prompt, accent, text = theme.preview
            c.rounded(tx + 7, y + 7, tw - 14, 52, 7, bg)
            c.text(tx + 14, y + 22, '~ $', 10.5, prompt)
            c.text(tx + 14 + c.text_width('~ $ ', 10.5), y + 22, 'git', 10.5, accent)
            c.text(tx + 14, y + 40, 'ready', 10.5, text)
            c.text(tx + 12, y + 76, theme.title, 12.5, '#ffffff' if selected else P.text2, 'Medium' if selected else 'Regular')
            hits.append((Rect(tx, y, tw, 92), ('theme', theme.name)))
        y += 108
        rows = self.rows_group(c, x, y, w, 4)
        sx, sw = x + 200, w - 200 - 16
        self.label(c, x + 16, rows[0], 'Прозрачность окна')
        self.slider(c, hits, hover, 'background_opacity', sx, rows[0], sw, 0.3, 1.0, 0.01, '{:.0%}')
        self.label(c, x + 16, rows[1], 'Размытие фона')
        self.slider(c, hits, hover, 'background_blur', sx, rows[1], sw, 0, 64, 1, '{:.0f}')
        self.label(c, x + 16, rows[2], 'Размер шрифта')
        self.slider(c, hits, hover, 'font_size', sx, rows[2], sw, 9, 24, 0.5, '{:g} pt')
        self.label(c, x + 16, rows[3], 'Шрифт')
        font = self.s['font_family']
        fw = min(260, c.text_width(font, 12.5) + 64)
        fr = Rect(x + w - 16 - fw, rows[3] + self.row_h / 2 - 15, fw, 30)
        c.rounded(fr.x, fr.y, fr.w, fr.h, 8, P.pressed if hover == 'font' else '#20222b', '#2c2e3a')
        c.text(fr.x + 12, fr.y + 15, c.fit_text(font, 12.5, fr.w - 40), 12.5, P.text)
        c.line([(fr.x + fr.w - 22, fr.y + 12), (fr.x + fr.w - 16, fr.y + 18), (fr.x + fr.w - 10, fr.y + 12)], P.muted, 1.5)
        hits.append((fr, 'font'))

    def draw_terminal(self, c: Canvas, hits: list[Hit], hover: Any, x: float, y: float, w: float) -> None:
        rows = self.rows_group(c, x, y, w, 4)
        sx, sw = x + 230, w - 230 - 16
        self.label(c, x + 16, rows[0], 'Отступы вокруг текста')
        self.slider(c, hits, hover, 'window_padding_width', sx, rows[0], sw, 0, 32, 1, '{:.0f} pt')
        self.label(c, x + 16, rows[1], 'Межстрочный интервал')
        self.slider(c, hits, hover, 'cell_height', sx, rows[1], sw, 80, 160, 5, '{:.0f}%')
        self.label(c, x + 16, rows[2], 'Яркость неактивных панелей')
        self.slider(c, hits, hover, 'inactive_text_alpha', sx, rows[2], sw, 0.3, 1.0, 0.05, '{:.0%}')
        self.label(c, x + 16, rows[3], 'История прокрутки', 'строк в каждом терминале')
        self.segmented(c, hits, hover, 'scrollback_lines', x + w - 16, rows[3], tuple((n, f'{n // 1000} тыс') for n in self.S.SCROLLBACK_CHOICES),
                       int(self.s['scrollback_lines']))

    def draw_cursor(self, c: Canvas, hits: list[Hit], hover: Any, x: float, y: float, w: float) -> None:
        rows = self.rows_group(c, x, y, w, 5)
        self.label(c, x + 16, rows[0], 'Форма курсора')
        self.segmented(c, hits, hover, 'cursor_shape', x + w - 16, rows[0], self.S.CURSOR_SHAPES, self.s['cursor_shape'])
        self.label(c, x + 16, rows[1], 'След курсора', 'плавно тянется за курсором при прыжках')
        self.toggle(c, hits, hover, 'cursor_trail', x + w - 16, rows[1], int(self.s['cursor_trail']) > 0)
        self.label(c, x + 16, rows[2], 'Скорость следа')
        self.slider(c, hits, hover, 'cursor_trail_speed', x + 230, rows[2], w - 230 - 16, 0, 1, 0.05, '{:.0%}')
        self.label(c, x + 16, rows[3], 'Мигание курсора')
        self.toggle(c, hits, hover, 'cursor_blink', x + w - 16, rows[3], bool(self.s['cursor_blink']))
        self.label(c, x + 16, rows[4], 'Плавная прокрутка', 'попиксельно и с инерцией на тачпаде')
        self.toggle(c, hits, hover, 'smooth_scroll', x + w - 16, rows[4], bool(self.s['smooth_scroll']))

    def draw_workspaces(self, c: Canvas, hits: list[Hit], hover: Any, x: float, y: float, w: float) -> None:
        rows = self.rows_group(c, x, y, w, 3)
        self.label(c, x + 16, rows[0], 'Открывать воркспейсы при запуске', 'те, что были открыты при закрытии kitty')
        self.toggle(c, hits, hover, 'ws:restore_on_startup', x + w - 16, rows[0], bool(self.ws.get('restore_on_startup', True)))
        self.label(c, x + 16, rows[1], 'При закрытии kitty')
        self.segmented(c, hits, hover, 'ws:on_close', x + w - 16, rows[1], (('ask', 'Спрашивать'), ('save', 'Сохранять'), ('discard', 'Не сохранять')),
                       self.ws.get('on_close', 'ask'))
        self.label(c, x + 16, rows[2], 'Продолжать сессии Claude', 'claude --resume с тем же диалогом')
        self.toggle(c, hits, hover, 'ws:resume_claude', x + w - 16, rows[2], bool(self.ws.get('resume_claude', True)))
        c.text(x + 4, rows[2] + self.row_h + 30, 'Воркспейсы сохраняются сами: каждые 30 секунд и после открытия', 12, P.muted)
        c.text(x + 4, rows[2] + self.row_h + 48, 'или закрытия вкладок и панелей.', 12, P.muted)

    def draw_keys(self, c: Canvas, hits: list[Hit], hover: Any, x: float, y: float, w: float) -> None:
        rows = self.rows_group(c, x, y, w, len(self.shortcuts))
        for (keys, desc), ry in zip(self.shortcuts, rows):
            c.text(x + 16, ry + self.row_h / 2, desc, 13.5, P.text)
            kw = c.text_width(keys, 12) + 18
            c.rounded(x + w - 16 - kw, ry + self.row_h / 2 - 12, kw, 24, 6, '#20222b', '#2c2e3a')
            c.text(x + w - 16 - kw / 2, ry + self.row_h / 2, keys, 12, P.text2, anchor='mm')

    # interaction {{{
    def set_slider(self, key: str, x: float) -> None:
        sx, sw, lo, hi, step = self.sliders[key]
        frac = min(1, max(0, (x - sx) / sw))
        val = lo + frac * (hi - lo)
        val = round(round(val / step) * step, 4)
        if isinstance(self.s[key], int) and float(step).is_integer():
            val = int(val)
        if val != self.s[key]:
            self.s[key] = val
            self.apply(key, True)

    def on_press(self, action: Any, x: float, y: float) -> bool:
        if isinstance(action, tuple) and action[0] == 'slider':
            self.dragging = action[1]
            self.set_slider(action[1], x)
            return True
        return False

    def on_drag(self, x: float, y: float) -> None:
        if self.dragging:
            self.set_slider(self.dragging, x)

    def on_release(self) -> None:
        if self.dragging:
            key, self.dragging = self.dragging, ''
            self.apply(key, False)

    def on_click(self, action: Any) -> dict[str, Any] | None:
        if action == 'close':
            return {'action': 'cancel'}
        if action == 'font':
            i = self.fonts.index(self.s['font_family']) if self.s['font_family'] in self.fonts else -1
            self.s['font_family'] = self.fonts[(i + 1) % len(self.fonts)]
            self.apply('font_family', False)
            return None
        if not isinstance(action, tuple):
            return None
        kind = action[0]
        if kind == 'section':
            self.section = action[1]
        elif kind == 'theme':
            self.s['theme'] = action[1]
            self.apply('theme', False)
        elif kind == 'toggle':
            key = action[1]
            if key.startswith('ws:'):
                self.set_workspace_option(key[3:], not self.ws.get(key[3:], True))
            elif key == 'cursor_trail':
                self.s[key] = 0 if int(self.s[key]) > 0 else 8
                self.apply(key, False)
            else:
                self.s[key] = not self.s[key]
                self.apply(key, False)
        elif kind == 'seg':
            key, val = action[1], action[2]
            if key.startswith('ws:'):
                self.set_workspace_option(key[3:], val)
            else:
                self.s[key] = val
                self.apply(key, False)
        return None

    def set_workspace_option(self, key: str, val: Any) -> None:
        from kitty.workspaces import read_state, write_state
        self.ws[key] = val
        write_state({**read_state(), key: val})

    def on_key(self, key: KeyEvent) -> dict[str, Any] | None:
        keys = [k for k, _ in self.sections]
        if key.matches('down') or key.matches('tab'):
            self.section = keys[(keys.index(self.section) + 1) % len(keys)]
        elif key.matches('up') or key.matches('shift+tab'):
            self.section = keys[(keys.index(self.section) - 1) % len(keys)]
        return None
    # }}}


VIEWS: dict[str, Callable[[dict[str, Any]], View]] = {'workspaces': WorkspacesView, 'settings': SettingsView}
# }}}


class RemoteControl:
    '''
    Sends remote control commands to kitty from a background thread, so that dragging a slider never waits
    for them. Commands are keyed: while one runs, only the latest of the pending commands with a key is kept.
    '''

    def __init__(self, run: Callable[[list[str]], Any] | None):
        import threading
        self.run = run
        self.pending: dict[str, list[str]] = {}
        self.lock = threading.Lock()
        self.wakeup = threading.Event()
        if run is not None:
            threading.Thread(target=self.worker, daemon=True, name='RemoteControl').start()

    def send(self, key: str, cmd: list[str]) -> None:
        if self.run is None:
            return
        with self.lock:
            self.pending.pop(key, None)
            self.pending[key] = cmd
        self.wakeup.set()

    def worker(self) -> None:
        import subprocess
        while True:
            self.wakeup.wait()
            with self.lock:
                if not self.pending:
                    self.wakeup.clear()
                    continue
                key = next(iter(self.pending))
                cmd = self.pending.pop(key)
            try:
                assert self.run is not None
                self.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            except Exception:
                pass


class PixelUI(Handler):
    mouse_tracking = MouseTracking.full

    def __init__(self, view: View, background: str, remote_control: Callable[..., Any] | None = None):
        self.view = view
        self.rc = RemoteControl(remote_control)
        self.dragging = False
        if isinstance(view, SettingsView):
            view.apply = self.apply_setting
        self.background_path = background
        self.result: dict[str, Any] = {'action': 'cancel'}
        self.hits: list[Hit] = []
        self.hover: Any = None
        self.panel_rect = (0, 0, 0, 0)  # x, y, w, h in pixels of the panel itself, without its shadow
        self.panel_origin = (0, 0)  # where the panel image (with its shadow margin) is placed, in pixels
        self.margin_px = 0
        self.scale = 1.
        self.shadow_cache: tuple[Any, 'Image.Image', 'Image.Image'] | None = None  # size, shadow, mask for the rounded corners
        self.frame_cache: dict[Any, tuple['Image.Image', list[Hit]]] = {}
        self.frame_cache_size: Any = None
        self.prerender_scheduled = False
        self.appear_step = 0
        self.panel_slot = 0  # frames alternate between two image ids, so that the old one is removed only once the new one is shown

    # Graphics {{{
    def transmit(self, img: 'Image.Image', image_id: int, x: int, y: int, z: int) -> None:
        cw, ch = self.screen_size.cell_width, self.screen_size.cell_height
        if x < 0 or y < 0:
            # Images cannot be placed outside the window: cut off what sticks out (typically part of a
            # shadow), rather than moving the image, which would offset it from where the mouse is tested
            img = img.crop((max(0, -x), max(0, -y), img.width, img.height))
            x, y = max(0, x), max(0, y)
        with tempfile.NamedTemporaryFile(prefix='tty-graphics-protocol-', suffix='.rgba', dir='/dev/shm' if os.path.isdir('/dev/shm') else None, delete=False) as f:
            f.write(img.tobytes())
        path = standard_b64encode(f.name.encode()).decode()
        cmd = f'a=T,f=32,t=t,s={img.width},v={img.height},i={image_id},p=1,q=2,C=1,z={z},X={x % cw},Y={y % ch}'
        self.write(f'\x1b[{y // ch + 1};{x // cw + 1}H\x1b_G{cmd};{path}\x1b\\')

    def draw_background(self) -> None:
        from PIL import Image, ImageFilter
        W, H = self.screen_size.width, self.screen_size.height
        img: Image.Image | None = None
        if self.background_path:
            try:
                img = Image.open(self.background_path).convert('RGBA').resize((W, H))
                img = img.filter(ImageFilter.GaussianBlur(10 * self.scale))
            except Exception:
                img = None
            finally:
                try:
                    os.remove(self.background_path)
                except OSError:
                    pass
        if img is None:
            img = Image.new('RGBA', (W, H), (11, 12, 16, 255))
        img.alpha_composite(Image.new('RGBA', (W, H), (8, 9, 12, 140)))
        self.transmit(img, BG_IMAGE_ID, 0, 0, 0)

    def shadow_and_mask(self, pw: int, ph: int, m: int) -> tuple['Image.Image', 'Image.Image']:
        ' The drop shadow and the rounded corners only depend on the size of the panel, so are computed once '
        from PIL import Image, ImageDraw, ImageFilter
        s = self.scale
        key = (pw, ph, m, s)
        if self.shadow_cache is None or self.shadow_cache[0] != key:
            shadow = Image.new('RGBA', (pw + 2 * m, ph + 2 * m), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle((m, m + round(8 * s), m + pw, m + ph + round(8 * s)), radius=round(16 * s), fill=(0, 0, 0, 170))
            shadow = shadow.filter(ImageFilter.GaussianBlur(14 * s))
            ss = 4
            mask = Image.new('L', (pw * ss, ph * ss), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, pw * ss - 1, ph * ss - 1), radius=round(16 * s * ss), fill=255)
            self.shadow_cache = key, shadow, mask.reduce(ss)
        return self.shadow_cache[1], self.shadow_cache[2]

    def render_frame(self, pw: int, ph: int, m: int, hover: Any) -> tuple['Image.Image', list[Hit]]:
        view = self.view
        c = Canvas(pw, ph, self.scale, '#1a1b22', 'RGB')
        c.rounded(0, 0, view.width, view.height(), 16, None, '#2c2e3a')
        hits = view.draw(c, hover)
        shadow, mask = self.shadow_and_mask(pw, ph, m)
        panel = c.finish().convert('RGBA')
        panel.putalpha(mask)
        frame = shadow.copy()
        frame.alpha_composite(panel, (m, m))
        return frame, hits

    def apply_setting(self, key: str, live: bool) -> None:
        ' Called by the settings view: live is True while a slider is being dragged '
        view = self.view
        assert isinstance(view, SettingsView)
        val = view.s[key]
        if key == 'background_opacity':
            self.rc.send(key, ['set-background-opacity', '--all', f'{float(val):.2f}'])
        elif key == 'font_size':
            self.rc.send(key, ['set-font-size', '--all', f'{float(val):g}'])
        if live:
            return
        view.S.save(view.s)
        from kitty.constants import config_dir
        self.rc.send('load-config', ['load-config', os.path.join(config_dir, 'kitty.conf')])

    def draw_panel(self) -> None:
        s = self.scale
        view = self.view
        W, H = self.screen_size.width, self.screen_size.height
        view.avail = (W / s, H / s)
        pw, ph = round(view.width * s), round(view.height() * s)
        m = self.margin_px = round(28 * s)
        x0 = max(0, (W - pw) // 2)
        y0 = max(0, (H - ph) // 2) if view.centered else round(10 * s)
        self.panel_rect = (x0, y0, pw, ph)
        self.panel_origin = (x0 - m, y0 - m)
        if self.frame_cache_size != (pw, ph, s):
            self.frame_cache.clear()
            self.frame_cache_size = (pw, ph, s)
        key = (view.state_key(), self.hover)
        if (cached := self.frame_cache.get(key)) is None:
            if len(self.frame_cache) > 64:
                self.frame_cache.clear()
            cached = self.frame_cache[key] = self.render_frame(pw, ph, m, self.hover)
        frame, self.hits = cached
        self.schedule_prerender()
        if dump := os.environ.get('KITTY_FORK_TITLEBAR_DUMP'):  # for debugging: the layout, to drive the panel with synthetic mouse events
            with open(dump + '.panel.json', 'w') as f:
                json.dump({'scale': s, 'screen': [W, H], 'panel_rect': self.panel_rect, 'sliders': getattr(view, 'sliders', {}),
                           'hits': [(list(r), str(a)) for r, a in self.hits]}, f)
        if self.appear_step < len(APPEAR_STEPS):
            # Fade in while sliding down a little, like macOS popovers
            opacity, dy = APPEAR_STEPS[self.appear_step]
            frame = frame.copy()
            frame.putalpha(frame.getchannel('A').point(lambda a: int(a * opacity)))
            self.show_frame(frame, x0 - m, y0 - m - round(dy * s))
            self.appear_step += 1
            self.asyncio_loop.call_later(1 / 60, self.draw_panel)
            return
        self.show_frame(frame, x0 - m, y0 - m)

    def show_frame(self, frame: 'Image.Image', x: int, y: int) -> None:
        old = PANEL_IMAGE_ID + self.panel_slot
        self.panel_slot ^= 1
        new = PANEL_IMAGE_ID + self.panel_slot
        # The new frame goes above the old one, which is deleted only afterwards, so nothing flickers
        self.transmit(frame, new, x, y, 2 + self.panel_slot)
        self.write(f'\x1b_Ga=d,d=I,i={old},q=2\x1b\\')
    # }}}

    def schedule_prerender(self) -> None:
        if not self.prerender_scheduled:
            self.prerender_scheduled = True
            self.asyncio_loop.call_later(0.05, self.prerender_one)

    def prerender_one(self) -> None:
        ' Render, one per event loop iteration, the frames for hovering each control, so that hovering never waits for rendering '
        self.prerender_scheduled = False
        pw, ph, s = self.frame_cache_size or (0, 0, 0)
        if not pw:
            return
        state = self.view.state_key()
        for _, action in self.hits:
            key = (state, action)
            if key not in self.frame_cache:
                self.frame_cache[key] = self.render_frame(pw, ph, self.margin_px, action)
                self.schedule_prerender()
                return

    def initialize(self) -> None:
        self.cmd.set_cursor_visible(False)
        self.write(set_window_title(self.view.title))
        self.scale = max(1., min(3., round(self.screen_size.cell_height / 22 * 4) / 4))
        self.write(clear_screen())
        self.draw_background()
        self.draw_panel()

    def finalize(self) -> None:
        self.write('\x1b_Ga=d,d=A,q=2\x1b\\')
        self.write(set_cursor_visible(True))

    def on_resize(self, new_size: Any) -> None:
        super().on_resize(new_size)
        self.write(clear_screen())
        self.draw_panel()

    def finish(self, result: dict[str, Any] | None) -> None:
        if result is None:
            self.draw_panel()
            return
        self.result = result
        self.quit_loop(0)

    def hit_at(self, ev: MouseEvent) -> Any:
        x0, y0, pw, ph = self.panel_rect
        lx, ly = (ev.pixel_x - x0) / self.scale, (ev.pixel_y - y0) / self.scale
        for rect, action in reversed(self.hits):
            if rect.contains(lx, ly):
                return action
        return None

    def inside_panel(self, ev: MouseEvent) -> bool:
        x0, y0, pw, ph = self.panel_rect
        return x0 <= ev.pixel_x < x0 + pw and y0 <= ev.pixel_y < y0 + ph

    def panel_coords(self, ev: MouseEvent) -> tuple[float, float]:
        x0, y0, pw, ph = self.panel_rect
        return (ev.pixel_x - x0) / self.scale, (ev.pixel_y - y0) / self.scale

    def on_mouse_event(self, ev: MouseEvent) -> None:
        if self.dragging:
            if ev.type is EventType.RELEASE:
                self.dragging = False
                self.view.on_release()
            else:
                self.view.on_drag(*self.panel_coords(ev))
            self.draw_panel()
            return
        if ev.type is EventType.PRESS and ev.buttons & MouseButton.LEFT and self.inside_panel(ev):
            if self.view.on_press(self.hit_at(ev), *self.panel_coords(ev)):
                self.dragging = True
                self.draw_panel()
            return
        if ev.type is EventType.MOVE:
            action = self.hit_at(ev)
            if action != self.hover:
                self.hover = action
                self.draw_panel()
            return
        if ev.type is EventType.RELEASE and ev.buttons & MouseButton.LEFT:
            if not self.inside_panel(ev):
                self.finish({'action': 'cancel'})
                return
            action = self.hit_at(ev)
            if action is not None:
                self.finish(self.view.on_click(action))

    def on_text(self, text: str, in_bracketed_paste: bool = False) -> None:
        self.finish(self.view.on_text(text))

    def on_key(self, key_event: KeyEvent) -> None:
        if key_event.matches('esc'):
            self.finish(None if self.view.on_escape() else {'action': 'cancel'})
            return
        self.hover = None
        self.finish(self.view.on_key(key_event))

    def on_interrupt(self) -> None:
        self.finish({'action': 'cancel'})

    on_eot = on_interrupt


@kitten_ui(allow_remote_control=True)
def main(args: list[str]) -> dict[str, Any]:
    # args: [kitten name, path to a JSON spec: {"view": name, "data": {...}, "background": path to a PNG}]
    with open(args[1]) as f:
        spec = json.load(f)
    try:
        os.remove(args[1])
    except OSError:
        pass
    view = VIEWS[spec['view']](spec.get('data', {}))
    handler = PixelUI(view, spec.get('background', ''), main.remote_control if main.allow_remote_control else None)
    Loop().loop(handler)
    return handler.result


@result_handler()
def handle_result(args: list[str], data: dict[str, Any], target_window_id: int, boss: BossType) -> None:
    pass


if __name__ == '__main__':
    print(main(sys.argv))
