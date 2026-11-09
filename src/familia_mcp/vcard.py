"""Minimal vCard (RFC 2426 / 6350) reading and writing for the contacts tools.

Only what a voice assistant needs: name, phones, emails, birthday,
organisation, address and note. Reading tolerates vCard 3.0 and 4.0 as
written by Nextcloud, iOS, Android (DAVx5) and Google exports; writing
produces vCard 3.0, which every CardDAV client understands.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

NO_YEAR = "1604"  # placeholder year for birthdays without a year (X-APPLE-OMIT-YEAR)
# Properties read by parse(); used to ask the server for nothing else (no photos).
FIELDS = ("VERSION", "UID", "FN", "N", "ORG", "TEL", "EMAIL", "BDAY", "ADR", "NOTE")

_ESCAPES = {"\\n": "\n", "\\N": "\n", "\\,": ",", "\\;": ";", "\\:": ":", "\\\\": "\\"}
_ESCAPE_RE = re.compile(r"\\[nN,;:\\]")


def unescape(value: str) -> str:
    return _ESCAPE_RE.sub(lambda m: _ESCAPES[m.group(0)], value)


def escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n"))


def unfold(text: str) -> list[str]:
    """Join folded lines (CRLF/LF followed by a space or tab)."""
    return re.sub(r"\r?\n[ \t]", "", text).replace("\r\n", "\n").split("\n")


def fold(line: str) -> str:
    """Fold at 75 octets without splitting a UTF-8 character."""
    out, current = [], b""
    for ch in line:
        enc = ch.encode("utf-8")
        if len(current) + len(enc) > (75 if not out else 74):
            out.append(current.decode("utf-8"))
            current = b""
        current += enc
    out.append(current.decode("utf-8"))
    return "\r\n ".join(out)


def _split_unquoted(text: str, sep: str) -> list[str]:
    parts, buf, quoted = [], [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        if ch == sep and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _split_structured(value: str) -> list[str]:
    """Split on ';' not preceded by a backslash, then unescape each part."""
    return [unescape(p) for p in re.split(r"(?<!\\);", value)]


@dataclass
class Prop:
    name: str
    params: dict[str, list[str]]
    value: str

    @property
    def types(self) -> list[str]:
        out = []
        for t in self.params.get("TYPE", []):
            out.extend(x.strip().lower() for x in t.split(",") if x.strip())
        return [t for t in out if t not in ("voice", "internet", "pref", "x-internet")]


def parse_props(text: str) -> list[Prop]:
    props = []
    for line in unfold(text):
        if ":" not in line:
            continue
        head = _split_unquoted(line, ":")[0]
        value = line[len(head) + 1:]  # everything after the first unquoted ':'
        pieces = _split_unquoted(head, ";")
        name = pieces[0].split(".")[-1].upper()  # drop "item1." groups
        params: dict[str, list[str]] = {}
        for p in pieces[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params.setdefault(k.upper(), []).append(v.strip('"'))
            else:  # vCard 2.1 bare types: TEL;CELL:...
                params.setdefault("TYPE", []).append(p)
        props.append(Prop(name, params, value))
    return props


def parse_birthday(raw: str) -> str | None:
    """'19800102', '1980-01-02', '--0102', '--01-02', '1980-01-02T00:00:00Z' -> ISO or '--MM-DD'."""
    raw = raw.strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", raw)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return None
    m = re.match(r"^--(\d{2})-?(\d{2})$", raw)
    return f"--{m[1]}-{m[2]}" if m else None


@dataclass
class Contact:
    name: str
    uid: str | None = None
    phones: list[dict[str, str]] = field(default_factory=list)
    emails: list[dict[str, str]] = field(default_factory=list)
    birthday: str | None = None
    organization: str | None = None
    address: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        for key in ("phones", "emails", "birthday", "organization", "address", "note"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


def parse(text: str) -> Contact | None:
    props = parse_props(text)
    by = lambda n: [p for p in props if p.name == n]  # noqa: E731
    name = next((unescape(p.value).strip() for p in by("FN") if p.value.strip()), "")
    if not name:
        n = next((p for p in by("N")), None)
        if n:
            last, first, *rest = _split_structured(n.value) + ["", ""]
            name = " ".join(x for x in (first, rest[0] if rest else "", last) if x).strip()
    org = next((" ".join(x for x in _split_structured(p.value) if x) for p in by("ORG")), None)
    if not name:
        name = org or ""
    if not name:
        return None
    c = Contact(name=name, uid=next((p.value.strip() for p in by("UID")), None), organization=org or None)
    seen_numbers: set[str] = set()
    for p in by("TEL"):
        number = unescape(p.value).strip()
        if number.lower().startswith("tel:"):
            number = number[4:]
        digits = re.sub(r"\D", "", number)[-9:]  # same number with/without +34 or spaces
        if number and digits not in seen_numbers:
            seen_numbers.add(digits)
            c.phones.append({"number": number, **({"type": p.types[0]} if p.types else {})})
    seen_emails: set[str] = set()
    for p in by("EMAIL"):
        email = unescape(p.value).strip()
        if email and email.lower() not in seen_emails:
            seen_emails.add(email.lower())
            c.emails.append({"email": email, **({"type": p.types[0]} if p.types else {})})
    bday = next((p for p in by("BDAY")), None)
    if bday:
        c.birthday = parse_birthday(bday.value)
        omit = bday.params.get("X-APPLE-OMIT-YEAR", [""])[0]
        if c.birthday and omit and c.birthday.startswith(omit + "-"):
            c.birthday = "--" + c.birthday[len(omit) + 1:]
    adr = next((p for p in by("ADR")), None)
    if adr:
        # ADR: PO box; extended; street; locality; region; postal code; country
        parts = [x.strip() for x in _split_structured(adr.value)]
        c.address = ", ".join(x for x in parts[2:] + parts[:2] if x) or None
    note = next((unescape(p.value).strip() for p in by("NOTE")), None)
    c.note = note or None
    return c


def build(contact: Contact, prodid: str = "-//home-mcp//EN") -> str:
    """vCard 3.0 text with CRLF line endings."""
    uid = contact.uid or str(uuid.uuid4())
    parts = contact.name.split()
    given, family = (" ".join(parts[:1]), " ".join(parts[1:])) if parts else ("", "")
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"PRODID:{prodid}", f"UID:{escape(uid)}",
             f"FN:{escape(contact.name)}", f"N:{escape(family)};{escape(given)};;;"]
    for ph in contact.phones:
        t = ph.get("type")
        lines.append(f"TEL;TYPE={t.upper()}:{escape(ph['number'])}" if t else f"TEL:{escape(ph['number'])}")
    for em in contact.emails:
        t = em.get("type")
        lines.append(f"EMAIL;TYPE=INTERNET,{t.upper()}:{escape(em['email'])}" if t
                     else f"EMAIL;TYPE=INTERNET:{escape(em['email'])}")
    if contact.birthday:
        b = contact.birthday
        if b.startswith("--"):  # no year: the convention Nextcloud and iOS understand
            lines.append(f"BDAY;X-APPLE-OMIT-YEAR={NO_YEAR}:{NO_YEAR}-{b[2:]}")
        else:
            lines.append(f"BDAY:{b}")
    if contact.organization:
        lines.append(f"ORG:{escape(contact.organization)}")
    if contact.address:
        # Free text in the street component: voice gives no reliable structure.
        lines.append(f"ADR;TYPE=HOME:;;{escape(contact.address)};;;;")
    if contact.note:
        lines.append(f"NOTE:{escape(contact.note)}")
    lines.append("END:VCARD")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"
