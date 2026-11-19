"""Which version of the bot made a signal or a trade: its "Stand".

The Stand is a short code computed at startup from the bot's program files
and from every setting except the secrets. It changes whenever the code or a
setting changes. Every row of the journals carries the Stand it was written
under, and versions.csv lists each Stand with the settings that changed, so
results can be compared before and after a change.

No IBKR or config imports, so it can be tested on its own.
"""
import glob
import hashlib
import json
import os

import journal

VERSION_FIELDS = [
    "first_seen_utc", "stand", "code_hash", "code_changed", "settings_changes", "settings_json",
]

_SECRET_PARTS = ("secret", "token", "password")


def snapshot(settings_dict: dict) -> dict:
    """The settings worth comparing: all of them except anything secret."""
    return {
        k: v for k, v in sorted(settings_dict.items())
        if not any(part in k.lower() for part in _SECRET_PARTS)
    }


def code_hash(app_dir: str) -> str:
    h = hashlib.sha256()
    for path in sorted(glob.glob(os.path.join(app_dir, "*.py"))):
        h.update(os.path.basename(path).encode())
        with open(path, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:8]


def stand_id(code: str, snap: dict) -> str:
    text = code + json.dumps(snap, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def diff(old: dict, new: dict) -> str:
    parts = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            parts.append(f"{key}: {old.get(key, '-')} -> {new.get(key, '-')}")
    return "; ".join(parts)


def register(path: str, app_dir: str, settings_dict: dict) -> str:
    """Returns the current Stand and adds a row to versions.csv when it
    differs from the last one recorded there."""
    snap = snapshot(settings_dict)
    code = code_hash(app_dir)
    sid = stand_id(code, snap)
    rows = journal.read_rows(path)
    last = rows[-1] if rows else None
    if last is None or last.get("stand") != sid:
        old_snap = {}
        if last:
            try:
                old_snap = json.loads(last.get("settings_json") or "{}")
            except ValueError:
                old_snap = {}
        journal.append_row(path, VERSION_FIELDS, {
            "first_seen_utc": journal.now_utc(),
            "stand": sid,
            "code_hash": code,
            "code_changed": "ja" if last is None or last.get("code_hash") != code else "nein",
            "settings_changes": "erster Eintrag" if last is None else diff(old_snap, snap),
            "settings_json": json.dumps(snap, sort_keys=True, default=str),
        })
    return sid
