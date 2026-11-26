"""Does a listing sell the part the buyer asked for, or only a piece of it?

Asking for "headlight" must not rank "Headlight Glass" (PKR 1,350) or an "LED Headlight H4" bulb as the cheapest
headlight. Components are almost always cheaper than the assembly, so without this they win "cheapest" every time.

classify(wanted, sold_text, llm_kind) -> (kind, reason)
  exact      the requested part itself (a complete headlight / radiator / mirror ...)
  component  a piece of the requested part sold on its own (glass, lens, bulb, cover, bracket, cap, sensor ...)
  related    not the component the buyer asked for (asked "headlight glass", got a whole headlight or another part)
  other      a different part / accessory

Code rules decide when the words are explicit; the AI's own item_kind breaks ties when they are not.
"""
from __future__ import annotations

import re

# Pieces that are sold separately from the assembly they belong to.
COMPONENT_WORDS = {
    "glass", "lens", "lense", "cover", "bracket", "brackets", "holder", "socket", "harness", "wiring", "connector",
    "clip", "clips", "screw", "screws", "bolt", "bolts", "nut", "seal", "seals", "gasket", "oring", "cap", "sensor",
    "switch", "relay", "fuse", "motor", "bush", "bushing", "bushes", "mount", "mounting", "knob", "rubber", "washer",
    "hose", "fan", "shroud", "tank", "valve", "cable", "pin", "spring", "boot", "dust", "trim", "frame", "housing",
    "reflector", "adjuster", "actuator", "regulator", "lid", "plug", "grommet", "bezel", "sticker", "decal",
    "shim", "shims", "bearing", "liner", "insert", "repair", "film", "tint", "protector",
}
# Lamp bulbs: a bulb is never the lamp.
BULB_WORDS = {"bulb", "bulbs", "globe", "globes", "h1", "h3", "h4", "h7", "h8", "h11", "h13", "h15", "h16", "hb3", "hb4",
              "9005", "9006", "9012", "d1s", "d2s", "d3s", "d4s", "t10", "w5w", "1156", "1157", "7440", "7443", "880", "881"}
LIGHT_TECH = {"led", "halogen", "hid", "xenon", "smd", "cob"}          # bulb OR lamp, depends on context
ASSEMBLY_WORDS = {"assembly", "assy", "assey", "asy", "complete", "full", "unit", "whole", "set", "pair", "lamp", "lamps"}
INCLUDES = re.compile(r"\b(with|w/|incl|including|includes|included|plus|\+|&|and)\b.*$", re.I)
SIDE = {"left", "right", "lh", "rh", "front", "rear"}


# Trade names for the same part: a title naming any of them names the part.
HEAD_ALIASES = {
    "headlight": ["headlight", "headlamp", "head lamp", "head light", "front lamp", "front light"],
    "taillight": ["taillight", "tail light", "taillamp", "tail lamp", "back light", "rear lamp", "rear light"],
    "mirror": ["mirror"],
    "shock": ["shock", "damper", "strut"],
    "radiator": ["radiator"],
    "bumper": ["bumper"],
}


def _words(s: str) -> list[str]:
    s = re.sub(r"o-ring", "oring", (s or "").lower())
    return re.findall(r"[a-z0-9]+", s)


def _singular(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _head(wanted_words: list[str]) -> list[str]:
    """The nouns that name the part ('headlight', 'radiator', 'mirror', 'pad'), not qualifiers."""
    return [_singular(w) for w in wanted_words if w not in SIDE and w not in COMPONENT_WORDS and w not in BULB_WORDS
            and len(w) > 2]


def classify(wanted: str, sold: str, llm_kind: str | None = None, title: str | None = None) -> tuple[str, str]:
    """`sold` = everything known about the item (AI description + seller title) for spotting component words;
    `title` = the seller's own title, the only text trusted to prove the listing NAMES the part (the AI's
    description may add the word "headlight" to a 'Halogen Pair')."""
    want = _words(wanted)
    want_set = {_singular(w) for w in want}
    full = _words(sold)
    named_in = _words(title) if title else full
    core = _words(INCLUDES.sub("", sold or ""))      # "brake pads with wear sensor": the sensor is an extra
    core_set = {_singular(w) for w in core}
    full_set = {_singular(w) for w in full}
    has_assembly = bool(core_set & ASSEMBLY_WORDS)
    heads = _head(want)
    text = " ".join(named_in)
    joined = "".join(named_in)
    names = [a for h in heads for a in HEAD_ALIASES.get(h, [h])]
    if "".join(want).find("taillight") >= 0 or ("tail" in want and "light" in want):
        names += HEAD_ALIASES["taillight"]
    if "".join(want).find("headlight") >= 0 or ("head" in want and "light" in want):
        names += HEAD_ALIASES["headlight"]
    head_named = any(n in text or n.replace(" ", "") in joined for n in names) if heads else True

    # 1. Buyer asked for a component (e.g. "headlight glass"): a listing without that word is the bigger assembly.
    wanted_components = (want_set & COMPONENT_WORDS) | (want_set & BULB_WORDS)
    if wanted_components:
        if not (wanted_components & full_set):
            return "related", f"you asked for the {' '.join(sorted(wanted_components))} only"
        return "exact", "the requested component"

    # 2. Bulb words: a bulb is never the lamp ("LED Headlight H4", "headlight bulb").
    bulbs = (core_set & BULB_WORDS)
    if bulbs:
        return "component", f"bulb ({', '.join(sorted(bulbs))}), not the complete {wanted}"

    # 3. A component word the buyer did not ask for, and no "assembly / complete" -> a piece of the part.
    comps = sorted((core_set & COMPONENT_WORDS) - want_set)
    if comps and not has_assembly:
        return "component", f"{', '.join(comps)} only, not the complete {wanted}"

    # 4. LED / halogen / HID with no lamp noun and no assembly word = bulbs ("Suzuki Alto Halogen Pair").
    if core_set & LIGHT_TECH and not head_named and not (core_set & {"assembly", "assy", "assey", "complete", "unit"}):
        return "component", (f"the seller's title doesn't say {wanted} ({', '.join(sorted(core_set & LIGHT_TECH))}): "
                             f"probably bulbs, not the complete {wanted}")

    # 5. The title names the part and contains no component word: it IS the part, whatever the AI guessed
    #    ("Suzuki Alto car headlight left side for sale" was once misread as a component).
    if head_named:
        return "exact", "complete part"
    # 6. Title does not even name the part: use the AI's reading of the listing.
    if llm_kind in ("component", "accessory", "other_part"):
        return ("component" if llm_kind == "component" else "other"), f"listing reads as {llm_kind.replace('_', ' ')}"
    return "exact", "complete part"
