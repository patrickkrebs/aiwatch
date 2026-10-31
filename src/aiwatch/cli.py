# Copyright 2026 Patrick Krebs
# SPDX-License-Identifier: Apache-2.0 AND MIT
# Parts marked "Adapted from wfwatch" are Copyright (c) 2026 Zamir Musaev, MIT (licenses/wfwatch-LICENSE.txt).

"""aiwatch – wfwatch with Codex / ChatGPT sessions added: same screens, same keys.

Codex threads are wfwatch sessions (see codex_source), so wfwatch draws them itself; this module
only merges them into its app: numbered after the Claude sessions, in the tabs, the log and the keys."""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
import time

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

from wfwatch import model
from wfwatch.cli import FRAME_SEC, IDLE_FRAME_SEC, POLL_SEC, SCAN_SEC, App
from wfwatch.i18n import plural
from wfwatch.i18n import t as tr
from wfwatch.render import Overview, tabs_line
from wfwatch.theme import C, bold, status_count
from wfwatch.widgets import SESSION_COUNTS, Fit, fmt_dur, session_icon

from . import __version__
from .codex_source import PREFIX, CodexTracker


def is_codex(s) -> bool:
    return s.session_id.startswith(PREFIX)


class AiOverview(Overview):
    """wfwatch's overview; the header counts Claude and Codex sessions apart."""

    def __init__(self, runs, sessions, colors, codex_live: bool):
        super().__init__(runs, sessions, colors)
        self.codex_live = codex_live

    # Adapted from wfwatch v0.1.1 render.Overview.header (MIT).
    def header(self) -> RenderableType:
        running = sum(r.state == "running" for r in self.runs)
        finished = len(self.runs) - running
        colors = dict(zip(SESSION_COUNTS, (bold(C.blue), bold(C.yellow), C.cyan, C.dim), strict=True))
        groups = [
            (tr("overview.sessions"), [s for s in self.sessions if not is_codex(s)]),
            ("codex:", [s for s in self.sessions if is_codex(s)]),
        ]

        def counts_line(words: bool, with_finished: bool) -> Text:
            t = Text(no_wrap=True, overflow="ellipsis", justify="right")
            t.append(tr("overview.runs", running=status_count(model.RUNNING, running)), style=bold(C.orange))
            if finished and with_finished:
                t.append(" · " + plural("count.finished", finished), style=C.dim)
            for title, group in groups:
                by_state = {k: sum(s.state == k for s in group) for k in SESSION_COUNTS}
                t.append("   " + title + " ", style=C.text)
                if not group:
                    t.append("live" if self.codex_live else "–", style=C.green if self.codex_live else C.dim)
                first = True
                for k in SESSION_COUNTS:
                    if by_state[k]:
                        if not first:
                            t.append(" · ", style=C.dim)
                        n = by_state[k]
                        t.append(plural(f"count.{k}", n) if words else f"{session_icon(k)} {n}", style=colors[k])
                        first = False
            return t

        g = Table.grid(expand=True, padding=(0, 2))
        g.add_column(no_wrap=True)
        g.add_column(ratio=1, justify="right", no_wrap=True, overflow="ellipsis")
        right = Fit([counts_line(True, True), counts_line(True, False), counts_line(False, True), counts_line(False, False)])
        g.add_row(Text.assemble(("AIWATCH  ", bold(C.orange)), (tr("overview.title"), "bold")), right)
        keep, idle = model.Tracker.KEEP_SEC // 60, model.IDLE_KEEP_SEC // 60
        note = Text(tr("overview.note", keep=keep, idle=idle) + " · Claude + Codex", style=C.dim, no_wrap=True, overflow="ellipsis")
        return Group(g, note)


