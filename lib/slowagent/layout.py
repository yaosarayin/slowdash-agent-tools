# lib/slowagent/layout.py — slowplot regeneration helper.
#
# The slowtask owns the auto-generated `slowplot-NAME.json` file: it lists
# the channels the user has marked `connected: true` in `slowagent-NAME.json`.
# Both the slowtask (after a config reload) and the sd_agent server module
# (right after the user toggles a checkbox) need to write this file, so the
# logic lives here and is called from both places.

import os
import json
import logging


def regenerate_slowplot(layout_path: str) -> bool:
    """Read `slowagent-NAME.json` at `layout_path` and rewrite the matching
    `slowplot-NAME.json` to list exactly the channels marked `connected:
    true` (default true if the field is absent).

    Returns True if the file was written (channel set changed), False if
    the existing file already matches and was left untouched.

    Raises nothing — failures are logged via `logging` and surface as
    `False`.
    """
    try:
        with open(layout_path) as f:
            config = json.load(f)
    except (OSError, ValueError) as e:
        logging.warning("slowagent.layout: cannot read %s: %s", layout_path, e)
        return False

    layout_name = _layout_basename(layout_path)
    slowplot_path = os.path.join(os.path.dirname(layout_path),
                                 f'slowplot-{layout_name}.json')

    declared      = config.get('channels', [])
    visible       = [c for c in declared if c.get('connected', True)]
    visible_names = [c['name'] for c in visible]

    if not visible:
        # Don't write an empty plot — leaves the existing one in place so
        # slowplot rendering doesn't break on zero panels.
        return False

    # Skip if the existing file already matches.
    try:
        with open(slowplot_path) as f:
            existing = json.load(f)
        existing_names = [p.get('channel') for p in
                          (existing.get('panels') or [{}])[0].get('plots', [])]
        if existing_names == visible_names:
            return False
    except (OSError, ValueError, KeyError):
        pass

    plot_cfg = config.get('plot', {})
    plots = []
    for c in visible:
        plots.append({
            "type":          "timeseries",
            "channel":       c['name'],
            "color":         c.get('color', '#666'),
            "label":         c.get('label', c['name']),
            "format":        "%.1f",
            "opacity":       1,
            "marker_type":   "circle",
            "marker_size":   3,
            "line_width":    1,
            "line_type":     "connect",
            "fill_opacity":  0,
            "fill_envelope": False,
            "fill_baseline": 1e-100,
        })

    title = config.get('meta', {}).get('title', layout_name)
    doc = {
        "control": {
            "range":  {"length": int(plot_cfg.get('length', 86400)), "to": 0},
            "reload": int(plot_cfg.get('reload', 5)),
            "mode":   "normal",
            "grid":   {"rows": 1, "columns": 1},
        },
        "style":  {},
        "panels": [{
            "type":   "timeseries",
            "plots":  plots,
            "axes":   {
                "xfixed": False, "yfixed": False,
                "xlog":   False, "ylog":   False, "zlog": False,
                "ymin":   0, "ymax": 200,
                "title":  "Zone Temperatures (°C)",
                "ytitle": "Temperature (°C)",
            },
            "legend": {"style": "transparent", "position": "left"},
        }],
        "meta": {
            "name":        layout_name,
            "title":       f"{title} — Live Plot",
            "description": "Auto-generated from `connected` channels in the layout.",
        },
    }

    try:
        with open(slowplot_path, 'w') as f:
            json.dump(doc, f, indent=2)
        logging.info("slowagent.layout: rewrote %s with %d channel(s): %s",
                     os.path.basename(slowplot_path), len(visible),
                     ', '.join(visible_names))
        return True
    except OSError as e:
        logging.warning("slowagent.layout: cannot write %s: %s", slowplot_path, e)
        return False


def _layout_basename(layout_path: str) -> str:
    """slowagent-Omega.json → Omega"""
    name = os.path.splitext(os.path.basename(layout_path))[0]
    if name.startswith('slowagent-'):
        return name[len('slowagent-'):]
    return name
