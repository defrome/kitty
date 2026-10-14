#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

# The settings edited by the settings panel. They are kept in settings.json in
# the config directory and written out as settings.conf, which is included at
# the end of kitty.conf, so they override everything else.

import json
import os
import subprocess
from contextlib import suppress
from typing import Any, NamedTuple

from kitty.constants import config_dir

SETTINGS_JSON = 'settings.json'
SETTINGS_CONF = 'settings.conf'
INCLUDE_LINE = f'include {SETTINGS_CONF}'


class Theme(NamedTuple):
    name: str
    title: str
    colors: dict[str, str]  # empty means the colors of the main kitty.conf
    preview: tuple[str, str, str, str]  # background, prompt, accent, text


THEMES = (
    Theme('synthwave', 'Synthwave', {}, ('#0b0c10', '#5af78e', '#ff4fa3', '#2de2e6')),
    Theme('graphite', 'Графит', {
        'foreground': '#e6e6e6', 'background': '#1c1c1e', 'cursor': '#ffffff', 'cursor_text_color': '#1c1c1e',
        'selection_foreground': '#1c1c1e', 'selection_background': '#a5d6a7',
        'color0': '#2c2c2e', 'color8': '#636366', 'color1': '#ff6961', 'color9': '#ff8a80', 'color2': '#a5d6a7', 'color10': '#c8e6c9',
        'color3': '#ffd479', 'color11': '#ffe0a3', 'color4': '#82aaff', 'color12': '#a8c4ff', 'color5': '#d0a8ff', 'color13': '#e0c4ff',
        'color6': '#7fdbca', 'color14': '#a5ece0', 'color7': '#d1d1d6', 'color15': '#ffffff',
        'active_border_color': '#a5d6a7', 'inactive_border_color': '#3a3a3c',
        'active_tab_background': '#e6e6e6', 'active_tab_foreground': '#1c1c1e',
    }, ('#1c1c1e', '#a5d6a7', '#ffffff', '#9aa0a6')),
    Theme('midnight', 'Полночь', {
        'foreground': '#c8d3f5', 'background': '#111a2e', 'cursor': '#8fd3ff', 'cursor_text_color': '#111a2e',
        'selection_foreground': '#111a2e', 'selection_background': '#c5a3ff',
        'color0': '#1b2540', 'color8': '#4b5a80', 'color1': '#ff757f', 'color9': '#ff98a4', 'color2': '#8fd3ff', 'color10': '#b4e3ff',
        'color3': '#ffc777', 'color11': '#ffd8a0', 'color4': '#7aa2f7', 'color12': '#a3bfff', 'color5': '#c5a3ff', 'color13': '#dcc6ff',
        'color6': '#86e1fc', 'color14': '#b2ecfd', 'color7': '#c8d3f5', 'color15': '#ffffff',
        'active_border_color': '#c5a3ff', 'inactive_border_color': '#283456',
        'active_tab_background': '#c5a3ff', 'active_tab_foreground': '#111a2e',
    }, ('#111a2e', '#8fd3ff', '#c5a3ff', '#7aa2f7')),
    Theme('paper', 'Бумага', {
        'foreground': '#2b2b2b', 'background': '#f5f3ee', 'cursor': '#b0306a', 'cursor_text_color': '#f5f3ee',
        'selection_foreground': '#f5f3ee', 'selection_background': '#3a5a8c',
        'color0': '#2b2b2b', 'color8': '#7a7a7a', 'color1': '#b3261e', 'color9': '#d0473f', 'color2': '#1f7a3a', 'color10': '#2e9a4f',
        'color3': '#8a6a00', 'color11': '#a88400', 'color4': '#3a5a8c', 'color12': '#4f74ad', 'color5': '#b0306a', 'color13': '#c94e86',
        'color6': '#1f7a7a', 'color14': '#2c9898', 'color7': '#d8d4ca', 'color15': '#ffffff',
        'active_border_color': '#b0306a', 'inactive_border_color': '#d8d4ca',
        'active_tab_background': '#b0306a', 'active_tab_foreground': '#f5f3ee',
    }, ('#f5f3ee', '#1f7a3a', '#b0306a', '#3a5a8c')),
)
THEME_MAP = {t.name: t for t in THEMES}
SCROLLBACK_CHOICES = (2000, 10000, 50000)
CURSOR_SHAPES = (('block', 'Блок'), ('beam', 'Черта'), ('underline', 'Подчёркивание'))


def settings_path(name: str) -> str:
    return os.path.join(config_dir, name)