class AiApp(App):
    """wfwatch's app over Claude sessions and Codex threads together."""

    def __init__(self, tracker: model.Tracker, codex: CodexTracker, view: str = "graph"):
        super().__init__(tracker, False, view=view)
        self.codex = codex

    def sessions(self) -> list:
        return self.tracker.active_sessions() + self.codex.visible()

    def watch(self, sid):
        if not isinstance(sid, str):
            return None
        return self.tracker.watches.get(sid) or self.codex.watches.get(sid)

    def items(self):
        return [r.run_dir for r in self.tracker.visible()] + [s.session_id for s in self.sessions()]

    def current_session(self):
        if self.mode == "detail" and (w := self.watch(self.selected)) is not None:
            return self.session_painter(w)
        return None

    # Adapted from wfwatch v0.1.1 cli.App.prune (MIT).
    def prune(self) -> None:
        for k in [k for k in self.painters if k not in self.tracker.runs]:
            del self.painters[k]
        for k in [k for k in self.session_painters if self.watch(k) is None]:
            del self.session_painters[k]

    def animating(self) -> bool:
        if self.mode == "detail" and (w := self.watch(self.selected)) is not None:
            return w.animating()
        if self.mode == "overview" and any(w.animating() for w in self.codex.visible()):
            return True
        return super().animating()

    def status_error(self) -> str:
        return super().status_error() or self.codex.error

    # Adapted from wfwatch v0.1.1 cli.App._view (MIT).
    def _view(self, snapshot: bool, height: int = 0) -> RenderableType:
        self.prune()
        runs, sessions = self.tracker.visible(), self.sessions()
        for r in runs:
            self.painter(r)
        multi = len(runs) + len(sessions) > 1
        tabs = tabs_line(runs, self.selected, sessions) if multi else None
        animate = not self.still
        if self.mode == "detail":
            sel = self.selected
            if sel in self.tracker.runs:
                p = self.painter(self.tracker.runs[sel])
                return p.snapshot(tabs=tabs) if snapshot else p.screen(height, tabs, self.keys(multi), animate=animate)
            if (w := self.watch(sel)) is not None:
                sp = self.session_painter(w)
                return sp.snapshot(tabs=tabs) if snapshot else sp.screen(height, tabs, self.keys(multi), animate=animate)
            self.mode, self.selected = "overview", None
        ov = AiOverview(runs, sessions, self.colors, self.codex.app_running)
        ov.animate = animate
        return ov.snapshot() if snapshot else ov.screen(height)


# Adapted from wfwatch v0.1.1 cli._Terminated and cli.run_live (MIT).
class _Terminated(BaseException):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def run_live(app: AiApp, console: Console) -> int:
    """wfwatch's main loop, also re-reading Codex."""
    from wfwatch.keys import Keys

    tracker, codex = app.tracker, app.codex

    def on_signal(signum, frame):
        raise _Terminated(signum)

    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(ValueError, OSError):
                previous[sig] = signal.signal(sig, on_signal)
    last_poll = last_scan = time.monotonic()
    try:
        with Keys() as keys, Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                t = time.monotonic()
                if t - last_scan >= SCAN_SEC:
                    last_scan = t
                    app.safe("scan", lambda: app.on_new(tracker.scan()))
                    app.safe("codex", codex.scan)
                if t - last_poll >= POLL_SEC:
                    last_poll = t
                    app.safe("poll", tracker.poll)
                    app.safe("codex", codex.poll)
                live.update(app.frame(console.size.height), refresh=True)
                for k in keys.poll(FRAME_SEC if app.animating() else IDLE_FRAME_SEC):
                    if not app.on_key(k):
                        return 0
    except KeyboardInterrupt:
        return 0
    except _Terminated as e:
        return 128 + e.signum
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_list(console: Console) -> None:
    tracker, codex = model.Tracker(), CodexTracker()
    tracker.scan()
    codex.scan()
    at = model.now()
    t = Table(box=None, header_style="bold", pad_edge=False)
    for col in ("Source", "Name", "Project", "State", "Since", "Subagents"):
        t.add_column(col, no_wrap=col != "Name")
    for src, group in (("claude", tracker.active_sessions()), ("codex", codex.visible())):
        for w in group:
            since = fmt_dur((at - w.since).total_seconds()) if w.since else ""
            subs = f"{len(w.running_subagents())}/{len(w.subagents)}" if w.subagents else ""
            t.add_row(
                Text(src, style="cyan" if src == "claude" else "green"),
                Text(w.name, overflow="ellipsis"),
                Text(w.project),
                Text(w.state),
                since,
                subs,
            )
    console.print(t)


def main() -> int:
    p = argparse.ArgumentParser(prog="aiwatch", description="wfwatch with Codex / ChatGPT sessions added.")
    p.add_argument("-V", "--version", action="version", version=f"aiwatch {__version__}")
    p.add_argument("-l", "--list", action="store_true", help="list live sessions and exit")
    p.add_argument("-c", "--cards", action="store_true", help="cards instead of diagrams")
    p.add_argument("--once", action="store_true", help="print one frame and exit")
    p.add_argument("--open", metavar="N", type=int, help="with --once: print item N's detail view")
    p.add_argument("--width", type=int, help="render at this width")
    p.add_argument("--debug", action="store_true", help="raise errors instead of showing them")
    args = p.parse_args()

    console = Console(width=args.width) if args.width else Console()
    model.DEBUG = args.debug
    if args.list:
        run_list(console)
        return 0
    if not args.once and not (console.is_terminal and sys.stdout.isatty()):
        print("aiwatch: needs a terminal (or use --once / --list)", file=sys.stderr)
        return 2

    tracker, codex = model.Tracker(), CodexTracker()
    app = AiApp(tracker, codex, view="cards" if args.cards else "graph")
    app.safe("scan", tracker.scan)
    app.safe("codex", codex.scan)
    if args.once:
        items = app.items()
        if args.open and 0 < args.open <= len(items):
            app.open(items[args.open - 1])
        console.print(app.snapshot())
        return 1 if app.frame_error else 0
    try:
        return run_live(app, console)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
