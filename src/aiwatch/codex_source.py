# Copyright 2026 Patrick Krebs
# SPDX-License-Identifier: Apache-2.0

"""Codex threads as wfwatch sessions.

The thread list comes from ~/.codex/state_5.sqlite; what a thread does comes from its rollout log
(~/.codex/sessions/**/rollout-*.jsonl). A thread spawned by another (agent mode) becomes a subagent
of its parent, as Claude Code subagents are, so wfwatch's own cards and views draw both alike."""

from __future__ import annotations

import bisect
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from wfwatch.model import (
    BUSY,
    DONE,
    FAILED,
    IDLE,
    IDLE_KEEP_SEC,
    RUNNING,
    Action,
    Agent,
    Event,
    SessionInfo,
    SessionWatch,
    now,
)

CODEX_DIR = Path(os.environ.get("CODEX_CONFIG_DIR") or Path.home() / ".codex")
STATE_DB = CODEX_DIR / "state_5.sqlite"
IPC_SOCK = CODEX_DIR / "ipc" / "ipc.sock"

PREFIX = "codex:"  # session ids of Codex threads, so they never collide with Claude's
PLACE = "codex"
TAIL_BYTES = 1_000_000  # a main rollout can reach hundreds of MB: only its end is read at first
CHILD_FULL_MAX = 8_000_000  # a subagent's rollout is read whole (to skip its copied history) up to this
FRESH_SEC = 90  # a turn boundary not seen yet: busy while the log changes this recently (as wfwatch)


def short_model(m: str | None) -> str:
    if not m:
        return ""
    return m.removesuffix("-sol").replace("gpt-", "gpt ", 1)


def _ts(s) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ms(v) -> datetime | None:
    return datetime.fromtimestamp(v / 1000, tz=timezone.utc) if v else None


def first_line(s, n: int = 120) -> str:
    for line in (s or "").splitlines() if isinstance(s, str) else ():
        if line.strip():
            return line.strip()[:n]
    return ""


def _call_action(p: dict) -> Action:
    kind = p.get("type")
    name = p.get("name") or kind or "?"
    if kind == "custom_tool_call":
        return Action(name, first_line(p.get("input"), 80))
    if kind == "local_shell_call":
        cmd = (p.get("action") or {}).get("command") or []
        return Action("shell", first_line(" ".join(cmd) if isinstance(cmd, list) else str(cmd), 80))
    raw = p.get("arguments") or ""
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        args = {}
    if isinstance(args, dict):
        for key in ("task_name", "target", "title", "description", "command", "cmd", "path", "query", "url"):
            v = args.get(key)
            if v:
                return Action(name, first_line(" ".join(v) if isinstance(v, list) else str(v), 80))
        if not args:
            return Action(name, "")
    return Action(name, first_line(str(raw), 60))


def _message_text(p: dict) -> str:
    return first_line("\n".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict)))


