"""Claude, through the Claude Code CLI (`claude -p`), on the claude.ai subscription: no API key.

The answer streams in (`--output-format stream-json`), so Hektor can start saying the first sentence
while Claude is still writing the rest.
"""
import json
import logging
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

from .config import Conf

log = logging.getLogger(__name__)

# Claude may search the web and read (never change) mail, calendar, drive and docs.
# Everything else is refused automatically: there's no one to approve a prompt.
ALLOWED_TOOLS = [
    "WebSearch", "WebFetch", "ToolSearch",
    "mcp__claude_ai_Gmail__search_threads", "mcp__claude_ai_Gmail__get_thread", "mcp__claude_ai_Gmail__get_message",
    "mcp__claude_ai_Gmail__list_labels", "mcp__claude_ai_Gmail__list_drafts", "mcp__claude_ai_Gmail__get_draft",
    "mcp__claude_ai_Google_Calendar__list_calendars", "mcp__claude_ai_Google_Calendar__list_events",
    "mcp__claude_ai_Google_Calendar__search_events", "mcp__claude_ai_Google_Calendar__get_event",
    "mcp__claude_ai_Google_Calendar__suggest_time",
    "mcp__claude_ai_Google_Drive__search_files", "mcp__claude_ai_Google_Drive__list_recent_files",
    "mcp__claude_ai_Google_Drive__read_file_content", "mcp__claude_ai_Google_Drive__get_file_metadata",
    "mcp__claude_ai_Claude_Docs__query", "mcp__claude_ai_Claude_Docs__read",
]
MOOD = re.compile(r"^\s*\[(\w+)\]\s*")
TOOL = "\0tool"  # what ask() yields as the mood when Claude starts using a tool, the tool's name as the text


class Brain:
    def __init__(self, conf: Conf) -> None:
        self.conf = conf
        self.session: str | None = None
        self.last = 0.0
        self.reply = ""   # the whole of the last answer, once it's done

    def forget(self) -> None:
        self.session = None

    def _cmd(self, text: str, *args: str) -> list[str]:
        model = self.conf.get("MODEL")
        return ["claude", "-p", text, *args, *(["--model", model] if model else []),
                "--tools", "WebSearch,WebFetch,ToolSearch", "--allowedTools", *ALLOWED_TOOLS,
                "--append-system-prompt", self.conf["PROMPT"]]

    def warm_up(self) -> None:
        """Ask Claude something tiny in the background: the first `claude -p` after a boot is slow (the
        CLI and the connectors load from a cold disk), better now than on the first question."""
        def run() -> None:
            t = time.monotonic()
            done = subprocess.run(self._cmd("Reply with just: ok", "--no-session-persistence"), cwd=Path.home(),
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if done.returncode:
                log.warning("claude warm-up failed (%s): %s", done.returncode, done.stderr.strip()[-300:])
            else:
                log.info("claude warmed up in %.1f s", time.monotonic() - t)
        threading.Thread(target=run, name="claude-warm-up", daemon=True).start()

    def ask(self, text: str, start: Callable[..., subprocess.Popen | None]) -> Iterator[tuple[str | None, str]]:
        """Claude's answer to what the user said, as it comes: (mood, text) pieces, the mood when a
        part of the answer starts with one (else None); (TOOL, name) when it starts using a tool. `start` runs the command (None: cancelled);
        when the answer is done, `reply` has all of it."""
        if self.session and time.monotonic() - self.last > self.conf.num("FORGET_AFTER"):
            log.info("quiet for a while, starting a new conversation")
            self.session = None
        new = not self.session
        if new:
            self.session = str(uuid.uuid4())
        log.info("asking claude (%s conversation %s)", "new" if new else "resuming", self.session[:8])
        self.reply = ""
        said = False
        with tempfile.TemporaryFile("w+") as err:
            proc = start(self._cmd(text, "--session-id" if new else "--resume", self.session,
                                   "--output-format", "stream-json", "--verbose", "--include-partial-messages"),
                         cwd=Path.home(), stderr=err)
            if proc is None:
                return
            head = None  # the start of a part of the answer, until we know whether it has a mood
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("parent_tool_use_id"):  # a subagent's, not the answer
                    continue
                if msg.get("type") == "result":
                    self.reply = (msg.get("result") or "").strip()
                    if msg.get("is_error"):
                        log.error("claude: %s", self.reply[:500])
                        self.reply = ""
                if msg.get("type") != "stream_event":
                    continue
                ev = msg["event"]
                block = ev.get("content_block", {}) if ev.get("type") == "content_block_start" else {}
                if block.get("type") == "text":
                    head = ""
                elif block.get("type") == "tool_use":
                    yield TOOL, block.get("name", "")
                elif ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    piece = ev["delta"]["text"]
                    if head is None:
                        said = True
                        yield None, piece
                        continue
                    head += piece
                    mood = MOOD.match(head)
                    if mood:
                        said = True
                        yield mood.group(1).lower(), head[mood.end():]
                        head = None
                    elif len(head.lstrip()) > 20 or (head.lstrip() and not head.lstrip().startswith("[")):
                        said = True
                        yield None, head
                        head = None
            rc = proc.wait()
            self.last = time.monotonic()
            if rc and rc > 0:
                err.seek(0)
                log.error("claude failed (%s): %s", rc, err.read().strip()[-500:])
        if not said and rc:
            if new:  # the session may not exist, don't try to resume it next time
                self.session = None
            return
        if not said and self.reply:  # nothing streamed (shouldn't happen): the whole answer at once
            mood = MOOD.match(self.reply)
            yield (mood.group(1).lower(), self.reply[mood.end():]) if mood else (None, self.reply)
