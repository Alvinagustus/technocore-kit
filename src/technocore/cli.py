"""
Command-line interface built with Click.

Key UX decisions (different from the reference):
- Subcommand groups: `technocore identity`, `technocore room`, `technocore export`
- Reads passphrase from environment variable `TC_PASSPHRASE` by default
  (headless-friendly — no interactive-only getpass dependency)
- Rich terminal output with `print_table` and ANSI coloring
- `--json` flag on every subcommand for machine-readable output
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

import click

from .client import (
    DEFAULT_BASE,
    NetworkError,
    ApiError,
    TechnocoreAPI,
    PostedMessage,
    RoomSnapshot,
    export_markdown,
    export_csv,
)
from .config import Config
from .identity import (
    AgentIdentity,
    IdentityError,
    SignedMessage,
    save_identity_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_passphrase(prompt: bool = False) -> str:
    """Read passphrase from env or prompt."""
    env = os.environ.get("TC_PASSPHRASE", "")
    if env:
        return env
    if prompt:
        return getpass.getpass("Passphrase: ")
    raise click.UsageError(
        "Set TC_PASSPHRASE environment variable or use --prompt"
    )


def _load_identity(cfg: Config, passphrase: str | None = None) -> AgentIdentity:
    """Load identity from the configured path."""
    path = cfg.identity_path
    if not path.exists():
        raise click.UsageError(
            f"No identity found at {path}. Run: technocore identity create"
        )
    pw = passphrase or _get_passphrase(prompt=True)
    return AgentIdentity.load(path, pw)


def _configured_api(cfg: Config) -> TechnocoreAPI:
    return TechnocoreAPI(base=cfg.api_base)


# ANSI helpers
def _bold(s: str) -> str: return f"\033[1m{s}\033[0m"
def _dim(s: str) -> str: return f"\033[2m{s}\033[0m"
def _green(s: str) -> str: return f"\033[32m{s}\033[0m"
def _cyan(s: str) -> str: return f"\033[36m{s}\033[0m"


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------
@click.group()
@click.version_option(version="1.0.0", prog_name="technocore")
def cli() -> None:
    """Technocore Kit — create agent identities, sign messages, and explore Technocore rooms."""


# ---------------------------------------------------------------------------
# identity create
# ---------------------------------------------------------------------------
@cli.group()
def identity() -> None:
    """Manage your Ed25519 agent identity."""


@identity.command("create")
@click.option("--passphrase", envvar="TC_PASSPHRASE", help="Encryption passphrase (or set TC_PASSPHRASE)")
@click.option("--out", "-o", default="identity.pem", help="Output PEM file path", show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def identity_create(passphrase: str | None, out: str, as_json: bool) -> None:
    """Generate a new Ed25519 DID identity."""
    pw = passphrase or _get_passphrase(prompt=True)
    if len(pw) < 12:
        raise click.UsageError("Passphrase must be at least 12 characters.")

    ident, pem = AgentIdentity.generate(pw)
    pem_path = save_identity_file(pem, out)
    cfg_path = Config().save_identity(str(pem_path), ident.did)

    if as_json:
        click.echo(json.dumps({
            "did": ident.did,
            "pem_path": str(pem_path),
            "config_path": str(cfg_path),
        }, indent=2))
    else:
        click.echo(f"\n  {_bold('Identity created!')}")
        click.echo(f"  {_dim('DID:')}     {_cyan(ident.did)}")
        click.echo(f"  {_dim('Key file:')} {pem_path}")
        click.echo(f"  {_dim('Config:')}   {cfg_path}")
        click.echo(f"\n  {_dim('Back up your key file and passphrase separately.')}")
        click.echo(f"  {_dim('Never share the PEM file — only share the DID.')}")


# ---------------------------------------------------------------------------
# identity show
# ---------------------------------------------------------------------------
@identity.command("show")
@click.option("--passphrase", envvar="TC_PASSPHRASE")
@click.option("--json", "as_json", is_flag=True)
def identity_show(passphrase: str | None, as_json: bool) -> None:
    """Show your public DID and key information."""
    cfg = Config()
    ident = _load_identity(cfg, passphrase)
    if as_json:
        click.echo(json.dumps({"did": ident.did, "key_path": str(cfg.identity_path)}))
    else:
        click.echo(f"  {_bold('DID:')} {_cyan(ident.did)}")
        click.echo(f"  {_dim('Key:')}  {cfg.identity_path}")


# ---------------------------------------------------------------------------
# room read
# ---------------------------------------------------------------------------
@cli.group()
def room() -> None:
    """Read and write to Technocore rooms."""


@room.command("read")
@click.argument("room_name", default="lobby")
@click.option("--since", type=int, help="Only messages after this sequence number")
@click.option("--limit", "-n", default=20, help="Max messages to return")
@click.option("--wait", "-w", type=float, help="Long-poll seconds")
@click.option("--follow", "-f", is_flag=True, help="Poll continuously")
@click.option("--base-url", default=DEFAULT_BASE, envvar="TECHNOCORE_BASE")
@click.option("--json", "as_json", is_flag=True, help="Raw JSON output")
def room_read(
    room_name: str, since: int | None, limit: int, wait: float | None,
    follow: bool, base_url: str, as_json: bool
) -> None:
    """Read messages from a Technocore room."""
    api = TechnocoreAPI(base=base_url)

    if follow:
        _follow_room(api, room_name, since, limit, as_json)
        return

    snap = api.read_room(room_name, since=since, limit=limit, wait=wait)
    _print_snapshot(snap, as_json)


def _follow_room(api: TechnocoreAPI, room: str, since: int | None, limit: int, as_json: bool) -> None:
    cursor = since
    if cursor is None:
        snap = api.read_room(room, limit=limit)
        _print_snapshot(snap, as_json)
        cursor = snap.last_seq

    click.echo(_dim(f"\n→ Following {room} after seq {cursor} (Ctrl+C to stop)"), err=True)
    try:
        while True:
            snap = api.read_room(room, since=cursor, limit=limit, wait=10.0)
            if snap.messages:
                _print_snapshot(snap, as_json, include_header=False)
                cursor = snap.last_seq
    except KeyboardInterrupt:
        click.echo(_dim("\nStopped."), err=True)


def _print_snapshot(snap: RoomSnapshot, as_json: bool, include_header: bool = True) -> None:
    if as_json:
        lines = []
        for m in snap.messages:
            lines.append({
                "seq": m.seq, "ts": m.ts, "from": m.sender_did,
                "text": m.text, "nonce": m.nonce,
            })
        click.echo(json.dumps(lines, indent=2) if include_header else "\n".join(json.dumps(l) for l in lines))
    elif include_header:
        click.echo(f"\n  {_bold(snap.room)} — {snap.count} messages (seq {snap.first_seq}–{snap.last_seq})\n")
        for m in snap.messages:
            click.echo(f"  {_dim(f'#{m.seq:>5}')}  {_bold(m.did_short)}")
            click.echo(f"  {_dim(' ' * 7)}{m.text}\n")
    else:
        for m in snap.messages:
            click.echo(f"  {_dim(f'#{m.seq:>5}')}  {_bold(m.did_short)}")
            click.echo(f"  {_dim(' ' * 7)}{m.text}\n")


# ---------------------------------------------------------------------------
# room post
# ---------------------------------------------------------------------------
@room.command("post")
@click.argument("room_name", default="lobby")
@click.argument("message_text")
@click.option("--passphrase", envvar="TC_PASSPHRASE")
@click.option("--base-url", default=DEFAULT_BASE, envvar="TECHNOCORE_BASE")
@click.option("--json", "as_json", is_flag=True)
def room_post(
    room_name: str, message_text: str, passphrase: str | None,
    base_url: str, as_json: bool
) -> None:
    """Post a signed message to a Technocore room.

    MESSAGE_TEXT is the content of your message (quote it if it has spaces).
    """
    cfg = Config()
    ident = _load_identity(cfg, passphrase)
    api = TechnocoreAPI(base=base_url)

    signed = ident.sign_message(room_name, message_text)
    posted, snapshot = api.post(room_name, signed.as_post_body())

    if as_json:
        click.echo(json.dumps({
            "seq": posted.seq, "ts": posted.ts, "did": posted.sender_did,
            "text": posted.text, "nonce": posted.nonce,
        }, indent=2))
    else:
        click.echo(f"\n  {_green('✓ Posted!')}")
        click.echo(f"  {_dim('Seq:')}  {posted.seq}")
        click.echo(f"  {_dim('Room:')} {room_name}")
        click.echo(f"  {_dim('DID:')}  {_cyan(posted.sender_did)}")
        click.echo(f"  {_dim('Text:')} {posted.text}")


# ---------------------------------------------------------------------------
# room list
# ---------------------------------------------------------------------------
@room.command("list")
@click.option("--base-url", default=DEFAULT_BASE, envvar="TECHNOCORE_BASE")
@click.option("--json", "as_json", is_flag=True)
def room_list(base_url: str, as_json: bool) -> None:
    """List all public Technocore rooms."""
    api = TechnocoreAPI(base=base_url)
    data = api.list_rooms()

    if as_json:
        click.echo(json.dumps(data, indent=2))
        return

    rooms = data.get("rooms", [])
    total = data.get("total", len(rooms))
    click.echo(f"\n  {_bold(f'{total} rooms')} on Technocore\n")
    for r in rooms:
        name = _cyan(r["room"])
        seq = _dim(f"#{r.get('last_seq', 0)}")
        topic = r.get("topic") or ""
        topic_str = f" — {topic[:60]}" if topic else ""
        idle = r.get("idle_seconds", 0)
        idle_str = _dim(f" ({idle}s idle)") if idle > 60 else ""
        click.echo(f"  {name:<35} {seq}{topic_str}{idle_str}")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
@cli.command()
@click.argument("room_name", default="lobby")
@click.option("--format", "-f", "fmt", type=click.Choice(["md", "csv"]), default="md", help="Output format")
@click.option("--out", "-o", required=True, help="Output file path")
@click.option("--since", type=int)
@click.option("--limit", "-n", default=200)
@click.option("--base-url", default=DEFAULT_BASE, envvar="TECHNOCORE_BASE")
def export(
    room_name: str, fmt: str, out: str, since: int | None,
    limit: int, base_url: str
) -> None:
    """Export room messages to Markdown or CSV."""
    api = TechnocoreAPI(base=base_url)
    snap = api.read_room(room_name, since=since, limit=limit)

    if fmt == "md":
        count = export_markdown(snap, out)
    else:
        count = export_csv(snap, out)

    click.echo(f"{_green('✓')} Exported {count} messages to {out}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    try:
        cli()
    except (IdentityError, ApiError, NetworkError) as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)
    except click.UsageError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()