class Feed:
    """Follows one rollout log and keeps an Agent up to date: calls, tokens, last action, turns."""

    def __init__(self, path: Path, agent: Agent, child: bool):
        self.path = path
        self.agent = agent
        self.child = child
        self.offset: int | None = None
        self.in_fork = child  # a subagent's log opens with a copy of its parent's history
        self.fork_ts: datetime | None = None
        self.busy: bool | None = None  # None: no turn boundary seen yet
        self.ended_ok = True
        self.since: datetime | None = None  # when the last turn started or ended
        self.reply = ""

    def read(self) -> list[Event]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        skip_partial = False
        if self.offset is None or size < self.offset:
            limit = CHILD_FULL_MAX if self.child else TAIL_BYTES
            self.offset = max(0, size - limit)
            skip_partial = self.offset > 0
            self.in_fork = self.in_fork and not skip_partial
        if size <= self.offset:
            return []
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                data = f.read(size - self.offset)
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        chunk = data[: end + 1]
        self.offset += len(chunk)
        lines = chunk.split(b"\n")
        if skip_partial:
            lines = lines[1:]
        events: list[Event] = []
        for raw in lines:
            if raw:
                self._line(raw, events)
        return events

    def _line(self, raw: bytes, events: list[Event]) -> None:
        try:
            r = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(r, dict):
            return
        ts = _ts(r.get("timestamp"))
        if ts is None:
            return
        p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        kind, sub = r.get("type"), p.get("type")
        if self.in_fork:
            if self.fork_ts is None:
                self.fork_ts = ts
            own = sub == "task_started" and (p.get("turn_attribution") or {}).get("parent_turn_id")
            if not own and (ts - self.fork_ts).total_seconds() < 1:
                return
            self.in_fork = False
        a, who = self.agent, self.agent.label
        if a.last_ts is None or ts > a.last_ts:
            a.last_ts = ts
        if kind == "event_msg":
            if sub == "task_started":
                self.busy, self.ended_ok, self.since = True, True, ts
                events.append(Event(ts, who, "start"))
            elif sub == "task_complete":
                self.busy, self.ended_ok, self.since = False, True, ts
                self.reply = first_line(p.get("last_agent_message")) or self.reply
                a.result = self.reply
                events.append(Event(ts, who, "done", detail=self.reply))
            elif sub == "turn_aborted":
                self.busy, self.ended_ok, self.since = False, False, ts
                events.append(Event(ts, who, "failed"))
            elif sub == "token_count":
                info = p.get("info") or {}
                last = info.get("last_token_usage") or {}
                a.context_tokens = int(last.get("input_tokens") or 0)
                a.output_by_msg[str(len(a.output_by_msg))] = int(last.get("output_tokens") or 0)
        elif kind == "response_item":
            if sub in ("custom_tool_call", "function_call", "local_shell_call"):
                a.last_action = _call_action(p)
                a.tool_calls += 1
                a.tool_ts.append(ts)
                events.append(Event(ts, who, "tool", action=a.last_action))
            elif sub == "message" and p.get("role") == "assistant":
                text = _message_text(p)
                if text:
                    a.last_action = Action("", text)
                    self.reply = text

    def working(self) -> bool:
        if self.busy is None:
            last = self.agent.last_ts
            return last is not None and (now() - last).total_seconds() < FRESH_SEC
        return self.busy


class _NoLog:
    """Codex has no Claude-style background commands."""

    bg: dict = {}

    def poll(self) -> None:
        pass


def _project(cwd: str) -> str:
    m = re.search(r"-Documents-GitHub-([^/]+)", cwd)
    if m:
        return m.group(1)
    return Path(cwd).name or cwd or "?"


def _thread_name(row) -> str:
    name = (row["name"] or "").strip()
    if name:
        return name
    title = (row["title"] or "").strip()
    if title.startswith("You are ChatGPT, collaborating"):
        parts = title.split("\n\n", 1)
        if len(parts) > 1:
            return first_line(parts[1], 80)
    return first_line(title, 80) or row["id"][:8]


def _spawn(source: str) -> dict | None:
    """The thread_spawn record of a thread started by another, else None."""
    if not source or not source.startswith("{"):
        return None
    try:
        d = json.loads(source)
    except json.JSONDecodeError:
        return None
    spawn = (d.get("subagent") or {}).get("thread_spawn") if isinstance(d, dict) else None
    return spawn if isinstance(spawn, dict) and spawn.get("parent_thread_id") else None


