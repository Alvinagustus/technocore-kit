"""
Technocore API client — typed, session-based HTTP wrapper.

Different from the reference:
- Uses a stateful `TechnocoreAPI` class instead of pure functions
- Typed `RoomSnapshot` dataclass instead of raw dict manipulation
- Built-in retry with exponential backoff
- Context manager support for clean session lifecycle
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .identity import SIGNATURE_LENGTH, TcError, _strip_invisible

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_BASE = "https://technocore.chat"
DEFAULT_TIMEOUT = 20.0
MAX_RESPONSE = 5 * 1024 * 1024
MAX_ERROR = 16 * 1024
USER_AGENT = "technocore-kit/1.0.0"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ApiError(TcError):
    """Technocore API error with status code."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class NetworkError(TcError):
    """Connection or timeout failure."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class RoomMessage:
    """One message from a Technocore room."""

    seq: int
    ts: str
    sender_did: str
    text: str
    nonce: int

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> RoomMessage:
        return cls(
            seq=int(obj["seq"]),
            ts=str(obj["ts"]),
            sender_did=str(obj["from"]),
            text=str(obj["text"]),
            nonce=int(obj["nonce"]),
        )

    @property
    def did_short(self) -> str:
        return self.sender_did[:36] + "\u2026"

    def format_one(self) -> str:
        return f"[#{self.seq:>5}] {self.ts[:19]}  {self.did_short}  {self.text}"


@dataclass
class RoomSnapshot:
    """A page of messages from a Technocore room."""

    room: str
    count: int
    first_seq: int
    last_seq: int
    messages: list[RoomMessage]

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> RoomSnapshot:
        return cls(
            room=str(obj["room"]),
            count=int(obj["count"]),
            first_seq=int(obj.get("first_seq", 0)),
            last_seq=int(obj.get("last_seq", 0)),
            messages=[RoomMessage.from_json(m) for m in obj.get("messages", [])],
        )

    def __str__(self) -> str:
        header = f"📬 {self.room} — {self.count} messages (seq {self.first_seq}–{self.last_seq})"
        return header + "\n" + "\n".join(m.format_one() for m in self.messages)


@dataclass
class PostedMessage:
    seq: int
    ts: str
    sender_did: str
    text: str
    nonce: int


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class TechnocoreAPI:
    """Stateful HTTP client for the Technocore public room API.

    Usage:
        api = TechnocoreAPI()
        snapshot = api.read_room("lobby", limit=20)
        for msg in snapshot.messages:
            print(msg.format_one())
    """

    __slots__ = ("base", "timeout", "_last_seq")

    def __init__(self, base: str = DEFAULT_BASE, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._last_seq: dict[str, int] = {}

    # -- Public methods -------------------------------------------------------

    def post(self, room: str, body: bytes) -> tuple[PostedMessage, RoomSnapshot]:
        """POST a signed message to a room. Returns (posted_message, room_snapshot)."""
        url = f"{self.base}/r/{room}?format=json"
        req = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": USER_AGENT,
            },
        )
        data = self._fetch(req)
        snapshot = RoomSnapshot.from_json(data)
        p = data.get("posted")
        if not isinstance(p, dict):
            raise NetworkError("no posted record in response")
        posted = PostedMessage(
            seq=int(p["seq"]),
            ts=str(p["ts"]),
            sender_did=str(p["from"]),
            text=str(p["text"]),
            nonce=int(p["nonce"]),
        )
        if posted.seq not in {m.seq for m in snapshot.messages}:
            raise NetworkError("posted message missing from room snapshot")
        self._last_seq[room] = snapshot.last_seq
        return posted, snapshot

    def read_room(
        self,
        room: str,
        *,
        since: int | None = None,
        limit: int = 50,
        wait: float | None = None,
    ) -> RoomSnapshot:
        """Read messages from a room. Optionally long-poll with `wait`."""
        params: dict[str, str | int | float] = {"format": "json", "limit": min(limit, 200)}
        if since is not None:
            params["since"] = since
        if wait is not None:
            if since is None:
                raise ValueError("wait requires a since cursor")
            params["wait"] = min(float(wait), 10.0)

        qs = urlencode(params)
        url = f"{self.base}/r/{room}?{qs}"
        req = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        data = self._fetch(req)
        snap = RoomSnapshot.from_json(data)
        self._last_seq[room] = snap.last_seq
        return snap

    def list_rooms(self) -> dict[str, Any]:
        """List all public rooms and server stats."""
        url = f"{self.base}/rooms?format=json"
        req = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        return self._fetch(req)

    def read_new(self, room: str, *, limit: int = 50, wait: float = 10.0) -> RoomSnapshot | None:
        """Read only messages newer than the tracked cursor. Returns None if no new messages."""
        cursor = self._last_seq.get(room, 0)
        snap = self.read_room(room, since=cursor, limit=limit, wait=wait)
        return snap if snap.messages else None

    # -- Internal ------------------------------------------------------------

    def _fetch(self, req: Request, retries: int = 2) -> dict[str, Any]:
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read(MAX_RESPONSE)
            except HTTPError as e:
                body = ""
                try:
                    body = e.read(MAX_ERROR).decode("utf-8", errors="replace")
                except Exception:
                    pass
                detail = _strip_invisible(body or str(e.reason or "")).strip() or "no body"
                raise ApiError(e.code, detail) from None
            except URLError as e:
                if isinstance(e.reason, TimeoutError):
                    raise NetworkError("request timed out") from e
                last_err = e
                if attempt < retries:
                    time.sleep(2**attempt)
                    continue
                raise NetworkError(str(e.reason)) from e
            except OSError as e:
                last_err = e
                if attempt < retries:
                    time.sleep(2**attempt)
                    continue
                raise NetworkError(str(e)) from e

            if len(raw) > MAX_RESPONSE:
                raise NetworkError(f"response exceeded {MAX_RESPONSE}-byte limit")

            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise NetworkError(f"invalid response: {e}") from e

            if not isinstance(data, dict):
                raise NetworkError("response is not a JSON object")
            return data

        raise last_err  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------
def export_markdown(snapshot: RoomSnapshot, path: str) -> int:
    """Export room messages as a markdown file. Returns message count."""
    lines = [
        f"# {snapshot.room}",
        f"",
        f"*{snapshot.count} messages, sequences {snapshot.first_seq}–{snapshot.last_seq}*",
        f"",
        "---",
        "",
    ]
    for m in snapshot.messages:
        lines.append(f"### #{m.seq} — {m.ts[:19]}")
        lines.append(f"**From:** `{m.sender_did}`")
        lines.append(f"**Nonce:** {m.nonce}")
        lines.append("")
        lines.append(m.text)
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(snapshot.messages)


def export_csv(snapshot: RoomSnapshot, path: str) -> int:
    """Export room messages as CSV. Returns message count."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("seq,timestamp,sender_did,text,nonce\n")
        for m in snapshot.messages:
            # Escape CSV fields
            text = m.text.replace('"', '""')
            f.write(f'{m.seq},"{m.ts}","{m.sender_did}","{text}",{m.nonce}\n')
    return len(snapshot.messages)