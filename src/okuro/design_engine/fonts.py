# SPDX-License-Identifier: Apache-2.0
"""Font weight slots: a verbatim style name on one side, a CSS number on the other.

The owner, on why the authored value is a string:

    "All brand specific font settings. Are not directly transferrable as code
    supports weights of 100 - 900. But the font files installed on a Mac or on
    a Linux machine render things like "Light, Regular, etc." in the Figma UI.
    That's why font-weight isn't using number for weight. Programmatically this
    would be css weights."

And his own follow-up question, which P0 q5 answered:

    "Do you have integrated, that font-weights in web will be dealt differently
    (numbers) than what currently stands in the system?"

P0 q5 answer: the UI offers *either* a dropdown of style names or a weight
slider, "both filtered by the font's actually available values". So the numeric
side and the available range are read from the font faces themselves; the UI
never offers a weight the family cannot render. A slot therefore carries the
descriptor the brand authored, and the number comes from the face.

The slots are FATNESS RANGES, not fixed names -- his 2026-08-17 correction. One
family fills them ExtraBold/Bold/Regular/Light, another
ExtraBlack/Bold/Regular/Light, another Black/Bold/Medium/Light, because "Black
and ExtraBold are in a similar range". Slot 1 is the heaviest the family
offers, slot 4 the lightest in use. The names therefore do not transfer between
brands, which is why the slot holds a descriptor and not a number.
"""

from __future__ import annotations

# CSS-standard style-name to weight mapping. This is a FALLBACK, used only when
# a brand ships no measured faces: the authority is the font file itself
# (OS/2 usWeightClass, or the variable font's wght axis), per q5.
CANONICAL_WEIGHT_NAMES: dict[str, int] = {
    "thin": 100,
    "hairline": 100,
    "extralight": 200,
    "ultralight": 200,
    "light": 300,
    "regular": 400,
    "normal": 400,
    "book": 400,
    "medium": 500,
    "semibold": 600,
    "demibold": 600,
    "bold": 700,
    "extrabold": 800,
    "ultrabold": 800,
    "black": 900,
    "heavy": 900,
    "extrablack": 950,
    "ultrablack": 950,
}


def _key(descriptor: str) -> str:
    return descriptor.replace("-", "").replace("_", "").replace(" ", "").lower()


def numeric_weight(descriptor: str, faces: dict[str, int] | None = None) -> int:
    """CSS weight for a style-name descriptor.

    `faces` is the family's own measured table (descriptor -> usWeightClass),
    read from the installed font. When it carries the descriptor it wins
    outright: the family, not a lookup table, decides what "Black" weighs.
    """
    if faces:
        for name, value in faces.items():
            if _key(name) == _key(descriptor):
                return value
    key = _key(descriptor)
    if key in CANONICAL_WEIGHT_NAMES:
        return CANONICAL_WEIGHT_NAMES[key]
    raise ValueError(
        f"unknown weight descriptor {descriptor!r}; ship the family's faces so "
        "the number can be read from the font instead of guessed"
    )


def available_weights(faces: dict[str, int] | None) -> list[tuple[str, int]]:
    """What the UI may offer, sorted heaviest first.

    q5: the dropdown and the slider are "both filtered by the font's actually
    available values". With no measured faces there is nothing to filter by and
    the caller gets an empty list -- deliberately, so a UI cannot silently fall
    back to offering all nine CSS weights for a family that ships three.
    """
    if not faces:
        return []
    return sorted(faces.items(), key=lambda kv: -kv[1])