def defaults_from_config() -> dict[str, Any]:
    ' The current values of the options the settings panel edits, as configured without the settings panel '
    from kitty.config import load_config
    paths = [p for p in (settings_path('kitty.conf'),) if os.path.exists(p)]
    try:
        opts = load_config(*paths)
    except Exception:
        opts = load_config()
    cell_height = 100
    with suppress(Exception):
        mf = opts.modify_font.get('cell_height')
        if mf is not None and getattr(mf, 'mod_type', None) is not None and str(mf.mod_type).endswith('percent'):
            cell_height = int(mf.val)
    decay = tuple(opts.cursor_trail_decay)
    return {
        'theme': 'synthwave',
        'background_opacity': round(float(opts.background_opacity), 2),
        'background_blur': int(opts.background_blur),
        'font_size': float(opts.font_size),
        'font_family': str(getattr(opts.font_family, 'family', '') or 'monospace'),
        'window_padding_width': float(getattr(opts.window_padding_width, 'left', 0)),
        'cell_height': cell_height,
        'inactive_text_alpha': round(float(opts.inactive_text_alpha), 2),
        'scrollback_lines': int(opts.scrollback_lines),
        'cursor_shape': cursor_shape_name(opts.cursor_shape),
        'cursor_trail': int(opts.cursor_trail),
        'cursor_trail_speed': round(1 - min(1., float(decay[1]) / 1.0), 2) if decay else 0.5,
        'cursor_blink': float(opts.cursor_blink_interval[0]) != 0,
        'smooth_scroll': bool(opts.pixel_scroll) and float(opts.momentum_scroll) > 0,
    }


def load() -> dict[str, Any]:
    ans = defaults_from_config()
    with suppress(OSError, ValueError), open(settings_path(SETTINGS_JSON)) as f:
        saved = json.load(f)
        if isinstance(saved, dict):
            ans.update({k: v for k, v in saved.items() if k in ans})
    return ans


def cursor_shape_name(val: Any) -> str:
    from kitty.fast_data_types import CURSOR_BEAM, CURSOR_BLOCK, CURSOR_UNDERLINE
    if isinstance(val, int):
        return {CURSOR_BLOCK: 'block', CURSOR_BEAM: 'beam', CURSOR_UNDERLINE: 'underline'}.get(val, 'block')
    return str(val)


def as_conf(s: dict[str, Any]) -> str:
    speed = max(0., min(1., float(s['cursor_trail_speed'])))
    fast = 0.02 + (1 - speed) * 0.28
    slow = fast + 0.1 + (1 - speed) * 0.6
    lines = [
        '# Generated by the settings panel of the kitty fork, edits here are overwritten',
        f'background_opacity {s["background_opacity"]:.2f}',
        'dynamic_background_opacity yes',
        f'background_blur {int(s["background_blur"])}',
        f'font_size {float(s["font_size"]):.1f}',
        f'font_family family="{s["font_family"]}"',
        f'window_padding_width {float(s["window_padding_width"]):.0f}',
        f'modify_font cell_height {int(s["cell_height"])}%',
        f'inactive_text_alpha {float(s["inactive_text_alpha"]):.2f}',
        f'scrollback_lines {int(s["scrollback_lines"])}',
        f'cursor_shape {cursor_shape_name(s["cursor_shape"])}',
        f'cursor_trail {int(s["cursor_trail"])}',
        f'cursor_trail_decay {fast:.2f} {slow:.2f}',
        f'cursor_blink_interval {"-1" if s["cursor_blink"] else "0"}',
        f'pixel_scroll {"yes" if s["smooth_scroll"] else "no"}',
        f'momentum_scroll {"0.96" if s["smooth_scroll"] else "0"}',
    ]
    if theme := THEME_MAP.get(s['theme']):
        lines.extend(f'{k} {v}' for k, v in theme.colors.items())
    return '\n'.join(lines) + '\n'


def ensure_included() -> None:
    ' Make kitty.conf include settings.conf, at its end so that the settings override everything else '
    path = settings_path('kitty.conf')
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = ''
    if INCLUDE_LINE not in text.splitlines():
        with open(path, 'a') as f:
            f.write(('' if not text or text.endswith('\n') else '\n') + f'\n# Settings changed from the settings panel\n{INCLUDE_LINE}\n')


def save(s: dict[str, Any]) -> None:
    from kitty.config import atomic_save
    atomic_save(json.dumps(s, indent=2, ensure_ascii=False).encode(), settings_path(SETTINGS_JSON))
    atomic_save(as_conf(s).encode(), settings_path(SETTINGS_CONF))
    ensure_included()


def monospace_families() -> list[str]:
    try:
        out = subprocess.run(['fc-list', ':spacing=mono', 'family'], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    names = set()
    for line in out.splitlines():
        name = line.split(',')[0].strip()
        if name and not any(x in name for x in ('Symbols', 'Emoji', 'Nerd Font Propo')):
            names.add(name)
    return sorted(names, key=str.lower)
