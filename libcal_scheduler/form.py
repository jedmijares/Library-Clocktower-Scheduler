"""Parsing CPL's booking form and building a submission payload.

Two things make this more than a field dump.

**Honeypots.** Every branch's form carries one extra `required` text input whose
container is deleted by an inline `<script>` before a human sees it. The field name
and container id rotate per branch — `Zip`, `Feedback`, `Name`, `Question` and `URL`
were all observed across seven forms — so nothing here may depend on knowing the
name. Detection prefers the `removeChild` call (behavioural) over the container class
(cosmetic, and the more likely to churn).

**The payload is an allowlist, never an enumeration.** `build_payload` sends only the
identity fields and the question ids a field map explicitly names. Combined with
positional exclusion of honeypot elements, a honeypot that happened to share a real
field's name would still be handled correctly rather than filled.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field as dc_field
from html.parser import HTMLParser

from .config import IDENTITY_FIELDS, FieldMap

_CONTAINERS = {"div", "fieldset", "form"}
_CONTROLS = {"input", "select", "textarea"}

# Class tokens seen on honeypot containers: s-input-hp, s-inph-ox, s-hpinp-ox, slc-hi.
_HONEYPOT_CLASS = re.compile(r"(?:^|-)(?:hp|hi|inph|hpinp)(?:$|-)|hp(?:inp)?|inph")

_REMOVE_CHILD = re.compile(r"""getElementById\(\s*["']([^"']+)["']\s*\)""")

_BUCKET = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


@dataclass
class Field:
    name: str
    kind: str
    required: bool = False
    container_id: str = ""
    container_class: str = ""
    label: str = ""
    value: str = ""  # the element's own value attribute (radios, checkboxes, hidden)
    options: tuple[str, ...] = ()  # <select> only
    honeypot: bool = False
    signals: tuple[str, ...] = ()

    @property
    def is_acknowledgement(self) -> bool:
        return self.kind == "checkbox" and self.name.endswith("[]")


@dataclass
class BookingForm:
    session: str = ""
    action: str = ""
    fields: list[Field] = dc_field(default_factory=list)

    @property
    def honeypots(self) -> list[Field]:
        return [f for f in self.fields if f.honeypot]

    @property
    def real_fields(self) -> list[Field]:
        return [f for f in self.fields if not f.honeypot]

    def by_name(self, name: str) -> list[Field]:
        return [f for f in self.real_fields if f.name == name]

    def options_for(self, name: str) -> tuple[str, ...]:
        for candidate in self.by_name(name):
            if candidate.options:
                return candidate.options
        return ()

    def choices_for(self, name: str) -> tuple[str, ...]:
        """Every value the form will accept for `name`.

        A `<select>` carries its options directly; radio and checkbox groups spread
        theirs across sibling elements sharing the name, so they have to be gathered.
        """
        controls = self.by_name(name)
        if not controls:
            return ()
        if controls[0].options:
            return controls[0].options
        if controls[0].kind in ("radio", "checkbox"):
            return tuple(dict.fromkeys(c.value for c in controls if c.value))
        return ()

    def required_names(self) -> set[str]:
        return {f.name for f in self.real_fields if f.required}


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.form = BookingForm()
        self.removed_ids: set[str] = set()
        self._stack: list[tuple[str, str, str]] = []
        self._script = False
        self._script_text: list[str] = []
        self._select: Field | None = None
        self._option: list[str] | None = None
        self._option_value: str | None = None
        self._labels: dict[str, str] = {}
        self._label_for: str | None = None
        self._label_text: list[str] = []

    # -- structure ---------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "script":
            self._script = True
            self._script_text = []
            return
        if tag == "form":
            self.form.action = a.get("action", self.form.action)
        if tag in _CONTAINERS:
            self._stack.append((tag, a.get("id", ""), a.get("class", "")))
            return
        if tag == "label":
            self._label_for = a.get("for") or None
            self._label_text = []
            return
        if tag == "option" and self._select is not None:
            # Options may carry no value attribute, in which case the submitted value
            # is the element's text — which is how q10099's ranges work.
            self._option_value = a.get("value") if "value" in a else None
            self._option = []
            return
        if tag not in _CONTROLS:
            return

        name = a.get("name", "")
        kind = a.get("type", tag) if tag == "input" else tag
        container_id, container_class = "", ""
        for _, cid, cls in reversed(self._stack):
            if cid or cls:
                container_id, container_class = cid, cls
                break

        if name == "session" and kind == "hidden":
            self.form.session = a.get("value", "")

        entry = Field(
            name=name,
            kind=kind,
            required="required" in a or a.get("aria-required") == "true",
            container_id=container_id,
            container_class=container_class,
            value=a.get("value", ""),
        )
        if tag == "select":
            self._select = entry
        self.form.fields.append(entry)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            joined = "".join(self._script_text)
            if "removeChild" in joined:
                self.removed_ids.update(_REMOVE_CHILD.findall(joined))
            self._script = False
            return
        if tag in _CONTAINERS and self._stack:
            self._stack.pop()
            return
        if tag == "label":
            text = html.unescape("".join(self._label_text))
            text = re.sub(r"\s+", " ", text).strip().rstrip("*").strip().rstrip(":")
            if self._label_for and text:
                self._labels[self._label_for] = text
            self._label_for = None
            return
        if tag == "option" and self._select is not None and self._option is not None:
            text = re.sub(r"\s+", " ", html.unescape("".join(self._option))).strip()
            value = self._option_value if self._option_value is not None else text
            if value:
                self._select.options = self._select.options + (value,)
            self._option = None
            self._option_value = None
            return
        if tag == "select":
            self._select = None

    def handle_data(self, data: str) -> None:
        if self._script:
            self._script_text.append(data)
        if self._option is not None:
            self._option.append(data)
        if self._label_for is not None:
            self._label_text.append(data)


def parse_form(markup: str) -> BookingForm:
    """Parse a booking form, flagging honeypot fields."""
    parser = _Parser()
    parser.feed(markup)
    parser.close()
    form = parser.form

    # Attach labels by id where we can (LibCal uses for="<id>" on real questions).
    id_labels = parser._labels
    for entry in form.fields:
        if not entry.label:
            entry.label = id_labels.get(entry.name, "")

    for entry in form.fields:
        signals: list[str] = []
        if entry.container_id and entry.container_id in parser.removed_ids:
            signals.append("removed-by-script")
        if entry.container_class and _HONEYPOT_CLASS.search(entry.container_class):
            signals.append("honeypot-class")
        if signals:
            entry.honeypot = True
            entry.signals = tuple(signals)
    return form


def pick_bucket(options: tuple[str, ...], count: int) -> str | None:
    """Choose the option whose numeric range contains `count`.

    `q10099` offers ranges ("1-5", "11-20", … "126-178") rather than numbers, and the
    list is not capped at room capacity, so the caller still has to check that
    separately.
    """
    for option in options:
        match = _BUCKET.match(option)
        if match and int(match.group(1)) <= count <= int(match.group(2)):
            return option
    return None


class PayloadError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("cannot build a submission payload:\n  - " + "\n  - ".join(problems))


def build_payload(
    form: BookingForm,
    field_map: FieldMap,
    values: dict,
    requester: dict,
    *,
    auto_check_acknowledgements: bool = True,
) -> dict[str, object]:
    """Build the submission payload from an allowlist of known fields.

    Nothing is filled because it *looks* required — only identity fields and the
    question ids `field_map` names are ever sent. Raises if a field the branch
    requires could not be filled, so a partial submission is impossible.
    """
    problems: list[str] = []
    payload: dict[str, object] = {}
    honeypot_names = {f.name for f in form.honeypots}

    identity = {
        "fname": requester.get("firstName", ""),
        "lname": requester.get("lastName", ""),
        "email": requester.get("email", ""),
    }
    for name in IDENTITY_FIELDS:
        # by_name() already excludes honeypots, so a honeypot sharing this name
        # cannot shadow the real control.
        if not form.by_name(name):
            problems.append(f"{name}: not present in the form")
            continue
        if not identity[name].strip():
            problems.append(f"{name}: no value configured")
            continue
        payload[name] = identity[name]

    for key, target in field_map.fields.items():
        if key == "acknowledgements":
            continue
        names = target if isinstance(target, list) else [target]
        raw = values.get(key)
        text = "" if raw is None else str(raw)

        for name in names:
            controls = form.by_name(name)
            if not controls:
                if key in field_map.required:
                    problems.append(f"{key}: field {name} is required here but absent from the form")
                continue
            control = controls[0]
            choices = form.choices_for(name)

            if choices and key == "expectedAttendance":
                bucket = pick_bucket(choices, int(raw)) if isinstance(raw, int) else None
                if bucket is None:
                    problems.append(
                        f"expectedAttendance: {raw!r} matches none of {name}'s ranges "
                        f"({', '.join(choices)})"
                    )
                    continue
                payload[name] = bucket
                continue

            # Covers <select> options and radio/checkbox groups alike.
            if choices and text and text not in choices:
                problems.append(
                    f"{key}: {text!r} is not one of {name}'s accepted values ({', '.join(choices)})"
                )
                continue

            if not text.strip():
                if key in field_map.required:
                    problems.append(f"{key}: required by this branch but no value configured")
                continue
            payload[name] = text

    if auto_check_acknowledgements:
        for name in field_map.field_names("acknowledgements"):
            controls = form.by_name(name)
            if not controls:
                continue
            # Take the value the form itself declares rather than assuming the wording.
            payload[name] = controls[0].value or "I understand."

    # Final assertion: nothing outside the allowlist, and no honeypot.
    allowed = set(IDENTITY_FIELDS)
    for target in field_map.fields.values():
        allowed.update(target if isinstance(target, list) else [target])
    stray = sorted(set(payload) - allowed)
    if stray:
        problems.append(f"payload contains keys outside the allowlist: {stray}")
    trapped = sorted(set(payload) & honeypot_names)
    if trapped:
        problems.append(f"payload would fill honeypot field(s): {trapped}")

    if problems:
        raise PayloadError(problems)
    return payload