class CodexWatch(SessionWatch):
    """A Codex thread drawn as a wfwatch session; the threads it spawned are its subagents."""

    def __init__(self, row):
        self.thread_id = row["id"]
        self.created = _ms(row["created_at_ms"])
        info = SessionInfo(
            session_id=PREFIX + self.thread_id, name=_thread_name(row), status=IDLE, cwd=row["cwd"] or "",
            entrypoint=PLACE, started_at=self.created,
        )
        path = Path(row["rollout_path"] or "")
        super().__init__(info.session_id, path, info, log=_NoLog())
        self.main.started = self.created
        self.feed = Feed(path, self.main, child=False)
        self.child_feeds: dict[str, Feed] = {}
        self.tokens_used = 0
        self.update(row)

    def update(self, row) -> None:
        self.info.name = _thread_name(row)
        self.main.label = self.info.name
        self.main.model = short_model(row["model"])
        self.tokens_used = row["tokens_used"] or 0
        self.updated = _ms(row["updated_at_ms"])

    def add_child(self, row, spawn: dict) -> None:
        cid = row["id"]
        feed = self.child_feeds.get(cid)
        if feed is None:
            nick = row["agent_nickname"] or spawn.get("agent_nickname") or ""
            task = Path(row["agent_path"] or spawn.get("agent_path") or "").name
            label = " · ".join(x for x in (nick, task) if x) or cid[:8]
            a = Agent(cid, label, "", None, started=_ms(row["created_at_ms"]))
            feed = self.child_feeds[cid] = Feed(Path(row["rollout_path"] or ""), a, child=True)
            self.subagents[Path(PREFIX + cid)] = a
        feed.agent.model = short_model(row["model"])

    @property
    def project(self) -> str:
        return _project(self.info.cwd)

    def poll(self, info: SessionInfo | None = None) -> None:
        for e in self.feed.read():
            self._add(e)
        self.info.status = BUSY if self.feed.working() else IDLE
        self.info.since = self.feed.since or self.main.last_ts or self.updated
        self.main.status = RUNNING if self.info.status == BUSY else DONE
        for feed in self.child_feeds.values():
            for e in feed.read():
                self._add(e)
            a = feed.agent
            a.status = RUNNING if feed.working() else DONE if feed.ended_ok else FAILED

    def _add(self, e: Event) -> None:
        bisect.insort(self.events, e, key=lambda x: x.ts)
        if len(self.events) > 300:
            del self.events[:100]

    def last_activity(self) -> datetime | None:
        ts = [t for t in (self.main.last_ts, self.updated, *(a.last_ts for a in self.subagents.values())) if t]
        return max(ts) if ts else None

    def shown(self, at: datetime | None = None) -> bool:
        at = at or now()
        if self.info.status == BUSY or self.running_subagents():
            return True
        last = self.last_activity()
        return last is not None and (at - last).total_seconds() < IDLE_KEEP_SEC


class CodexTracker:
    """Codex threads of the last few hours; spawned ones are folded into their parents."""

    RECENT_SEC = 3 * 3600

    def __init__(self):
        self.watches: dict[str, CodexWatch] = {}  # by session id (PREFIX + thread id)
        self.app_running = False
        self.error = ""

    def _rows(self, con, where: str, args: tuple) -> list:
        return con.execute(
            f"""SELECT id, title, name, model, tokens_used, updated_at_ms, created_at_ms, cwd, source,
                       rollout_path, agent_nickname, agent_path
                FROM threads WHERE archived = 0 AND {where}""",
            args,
        ).fetchall()

    def scan(self) -> None:
        self.app_running = IPC_SOCK.exists()
        if not STATE_DB.exists():
            self.error = f"Codex state DB not found: {STATE_DB}"
            return
        cutoff = int((now().timestamp() - self.RECENT_SEC) * 1000)
        try:
            con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=2.0)
            con.row_factory = sqlite3.Row
            try:
                rows = self._rows(con, "updated_at_ms >= ? ORDER BY created_at_ms LIMIT 500", (cutoff,))
                ids = {r["id"] for r in rows}
                parents = {s["parent_thread_id"] for r in rows if (s := _spawn(r["source"]))} - ids
                if parents:
                    marks = ",".join("?" * len(parents))
                    rows = self._rows(con, f"id IN ({marks})", tuple(parents)) + rows
            finally:
                con.close()
        except sqlite3.Error as e:
            self.error = f"Codex: {e}"
            return
        self.error = ""
        seen, children = set(), []
        for row in rows:
            spawn = _spawn(row["source"])
            if spawn:
                children.append((row, spawn))
                continue
            sid = PREFIX + row["id"]
            seen.add(sid)
            w = self.watches.get(sid)
            if w is None:
                self.watches[sid] = CodexWatch(row)
            else:
                w.update(row)
        for row, spawn in children:
            parent = self.watches.get(PREFIX + spawn["parent_thread_id"])
            if parent is not None:
                parent.add_child(row, spawn)
        for sid in [k for k in self.watches if k not in seen]:
            del self.watches[sid]
        self.poll()

    def poll(self) -> None:
        for w in self.watches.values():
            w.poll()

    def visible(self) -> list[CodexWatch]:
        at = now()
        return [w for w in self.watches.values() if w.shown(at)]
