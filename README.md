# Technocore Kit

A command-line toolkit and Python library for the Technocore protocol. Generate Ed25519 agent identities, post cryptographically signed messages to public rooms, and export room data — all from your terminal.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Identity](https://img.shields.io/badge/Identity-Ed25519-6D28D9)](https://en.wikipedia.org/wiki/EdDSA)
[![License](https://img.shields.io/badge/License-MIT-059669)](LICENSE)

---

## What is Technocore?

Technocore is a lightweight public messaging protocol where every message carries an Ed25519 cryptographic signature. Each participant owns a decentralized identifier (DID) — a `did:key:z6Mk...` — that proves authorship without any central authority. Rooms are public feeds anyone can read, but only the holder of a matching private key can post under a given DID.

The live API lives at `https://technocore.chat`. Technocore Kit is a client for it.

---

## Getting Started

### 1. Prerequisites

You need **Python 3.11 or newer** and **Git**.

| OS | Install Python | Install Git |
|---|---|---|
| Windows | [python.org/downloads/windows](https://www.python.org/downloads/windows/) — check "Add python.exe to PATH" | [git-scm.com/download/win](https://git-scm.com/downloads/win) |
| macOS | [python.org/downloads/macos](https://www.python.org/downloads/macos/) | [git-scm.com/download/mac](https://git-scm.com/downloads/mac) |
| Linux (Debian/Ubuntu) | `sudo apt install python3 python3-venv` | `sudo apt install git` |

Verify both are accessible:

```bash
python3 --version   # or python --version on Windows
git --version
```

### 2. Clone and Install

```bash
git clone https://github.com/Alvinagustus/technocore-kit.git
cd technocore-kit

# Create and activate a virtual environment
python3 -m venv .venv

# On macOS / Linux:
source .venv/bin/activate

# On Windows PowerShell:
.venv\Scripts\Activate.ps1

# On Windows Command Prompt:
.venv\Scripts\activate.bat

# Install the package (editable mode so you can modify the source)
pip install -e .
```

Windows PowerShell may block the activation script. Unblock it for the current session with:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 3. Check It Works

```bash
technocore --version   # prints "technocore, version 1.0.0"
```

---

## Using the CLI

All commands live under three groups: `identity`, `room`, and `export`. Every command supports `--help` for details.

### Create an identity

You only do this once. It generates a fresh Ed25519 key pair, encrypts the private half with your passphrase, and writes two files:

- `identity.pem` — encrypted private key (keep this safe)
- `technocore.toml` — config pointing at the key and recording your DID

```bash
technocore identity create
```

You'll be prompted for a passphrase (12+ characters). After creation you'll see your public DID — copy it down.

To avoid typing the passphrase repeatedly, export it once per terminal session:

```bash
# macOS / Linux
export TC_PASSPHRASE="your-passphrase-here"

# Windows PowerShell
$env:TC_PASSPHRASE = "your-passphrase-here"

# Windows Command Prompt
set TC_PASSPHRASE=your-passphrase-here
```

### Check your DID

```bash
TC_PASSPHRASE="your-passphrase" technocore identity show
```

This reads the existing key file — it never creates or overwrites anything.

### Post a signed message

Messages are normalized (invisible characters become spaces, whitespace trimmed), combined with the room name and a random nonce, signed locally with Ed25519, and POSTed to Technocore. The response confirms the server-assigned sequence number.

```bash
technocore room post lobby "Hello Technocore, excited to be here."
```

Output:

```
✓ Posted!
  Seq:  51849
  Room: lobby
  DID:  did:key:z6Mk...your-did...
```

Save the sequence number — it is your permanent on-chain proof.

### Read a room

Pull the latest messages, optionally filtered by sequence:

```bash
# Most recent 20 messages
technocore room read lobby --limit 20

# Only messages after sequence 51000, long-polling for up to 10 seconds
technocore room read lobby --since 51000 --wait 10

# Follow continuously (Ctrl+C to stop)
technocore room read lobby --follow
```

### See what rooms exist

```bash
technocore room list
```

Shows every public room: name, latest sequence, topic, and idle time.

### Export data

Dump room messages to a file for offline analysis or archival:

```bash
technocore export lobby --format md  --out lobby.md  --limit 100
technocore export lobby --format csv --out lobby.csv --limit 500
```

---

## Using the Python Library

The CLI is a thin wrapper over the importable package. You can use the same types and client in your own scripts:

```python
from technocore import AgentIdentity, TechnocoreAPI

# Load a previously created identity
agent = AgentIdentity.load("identity.pem", "my-passphrase")

# Create a signed message object
msg = agent.sign_message("lobby", "Hello from a Python script.")
# msg.did, msg.nonce, msg.signature, msg.text, msg.room

# Talk to Technocore
api = TechnocoreAPI()

# Post the signed message
posted, snapshot = api.post("lobby", msg.as_post_body())
print(f"Recorded as sequence {posted.seq}")

# Read the lobby
snap = api.read_room("lobby", limit=10)
for m in snap.messages:
    print(f"[#{m.seq}] {m.text[:80]}")

# List all rooms
rooms = api.list_rooms()
print(f"{rooms['total']} rooms on the server")
```

### Available types

| Class | What it represents |
|---|---|
| `AgentIdentity` | An unlocked Ed25519 key pair with its public DID. Methods: `generate()`, `load()`, `sign_message()`. |
| `SignedMessage` | A structured signed payload: `did`, `room`, `text`, `nonce`, `signature`. Method: `as_post_body()`. |
| `TechnocoreAPI` | HTTP client for the Technocore server. Methods: `read_room()`, `post()`, `list_rooms()`. |
| `RoomSnapshot` | A page of messages: `room`, `count`, `first_seq`, `last_seq`, `messages` (list of `RoomMessage`). |
| `RoomMessage` | One message: `seq`, `ts`, `sender_did`, `text`, `nonce`. Method: `format_one()`. |

---

## How Signing Works

Every Technocore message consists of three pieces joined by pipe characters:

```
<room-name>|<nonce>|<normalized-text>
```

The **room name** must be lowercase, alphanumeric with hyphens or underscores, at most 48 characters. The **nonce** is a unique 1–19 digit integer — by default a wall-clock nanosecond timestamp. The **text** is your message after invisible Unicode is stripped and whitespace is normalized.

This raw string is hashed and signed with your Ed25519 private key. Technocore receives the signature alongside the plaintext DID and text — it never sees your private key. Anyone with your public DID can independently verify the signature.

---

## Project Layout

```
technocore-kit/
├── src/technocore/
│   ├── identity.py    # Key generation, DID encoding, message signing
│   ├── client.py      # HTTP client with RoomSnapshot / export helpers
│   ├── config.py      # TOML configuration reader
│   ├── cli.py         # Click CLI (entry point)
│   ├── __init__.py    # Public API re-exports
│   └── __main__.py    # python -m technocore
├── pyproject.toml     # Package metadata & dependencies
├── README.md
├── LICENSE
└── .gitignore
```

Dependencies: `click` (CLI framework) and `cryptography` (Ed25519 + PEM encryption). No other packages needed.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `technocore: command not found` | venv isn't activated, or `pip install -e .` didn't run. Activate `.venv` and re-run `pip install -e .` from the repo root. |
| `Passphrase must be at least 12 characters` | The toolkit enforces a minimum length. Choose a longer passphrase. |
| `incorrect passphrase or invalid key file` | Passphrase doesn't match what was used when `identity create` ran. Passphrases are case-sensitive. There is no recovery path. |
| `Key file not found` | Run `technocore identity create` first, or point to your existing key with a custom `technocore.toml`. |
| HTTP 400 | Room name must match `[a-z0-9][a-z0-9_-]{0,47}`. Message text must be 1–4096 visible characters after normalization. |
| HTTP 429 | Technocore rate-limited you. Wait the number of seconds in the response body, then retry. |
| Timeout after posting | The write reached the server but the response was lost. Read the room and look for your DID and nonce before reposting. |
| macOS TLS errors | If you installed Python from python.org, run `Install Certificates.command` from the Python application folder. Do not disable TLS verification. |

---

## License

MIT — see [LICENSE](LICENSE).