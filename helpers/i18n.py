#!/usr/bin/env python3
"""Update or insert translation keys across every locale file.

This is the i18n helper for the XMEye integration. It supports arbitrary
nested paths inside the JSON (entity.*, options.*, services.*, issues.*,
…), so it can be used to add, rename, or update any string the
integration shows to the user.

The script preserves Python's native dict ordering (insertion order on
Python 3.7+) and writes each locale with `sort_keys=True, indent=2` so
the output is deterministic and easy to diff. Running it twice with the
same arguments is a no-op (bytewise).

CAVEATS
-------
* The script does NOT verify that the new key is referenced by Python
  code. After adding keys, run with --check to make sure every locale
  has the same leaf set as strings.json / en.json.
* The source of truth is `strings.json` — add the new key there first
  so the next CI run of the i18n validator agrees with every locale.
* Each locale file is fully rewritten on every change. The diff against
  git is "the only change is the new key + the surrounding indent
  bubble" — perfectly reviewable.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_LOCALES_DIR = REPO_ROOT / "custom_components" / "xmeye" / "translations"
DEFAULT_STRINGS = REPO_ROOT / "custom_components" / "xmeye" / "strings.json"

SUPPORTED_LOCALES = [
    "de", "en", "es", "fr", "it", "ja", "nl", "pl", "pt", "ru", "tr", "zh-Hans",
]

DUMP_KWARGS = {"ensure_ascii": False, "indent": 2, "sort_keys": True}


def _decode_cli_string(s: str) -> str:
    """Decode only the standard ASCII escapes (\\n, \\t, \\r, \\\\,
    \\\", \\u00XX) in `s`. Leaves all other characters (including
    UTF-8 multi-byte sequences) untouched.

    `bytes.decode('unicode_escape')` would mangle UTF-8 because it
    interprets the raw bytes of non-ASCII code points as escape
    sequences, so we walk character-by-character instead.
    """
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(s):
            out.append("\\")
            i += 1
            continue
        nxt = s[i + 1]
        if nxt == "n":
            out.append("\n")
            i += 2
        elif nxt == "t":
            out.append("\t")
            i += 2
        elif nxt == "r":
            out.append("\r")
            i += 2
        elif nxt == "\\":
            out.append("\\")
            i += 2
        elif nxt == '"':
            out.append('"')
            i += 2
        elif nxt == "'":
            out.append("'")
            i += 2
        elif nxt == "0":
            out.append("\0")
            i += 2
        elif nxt == "u" and i + 5 < len(s) and all(c in "0123456789abcdefABCDEF" for c in s[i + 2 : i + 6]):
            out.append(chr(int(s[i + 2 : i + 6], 16)))
            i += 6
        else:
            # Unknown escape — keep the backslash literally.
            out.append("\\")
            i += 1
    return "".join(out)


def parse_kv(tokens: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in tokens:
        if "=" not in tok:
            raise SystemExit(f"bad argument (expected key=value): {tok!r}")
        k, _, v = tok.partition("=")
        out[k.strip()] = _decode_cli_string(v)
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--locales-dir", type=pathlib.Path, default=DEFAULT_LOCALES_DIR)
    parser.add_argument("--strings", type=pathlib.Path, default=DEFAULT_STRINGS)
    parser.add_argument(
        "--check", action="store_true",
        help="Verify every locale has the same leaf set as strings.json.",
    )
    parser.add_argument(
        "--set", action="append", default=[], dest="raw_sets", nargs="+",
        metavar="PATH k=v k=v …",
        help=(
            "Update one key per --set. The first token is the dotted JSON "
            "path; the rest are k=v pairs. Repeat --set for multiple keys."
        ),
    )
    args = parser.parse_args()
    args.sets: list[tuple[tuple[str, ...], dict[str, str]]] = []
    for tokens in args.raw_sets:
        if len(tokens) < 2:
            raise SystemExit(f"--set expects PATH + k=v tokens; got {tokens!r}")
        path_str, *kv = tokens
        path = tuple(path_str.split("."))
        if not path or not all(p for p in path):
            raise SystemExit(f"invalid path in --set: {path_str!r}")
        args.sets.append((path, parse_kv(kv)))
    return args


def set_path(obj: dict, path: tuple[str, ...], value: object) -> None:
    """Set `obj[path] = value`, creating intermediate dicts as needed."""
    for segment in path[:-1]:
        if segment not in obj or not isinstance(obj[segment], dict):
            obj[segment] = {}
        obj = obj[segment]
    obj[path[-1]] = value


def leaves(obj: object, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Return the set of leaf paths (tuples of segments) in a JSON object."""
    out: set[tuple[str, ...]] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = (*prefix, k)
            if isinstance(v, dict):
                out |= leaves(v, child)
            else:
                out.add(child)
    return out


def update_locale(sets: list[tuple[tuple[str, ...], dict[str, str]]], locale: str, path: pathlib.Path) -> bool:
    relevant = [(p, v) for p, v in sets if locale in v]
    if not relevant:
        return False
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    for p, v in relevant:
        set_path(data, p, v[locale])
    new_raw = json.dumps(data, **DUMP_KWARGS) + "\n"
    if new_raw == raw:
        return False
    path.write_text(new_raw, encoding="utf-8")
    return True


def main() -> int:
    args = parse_args()
    if not args.locales_dir.is_dir():
        print(f"locales dir not found: {args.locales_dir}", file=sys.stderr)
        return 1

    if args.strings.exists():
        strings = json.loads(args.strings.read_text(encoding="utf-8"))
        known = leaves(strings)
        for path, _ in args.sets:
            if path not in known:
                print(
                    f"warning: {'.'.join(path)} is not in {args.strings.name} — "
                    f"add it there first (it's the source of truth)",
                    file=sys.stderr,
                )

    if args.check:
        en = json.loads((args.locales_dir / "en.json").read_text(encoding="utf-8"))
        en_keys = leaves(en)
        bad = False
        for f in sorted(args.locales_dir.glob("*.json")):
            if f.name == "en.json":
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
            keys = leaves(data)
            missing = en_keys - keys
            extra = keys - en_keys
            if missing or extra:
                bad = True
                print(f"  {f.name}: missing={sorted(missing)} extra={sorted(extra)}")
        if not bad:
            print("All locales match en.json.")
        return 1 if bad else 0

    if not args.sets:
        print("nothing to do (no --set given; use --check to verify)", file=sys.stderr)
        return 0

    total = 0
    for locale in SUPPORTED_LOCALES:
        path = args.locales_dir / f"{locale}.json"
        if not path.exists():
            print(f"  skip: {path.name} (missing)")
            continue
        if update_locale(args.sets, locale, path):
            total += 1
            print(f"  updated: {path.name}")
        else:
            print(f"  unchanged: {path.name}")
    print(f"\n{total} locale file(s) updated.")
    if total:
        print("Run with --check to verify key parity with strings.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
