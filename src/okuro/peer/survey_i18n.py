# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.survey_i18n — every recipient-facing string in the People survey,
#   one catalogue per language. THE single translation surface.
# index: LANGUAGES | Catalogue | CATALOGUES | catalogue | reviewed_languages
# AGENT_HEADER_END -->
"""Recipient-facing survey copy, one catalogue per language.

WHY THIS MODULE EXISTS. Until now the words lived in three places:
``survey.py`` owned the axis statements, ``questionnaire.tsx`` owned the SPA
card copy, and ``offline_survey.py`` owned a third hardcoded copy of the same
cards. That was survivable in one language behind a parity test. In four it
is twelve copies of every sentence, and the parity test can only tell you
they disagree — not which one is right.

So the copy moves HERE and both transports render whatever ``survey_form()``
hands them. Adding a language is a new entry in ``CATALOGUES``; adding a
string is one key, and the key-parity test fails every language that lacks
it.

REVIEW STATUS IS A STATE, NOT A COMMENT. ``reviewed=False`` means the strings
have not been checked by a human who speaks the language. These are
psychometric items: NCS-6, REI, SNS and SGL each have published, validated
translations, and a loose rendering of "I decide by gut / instinct" measures
something subtly different from the instrument it is named after. A survey
answered against an unreviewed item still records its axis at
``CONF_DIRECT`` 0.95 — a confident number derived from a sentence nobody
verified. ``peer.survey_guard`` refuses to deliver an unreviewed language
without an explicit override, so the flag has teeth rather than good
intentions.

DECISION ON RECORD (the owner, 2026-08-02): translate everything by LLM and
flag it for review, rather than sourcing the published instrument versions
first. The recommendation was the other way round. This docstring is where
that trade-off stays visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
from okuro.db.engine import okuro_home

# Order is display order wherever a language picker is offered.
LANGUAGES: tuple[str, ...] = ("en", "de", "fr", "it")

DEFAULT_LANGUAGE = "en"


@dataclass(frozen=True)
class Catalogue:
    """Every recipient-facing string for one language.

    ``reviewed`` is False until a human who speaks the language has checked
    the ITEMS — not the buttons. Chrome being slightly stiff costs nothing;
    an item that has drifted changes what the axis measures.
    """

    lang: str
    reviewed: bool
    # Endonym — the language's name IN that language. A picker labelled
    # "German" is for people who already read English; "Deutsch" is for the
    # person who needs the German form.
    label: str = ""
    strings: dict[str, Any] = field(default_factory=dict)
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    note: str | None = None
    # Reviews that were done and FAILED. Distinct from reviewed=False, which
    # only says nobody has looked. A catalogue a native speaker has rejected
    # is not the same risk as one nobody has read, and the delivery refusal
    # should not describe them the same way.
    #
    # Each entry: {"date", "reviewer", "verdict"}.
    review_failures: tuple[dict[str, str], ...] = ()


# ── English — the reference catalogue ────────────────────────────────
#
# Every other language is key-checked against this one. Extracted verbatim
# from the copy that shipped in survey.py, questionnaire.tsx and
# offline_survey.py, so switching to catalogue-driven rendering changed no
# recipient-visible word.
_EN: dict[str, Any] = {
    "ui": {
        "eyebrow": "Communication profile",
        "start": "Start",
        "next": "Next",
        "next_area": "Next area",
        "back": "Back",
        "finish": "Finish",
        "finish_download": "Finish & download",
        "saving": "Saving…",
        "add": "Add",
        "keep_going": "Keep going",
        "im_done": "I'm done — finish",
        "step_of": "Step {n} of {total}",
        "not_answered": "— not answered",
        "type_and_enter": "Type and press Enter…",
        "skip_area": "Not sure — skip this area",
        "no_areas": "No areas? That's fine — tap Next.",
        "nothing_here": "Nothing here? Tap Next.",
        "remove": "Remove {name}",
        "your_name": "Your name",
        "of_count": "{i} of {n}",
        "thanks_title": "Thanks — got it.",
        "thanks_body": "Your answers shape how messages are written for you from here on.",
        "already_title": "Already filled in",
        "already_body": "You've answered this before. Nothing more needed.",
        "download_title": "Done — thank you.",
        "download_body": "A file just downloaded. Send {file} back so it can be imported. You can close this page.",
        "copy_hint": "Can't find the file? Copy the text below and paste it into your reply — it works just as well.",
"copy_button": "Copy",
"copied": "Copied — now paste it into your reply.",
        "unavailable": "This form is unavailable — the server sent no steps.",
    },
    "steps": {
        "intro": {
            "name": "Welcome",
            "heading": "Communication profile",
            "hint": "Three quick questions — about a minute. Your answers tune how things get written for you: the right level of detail, fewer walls of text. There's more you can add afterwards if you want.",
        },
        "you": {
            "name": "About you",
            "heading": "About you",
            "hint": "A few basics so messages land right.",
            "profession_label": "Your profession / role",
            "profession_placeholder": "e.g. Product Designer, CFO, Nurse",
            "function_label": "Which area is that closest to?",
            "seniority_label": "Seniority",
        },
        "areas": {
            "name": "Your areas",
            "heading": "What do you work in?",
            "hint": "Tap the areas you know. Add your own.",
        },
        "depth": {
            "name": "Depth",
            "heading": "How deep are you in {area}?",
            "hint": "Pick the closest fit.",
            "recency_label": "Still current?",
        },
        "weak": {
            "name": "Weak spots",
            "heading": "Anything you'd rather have explained — or skipped?",
            "hint": "Optional. Add topics you're not comfortable with.",
            "placeholder": "e.g. Finance, Legal…",
            "explain": "Explain it to me",
            "skip": "Just skip it",
        },
        "nfc": {
            "name": "Reasoning",
            "heading": "When I write to you, how much reasoning do you want?",
            "hint": "Pick the one that fits most of the time.",
        },
        "decide": {
            "name": "Deciding",
            "heading": "When you decide, what do you lean on?",
            "hint": "Two separate things — rate each. Both can be high.",
            "group_rational": "Careful analysis",
            "group_experiential": "Gut / instinct",
        },
        "format": {
            "name": "Format",
            "heading": "To grasp something new, you'd rather…",
            "hint": "Pick what clicks faster for you.",
        },
        "density": {
            "name": "Density",
            "heading": "On a slide or a page, you'd rather…",
            "hint": "How packed should information be?",
        },
        "ambiguity": {
            "name": "Open questions",
            "heading": "When something's uncertain…",
            "hint": "How do you like it handled?",
        },
        "numeracy": {
            "name": "Numbers",
            "heading": "How comfortable are you with numbers?",
            "hint": "So results arrive as figures or as a plain-language gist.",
        },
        "graph_literacy": {
            "name": "Charts",
            "heading": "Charts and graphs…",
            "hint": "Whether a picture of the data helps or gets in the way.",
        },
        "construal": {
            "name": "Altitude",
            "heading": "You care more about…",
            "hint": "Pick the one you reach for first.",
        },
        "framing": {
            "name": "Framing",
            "heading": "Frame things as…",
            "hint": "Which lands better for you?",
        },
        "more": {
            "name": "A bit more?",
            "heading": "That's the essentials — thank you.",
            "hint": "Everything below is optional and makes the fit sharper.",
        },
        "confidence": {
            "name": "Sure?",
            "heading": "How sure are you about all that?",
            "hint": "Quick gut check on what you just told me.",
            "low": "rough guess",
            "mid": "fairly sure",
            "high": "very sure",
            "untouched": "Drag to answer — or tap Next to skip.",
        },
        "recap": {
            "name": "Recap",
            "heading": "Here's what I got — fix anything.",
            "hint": "Tap Back to change anything. Otherwise, you're done.",
            "you_are": "You're {bits}.",
            "deep_in": "Deep in: {areas}.",
            "want_explained": "Want explained: {areas}.",
            "skip": "Skip: {areas}.",
            "reasoning": "Reasoning",
            "decisions": "Decisions: analysis {a}, gut {b}.",
            "format": "Format",
            "density": "Density",
            "uncertainty": "Uncertainty",
            "numbers": "Numbers",
            "charts": "Charts",
            "focus": "Focus",
            "framing": "Framing",
            "confidence": "Confidence",
            "closest_to": "closest to {fn}",
        },
    },
    "modules": {
        "knowledge": {
            "name": "What you know",
            "blurb": "Your areas and how deep you go — sharpens vocabulary per topic.",
        },
        "register": {
            "name": "How you read",
            "blurb": "Density, uncertainty, how you decide.",
        },
        "quant": {
            "name": "Numbers and charts",
            "blurb": "Whether figures and graphs help you or get in the way.",
        },
        "angle": {
            "name": "Your angle",
            "blurb": "Steps vs strategy, risk vs opportunity.",
        },
    },
    # Axis items. `statement`/`low`/`high` are the scorer-facing labels;
    # `cards` are what the recipient actually taps. `value` is NEVER
    # translated — it is the datum.
    "axes": {
        "need_for_cognition": {
            "statement": "How much reasoning do you want?",
            "low": "just the conclusion",
            "high": "the full reasoning",
            "cards": [
                [0, "Just the bottom line", "Conclusion first, skip the working"],
                [2.5, "Bottom line + key reasons", "The answer, plus why"],
                [5, "Full reasoning and evidence", "Walk me through it"],
            ],
        },
        "rational": {
            "statement": "I decide by careful analysis.",
            "low": "rarely",
            "high": "always",
            "cards": [
                [0, "Rarely", "I don't analyze much"],
                [2.5, "Sometimes", "Depends on the call"],
                [5, "Almost always", "I think it through"],
            ],
        },
        "experiential": {
            "statement": "I decide by gut / instinct.",
            "low": "rarely",
            "high": "always",
            "cards": [
                [0, "Rarely", "I don't go on gut"],
                [2.5, "Sometimes", "Depends on the call"],
                [5, "Almost always", "I trust my instinct"],
            ],
        },
        "visual_verbal": {
            "statement": "To grasp something new, I'd rather…",
            "low": "read it in words",
            "high": "see it as a diagram",
            "cards": [
                [0, "Read it in words", "Sentences, prose"],
                [5, "See it as a diagram", "Picture, chart, sketch"],
            ],
        },
        "density": {
            "statement": "How should information be packed?",
            "low": "one idea at a time",
            "high": "dense & packed",
            "cards": [
                [0, "One idea at a time", "Lots of space, easy to scan"],
                [2.5, "A balanced amount", "Some detail, not crowded"],
                [5, "Dense and packed", "Everything on one view"],
            ],
        },
        "ambiguity": {
            "statement": "Open questions and 'it depends'…",
            "low": "I want a clear answer",
            "high": "are fine with me",
            "cards": [
                [0, "Give me one clear answer", "Just tell me what to do"],
                [2.5, "A lean, with the caveats", "Recommend, but flag the ifs"],
                [5, "Show me the options", '"It depends" is fine'],
            ],
        },
        "numeracy": {
            "statement": "How comfortable are you with numbers?",
            "low": "give me the gist",
            "high": "give me the actual figures",
            "cards": [
                [0, "Give me the gist", '"Most of them" beats "63%"'],
                [2.5, "A number or two", "The headline figure, no table"],
                [5, "Give me the figures", "I want to check the maths"],
            ],
        },
        "graph_literacy": {
            "statement": "Charts and graphs…",
            "low": "I'd rather have it in words",
            "high": "I read them faster than text",
            "cards": [
                [0, "I'd rather have it in words", "Charts slow me down"],
                [2.5, "A simple chart is fine", "Nothing with two axes and a legend"],
                [5, "I read charts faster than text", "Show me the shape of it"],
            ],
        },
        "construal": {
            "statement": "I care more about…",
            "low": "the how / the steps",
            "high": "the why / the big picture",
            "cards": [
                [0, "The how — the steps", "Concrete, near-term, actionable"],
                [5, "The why — the big picture", "Strategic, the point of it all"],
            ],
        },
        "regulatory_focus": {
            "statement": "Frame things as…",
            "low": "the risk to manage",
            "high": "the opportunity to chase",
            "cards": [
                [0, "A risk to manage", "What could go wrong, stay safe"],
                [5, "An opportunity to chase", "What we could gain, go for it"],
            ],
        },
    },
    "knowledge": {
        "domains": [
            "Business", "Strategy", "Design", "Development", "Product",
            "Marketing", "Sales", "Finance", "Operations", "Data & analytics",
            "People & HR", "Legal", "Research", "Communications",
        ],
        "function_other": "Other",
        "seniority": {
            "ic": "IC / hands-on",
            "lead": "Lead / senior",
            "manager": "Manager",
            "director": "Director / VP",
            "executive": "Executive / C-level",
            "founder": "Founder / owner",
        },
        "depth_anchors": [
            "none", "heard of it", "can follow a conversation",
            "can use it with help", "use it independently", "could teach it",
        ],
        "depth_helpers": [
            "Never touched it",
            "Recognize the name",
            "Follow along when others discuss it",
            "Get it done with some support",
            "Handle it on my own",
            "Confident enough to teach it",
        ],
        "detail_prompt": "What do you do with it / like about it?",
        "recency": {
            "current": "Current",
            "rusty": "Rusty",
            "long ago": "Long ago",
        },
    },
}


# ── German ──────────────────────────────────────────────────────────
#
# INFORMAL "du", rewritten 2026-08-03 — same pass as Italian, same two
# faults. "Sie" plus formal imperatives is the register of an authority
# writing to a citizen; this is a two-minute questionnaire from someone the
# reader knows. And translating the English SENTENCE rather than the
# QUESTION produced calques: "Flughöhe" for Altitude, "Rahmung" for Framing,
# "Führen Sie mich durch" for "Walk me through it" — each defensible
# word-for-word, none what a German speaker would actually say.
#
# Swiss note: standard written German throughout. Swiss German is spoken,
# not written, so de-CH readers read this — but "ss" for "ß" is used, which
# is the Swiss convention and harmless everywhere else.
_DE: dict[str, Any] = {
    "ui": {
        "eyebrow": "Kommunikationsprofil",
        "start": "Los geht's",
        "next": "Weiter",
        "next_area": "Nächster Bereich",
        "back": "Zurück",
        "finish": "Fertig",
        "finish_download": "Fertig und runterladen",
        "saving": "Speichere…",
        "add": "Hinzufügen",
        "keep_going": "Weiter geht's",
        "im_done": "Reicht mir so",
        "step_of": "Schritt {n} von {total}",
        "not_answered": "— nicht beantwortet",
        "type_and_enter": "Tippen und Enter drücken…",
        "skip_area": "Weiss nicht — überspring den",
        "no_areas": "Keine dabei? Kein Problem — einfach weiter.",
        "nothing_here": "Nichts dabei? Einfach weiter.",
        "remove": "{name} entfernen",
        "your_name": "Wie heisst du?",
        "of_count": "{i} von {n}",
        "thanks_title": "Danke, ist angekommen.",
        "thanks_body": "Ab jetzt bestimmen deine Antworten, wie ich dir schreibe.",
        "already_title": "Hast du schon ausgefüllt",
        "already_body": "Du hast das schon beantwortet. Mehr braucht's nicht.",
        "download_title": "Fertig, danke dir.",
        "download_body": "Eine Datei ist rausgegangen. Schick mir {file} zurück, dann lese ich sie ein. Die Seite kannst du zumachen.",
        "copy_hint": "Datei nicht zu finden? Kopier den Text unten und häng ihn in deine Antwort — geht genauso.",
        "copy_button": "Kopieren",
        "copied": "Kopiert — jetzt in die Antwort einfügen.",
        "unavailable": "Das Formular geht gerade nicht — der Server hat nichts geschickt.",
    },
    "steps": {
        "intro": {
            "name": "Hallo",
            "heading": "Kommunikationsprofil",
            "hint": "Drei kurze Fragen, keine Minute. Deine Antworten bestimmen, wie ich dir schreibe: der richtige Detailgrad, weniger Textwüsten. Wenn du magst, kannst du danach noch mehr ergänzen.",
        },
        "you": {
            "name": "Zu dir",
            "heading": "Zwei Sachen zu dir",
            "hint": "Nur zur Einordnung.",
            "profession_label": "Was machst du beruflich?",
            "profession_placeholder": "z. B. Produktdesignerin, Finanzchef, Pfleger",
            "function_label": "Wo passt das am ehesten rein?",
            "seniority_label": "Was für eine Rolle hast du?",
        },
        "areas": {
            "name": "Deine Themen",
            "heading": "Womit beschäftigst du dich?",
            "hint": "Tipp an, was du kennst. Eigenes kannst du dazuschreiben.",
        },
        "depth": {
            "name": "Wie tief",
            "heading": "Wie gut kennst du dich mit {area} aus?",
            "hint": "Nimm, was am ehesten passt.",
            "recency_label": "Machst du das noch?",
        },
        "weak": {
            "name": "Schwache Stellen",
            "heading": "Gibt's was, das ich dir lieber erklären soll — oder weglassen?",
            "hint": "Wenn du magst. Themen, bei denen du dich unwohl fühlst.",
            "placeholder": "z. B. Buchhaltung, Verträge…",
            "explain": "Erklär's mir",
            "skip": "Lass es weg",
        },
        "nfc": {
            "name": "Erklärungen",
            "heading": "Wenn ich dir schreibe — wie viel Erklärung willst du?",
            "hint": "Denk dran, wie es meistens ist.",
        },
        "decide": {
            "name": "Wie du entscheidest",
            "heading": "Worauf gehst du, wenn du entscheidest?",
            "hint": "Zwei verschiedene Sachen — sag zu beiden was. Beide dürfen hoch sein.",
            "group_rational": "Ich denk's durch",
            "group_experiential": "Ich geh nach Bauch",
        },
        "format": {
            "name": "Wie du's liest",
            "heading": "Wenn du was Neues kapieren willst, lieber…",
            "hint": "Was dir leichter fällt.",
        },
        "density": {
            "name": "Wie viel auf einmal",
            "heading": "Auf einer Folie oder Seite lieber…",
            "hint": "Wie viel auf einmal ist dir recht?",
        },
        "ambiguity": {
            "name": "Wenn's unklar ist",
            "heading": "Wenn etwas noch nicht klar ist…",
            "hint": "Wie ist es dir am liebsten?",
        },
        "numeracy": {
            "name": "Zahlen",
            "heading": "Wie gut kommst du mit Zahlen klar?",
            "hint": "Damit ich dir Zahlen gebe oder es dir erzähle.",
        },
        "graph_literacy": {
            "name": "Diagramme",
            "heading": "Und Diagramme?",
            "hint": "Helfen sie dir oder kosten sie dich Zeit?",
        },
        "construal": {
            "name": "Worauf du schaust",
            "heading": "Was interessiert dich mehr…",
            "hint": "Worauf du zuerst schaust.",
        },
        "framing": {
            "name": "Wie ich's sage",
            "heading": "Wie soll ich dir Sachen hinlegen…",
            "hint": "Was überzeugt dich eher?",
        },
        "more": {
            "name": "Noch was?",
            "heading": "Das Wichtigste steht — danke dir.",
            "hint": "Der Rest ist freiwillig und macht's nur noch genauer.",
        },
        "confidence": {
            "name": "Wie sicher",
            "heading": "Wie sicher bist du dir bei dem, was du gesagt hast?",
            "hint": "So aus dem Bauch raus.",
            "low": "geraten",
            "mid": "ziemlich sicher",
            "high": "ganz sicher",
            "untouched": "Zum Antworten schieben — oder weiter und überspringen.",
        },
        "recap": {
            "name": "Zusammenfassung",
            "heading": "Das hab ich verstanden — korrigier ruhig.",
            "hint": "Mit Zurück kannst du alles ändern. Sonst bist du durch.",
            "you_are": "Du bist {bits}.",
            "deep_in": "Kennst dich aus mit: {areas}.",
            "want_explained": "Soll ich erklären: {areas}.",
            "skip": "Lass ich weg: {areas}.",
            "reasoning": "Erklärungen",
            "decisions": "Entscheidungen: durchdenken {a}, Bauch {b}.",
            "format": "Wie du liest",
            "density": "Wie viel auf einmal",
            "uncertainty": "Wenn's unklar ist",
            "numbers": "Zahlen",
            "charts": "Diagramme",
            "focus": "Worauf du schaust",
            "framing": "Wie ich's sage",
            "confidence": "Sicherheit",
            "closest_to": "ungefähr {fn}",
        },
    },
    "modules": {
        "knowledge": {
            "name": "Was du kannst",
            "blurb": "Deine Themen und wie tief du drin steckst — damit ich die richtigen Wörter nehme.",
        },
        "register": {
            "name": "Wie du liest",
            "blurb": "Wie viel auf einmal, was bei Unklarem passiert, wie du entscheidest.",
        },
        "quant": {
            "name": "Zahlen und Diagramme",
            "blurb": "Ob Zahlen und Grafiken dir helfen oder im Weg sind.",
        },
        "angle": {
            "name": "Von welcher Seite",
            "blurb": "Schritte oder Strategie, Risiko oder Chance.",
        },
    },
    "axes": {
        "need_for_cognition": {
            "statement": "Wie viel Erklärung willst du?",
            "low": "nur das Ergebnis",
            "high": "den ganzen Gedankengang",
            "cards": [
                [0, "Nur das Ergebnis", "Sag mir, was rauskommt, den Rest lass weg"],
                [2.5, "Ergebnis und warum", "Die Antwort, plus die wichtigsten Gründe"],
                [5, "Den ganzen Gedankengang", "Erklär mir, wie du drauf kommst"],
            ],
        },
        "rational": {
            "statement": "Ich entscheide, nachdem ich's durchdacht habe.",
            "low": "fast nie",
            "high": "fast immer",
            "cards": [
                [0, "Fast nie", "Ich denk da nicht lange drüber nach"],
                [2.5, "Kommt drauf an", "Kommt drauf an, was ansteht"],
                [5, "Fast immer", "Ich denk's ordentlich durch"],
            ],
        },
        "experiential": {
            "statement": "Ich entscheide aus dem Bauch.",
            "low": "fast nie",
            "high": "fast immer",
            "cards": [
                [0, "Fast nie", "Auf mein Bauchgefühl geh ich nicht"],
                [2.5, "Kommt drauf an", "Kommt drauf an, was ansteht"],
                [5, "Fast immer", "Auf meinen Bauch ist Verlass"],
            ],
        },
        "visual_verbal": {
            "statement": "Wenn ich was Neues kapieren will, lieber…",
            "low": "lesen",
            "high": "aufgezeichnet sehen",
            "cards": [
                [0, "Lesen", "Geschrieben, mit Worten"],
                [5, "Aufgezeichnet sehen", "Eine Skizze, ein Schaubild, ein Bild"],
            ],
        },
        "density": {
            "statement": "Wie viel auf einmal ist dir recht?",
            "low": "eins nach dem anderen",
            "high": "alles auf einmal",
            "cards": [
                [0, "Eins nach dem anderen", "Mit Luft drum rum, leicht zu überfliegen"],
                [2.5, "Was dazwischen", "Ein bisschen Detail, nicht überladen"],
                [5, "Alles auf einmal", "Pack mir alles vor die Augen"],
            ],
        },
        "ambiguity": {
            "statement": "Sachen, die noch offen sind, und «kommt drauf an»…",
            "low": "ich will eine klare Ansage",
            "high": "stören mich nicht",
            "cards": [
                [0, "Sag mir eine Sache", "Sag mir einfach, was zu tun ist"],
                [2.5, "Sag, wie du's siehst, mit dem Wenn und Aber", "Empfiehl was, aber nenn die Haken"],
                [5, "Zeig mir die Wege", "«Kommt drauf an» ist für mich okay"],
            ],
        },
        "numeracy": {
            "statement": "Wie gut kommst du mit Zahlen klar?",
            "low": "erzähl's mir",
            "high": "gib mir die Zahlen",
            "cards": [
                [0, "Erzähl's mir", "«Fast alle» sagt mir mehr als «63 %»"],
                [2.5, "Ein, zwei Zahlen", "Die Zahl, auf die es ankommt, ohne Tabelle"],
                [5, "Gib mir die Zahlen", "Ich will selber nachrechnen"],
            ],
        },
        "graph_literacy": {
            "statement": "Diagramme…",
            "low": "lieber in Worten",
            "high": "lese ich sofort",
            "cards": [
                [0, "Lieber in Worten", "Diagramme bremsen mich"],
                [2.5, "Ein einfaches ist okay", "Nichts mit zwei Achsen und Legende"],
                [5, "Lese ich sofort", "Zeig mir, welche Form die Sache hat"],
            ],
        },
        "construal": {
            "statement": "Mich interessiert mehr…",
            "low": "wie man's macht",
            "high": "warum wir's machen",
            "cards": [
                [0, "Wie man's macht", "Die Schritte, konkret, für jetzt"],
                [5, "Warum wir's machen", "Der Sinn, wo das hinführt"],
            ],
        },
        "regulatory_focus": {
            "statement": "Wie soll ich dir Sachen hinlegen…",
            "low": "als Risiko, auf das man aufpasst",
            "high": "als Chance, die man packt",
            "cards": [
                [0, "Als Risiko, auf das man aufpasst", "Was schiefgehen kann, lieber vorsichtig"],
                [5, "Als Chance, die man packt", "Was dabei rausspringt, probieren wir's"],
            ],
        },
    },
    "knowledge": {
        "domains": [
            "Business", "Strategie", "Design", "Entwicklung", "Produkt",
            "Marketing", "Verkauf", "Buchhaltung", "Betrieb", "Daten",
            "Personal", "Verträge und Recht", "Forschung", "Kommunikation",
        ],
        "function_other": "Was anderes",
        "seniority": {
            "ic": "Ich mach's selber",
            "lead": "Senior / erfahren",
            "manager": "Teamleitung",
            "director": "Bereichsleitung",
            "executive": "Geschäftsleitung",
            "founder": "Gründerin / Inhaber",
        },
        "depth_anchors": [
            "gar nicht", "schon mal gehört", "ich komm im Gespräch mit",
            "mit Hilfe krieg ich's hin", "mach ich allein", "könnt ich beibringen",
        ],
        "depth_helpers": [
            "Noch nie damit zu tun gehabt",
            "Weiss, was es ist, mehr nicht",
            "Wenn andere drüber reden, komm ich mit",
            "Mit etwas Hilfe krieg ich's hin",
            "Mach ich allein",
            "Könnt ich jemandem beibringen",
        ],
        "detail_prompt": "Was machst du damit / was gefällt dir daran?",
        "recency": {
            "current": "Mach ich",
            "rusty": "Etwas eingerostet",
            "long ago": "Lang her",
        },
    },
}


# ── French ──────────────────────────────────────────────────────────
#
# INFORMAL "tu", rewritten 2026-08-03 — same pass as Italian and German.
# "vous" plus the formal register turns a two-minute questionnaire into an
# administrative form, and translating the English SENTENCE rather than the
# QUESTION gave calques: "Altitude" left untranslated, "Cadrage" for
# Framing, "Expliquez-moi tout" for "Walk me through it".
_FR: dict[str, Any] = {
    "ui": {
        "eyebrow": "Profil de communication",
        "start": "C'est parti",
        "next": "Suivant",
        "next_area": "Domaine suivant",
        "back": "Retour",
        "finish": "J'ai fini",
        "finish_download": "Terminer et télécharger",
        "saving": "J'enregistre…",
        "add": "Ajouter",
        "keep_going": "Je continue",
        "im_done": "Ça me suffit",
        "step_of": "Étape {n} sur {total}",
        "not_answered": "— pas répondu",
        "type_and_enter": "Écris et appuie sur Entrée…",
        "skip_area": "Je ne sais pas — passe celui-là",
        "no_areas": "Aucun ? Pas grave — continue.",
        "nothing_here": "Rien à signaler ? Continue.",
        "remove": "Enlever {name}",
        "your_name": "Tu t'appelles comment ?",
        "of_count": "{i} sur {n}",
        "thanks_title": "Merci, c'est bien arrivé.",
        "thanks_body": "À partir de maintenant, tes réponses décident de la façon dont je t'écris.",
        "already_title": "Tu l'as déjà rempli",
        "already_body": "Tu as déjà répondu, rien de plus à faire.",
        "download_title": "C'est fait, merci.",
        "download_body": "Un fichier est parti. Renvoie-moi {file} pour que je le charge. Tu peux fermer la page.",
        "copy_hint": "Tu ne trouves pas le fichier ? Copie le texte ci-dessous et colle-le dans ta réponse — ça marche pareil.",
        "copy_button": "Copier",
        "copied": "Copié — colle-le dans ta réponse.",
        "unavailable": "Le formulaire ne marche pas — le serveur n'a rien envoyé.",
    },
    "steps": {
        "intro": {
            "name": "Salut",
            "heading": "Profil de communication",
            "hint": "Trois questions rapides, à peine une minute. Tes réponses décident de la façon dont je t'écris : le bon niveau de détail, moins de pavés. Si tu veux, tu pourras en ajouter après.",
        },
        "you": {
            "name": "Toi",
            "heading": "Deux mots sur toi",
            "hint": "Juste pour situer.",
            "profession_label": "Tu fais quoi comme métier ?",
            "profession_placeholder": "p. ex. designer, directrice financière, infirmier",
            "function_label": "Ça se rapproche de quoi ?",
            "seniority_label": "Tu as quel rôle ?",
        },
        "areas": {
            "name": "Tes domaines",
            "heading": "Tu travailles dans quoi ?",
            "hint": "Touche ce que tu connais. Tu peux en ajouter.",
        },
        "depth": {
            "name": "Jusqu'où",
            "heading": "Tu connais {area} jusqu'où ?",
            "hint": "Prends ce qui te ressemble le plus.",
            "recency_label": "Tu le fais encore ?",
        },
        "weak": {
            "name": "Points faibles",
            "heading": "Il y a des sujets que tu préfères que je t'explique — ou que je saute ?",
            "hint": "Si tu veux. Les sujets où tu n'es pas à l'aise.",
            "placeholder": "p. ex. compta, contrats…",
            "explain": "Explique-moi",
            "skip": "Saute-le",
        },
        "nfc": {
            "name": "Explications",
            "heading": "Quand je t'écris, tu veux combien d'explications ?",
            "hint": "Pense à comment ça se passe d'habitude.",
        },
        "decide": {
            "name": "Comment tu décides",
            "heading": "Quand tu décides, tu t'appuies sur quoi ?",
            "hint": "Deux choses différentes — réponds aux deux. Les deux peuvent être hautes.",
            "group_rational": "J'y réfléchis",
            "group_experiential": "Je le sens",
        },
        "format": {
            "name": "Comment tu lis",
            "heading": "Pour comprendre un truc nouveau, tu préfères…",
            "hint": "Ce qui te vient le plus facilement.",
        },
        "density": {
            "name": "Combien à la fois",
            "heading": "Sur une slide ou une page, tu préfères…",
            "hint": "Combien de choses en même temps ça te va ?",
        },
        "ambiguity": {
            "name": "Quand c'est flou",
            "heading": "Quand un truc n'est pas encore clair…",
            "hint": "Tu préfères que je fasse comment ?",
        },
        "numeracy": {
            "name": "Les chiffres",
            "heading": "Tu te débrouilles comment avec les chiffres ?",
            "hint": "Pour savoir si je te donne les chiffres ou si je te raconte.",
        },
        "graph_literacy": {
            "name": "Les graphiques",
            "heading": "Et les graphiques ?",
            "hint": "Ils t'aident ou ils te font perdre du temps ?",
        },
        "construal": {
            "name": "Ce que tu regardes",
            "heading": "Ce qui t'intéresse le plus…",
            "hint": "Ce que tu regardes en premier.",
        },
        "framing": {
            "name": "Comment je te le dis",
            "heading": "Tu préfères que je te présente ça comme…",
            "hint": "Ce qui te parle le plus.",
        },
        "more": {
            "name": "Encore deux trucs ?",
            "heading": "L'essentiel y est — merci à toi.",
            "hint": "Le reste est facultatif, ça sert juste à affiner.",
        },
        "confidence": {
            "name": "Tu es sûr ?",
            "heading": "Tu es sûr de ce que tu viens de dire ?",
            "hint": "Au feeling.",
            "low": "au pif",
            "mid": "plutôt sûr",
            "high": "certain",
            "untouched": "Fais glisser pour répondre — ou continue et passe.",
        },
        "recap": {
            "name": "Récap",
            "heading": "Voilà ce que j'ai compris — corrige si besoin.",
            "hint": "Retour si tu veux changer un truc. Sinon c'est bon.",
            "you_are": "Tu es {bits}.",
            "deep_in": "Tu connais : {areas}.",
            "want_explained": "À t'expliquer : {areas}.",
            "skip": "À sauter : {areas}.",
            "reasoning": "Explications",
            "decisions": "Décisions : réflexion {a}, feeling {b}.",
            "format": "Comment tu lis",
            "density": "Combien à la fois",
            "uncertainty": "Quand c'est flou",
            "numbers": "Les chiffres",
            "charts": "Les graphiques",
            "focus": "Ce que tu regardes",
            "framing": "Comment te le dire",
            "confidence": "Sûr de toi",
            "closest_to": "à peu près {fn}",
        },
    },
    "modules": {
        "knowledge": {
            "name": "Ce que tu connais",
            "blurb": "Tes domaines et jusqu'où tu vas — pour que j'emploie les bons mots sur chaque sujet.",
        },
        "register": {
            "name": "Comment tu lis",
            "blurb": "Combien à la fois, quoi faire quand c'est flou, comment tu décides.",
        },
        "quant": {
            "name": "Chiffres et graphiques",
            "blurb": "Si les chiffres et les graphiques t'aident ou te gênent.",
        },
        "angle": {
            "name": "De quel côté tu regardes",
            "blurb": "Les étapes ou la stratégie, le risque ou l'occasion.",
        },
    },
    "axes": {
        "need_for_cognition": {
            "statement": "Tu veux combien d'explications ?",
            "low": "juste le résultat",
            "high": "tout le raisonnement",
            "cards": [
                [0, "Juste le résultat", "Dis-moi ce que ça donne, laisse le reste"],
                [2.5, "Le résultat et pourquoi", "La réponse, plus les raisons principales"],
                [5, "Tout le raisonnement", "Explique-moi comment tu y arrives"],
            ],
        },
        "rational": {
            "statement": "Je décide après y avoir réfléchi.",
            "low": "presque jamais",
            "high": "presque toujours",
            "cards": [
                [0, "Presque jamais", "Je n'y réfléchis pas longtemps"],
                [2.5, "Ça dépend", "Ça dépend de ce qu'il y a à décider"],
                [5, "Presque toujours", "J'y réfléchis pour de bon"],
            ],
        },
        "experiential": {
            "statement": "Je décide au feeling.",
            "low": "presque jamais",
            "high": "presque toujours",
            "cards": [
                [0, "Presque jamais", "Je ne marche pas au feeling"],
                [2.5, "Ça dépend", "Ça dépend de ce qu'il y a à décider"],
                [5, "Presque toujours", "Je me fie à mon instinct"],
            ],
        },
        "visual_verbal": {
            "statement": "Pour comprendre un truc nouveau, je préfère…",
            "low": "le lire",
            "high": "le voir dessiné",
            "cards": [
                [0, "Le lire", "Écrit, avec des mots"],
                [5, "Le voir dessiné", "Un schéma, un dessin, une image"],
            ],
        },
        "density": {
            "statement": "Combien de choses en même temps ça te va ?",
            "low": "une chose à la fois",
            "high": "tout d'un coup",
            "cards": [
                [0, "Une chose à la fois", "De l'air autour, facile à parcourir"],
                [2.5, "Entre les deux", "Un peu de détail, sans surcharger"],
                [5, "Tout d'un coup", "Mets-moi tout sous les yeux"],
            ],
        },
        "ambiguity": {
            "statement": "Les trucs pas encore tranchés et les « ça dépend »…",
            "low": "je veux une réponse nette",
            "high": "ça ne me dérange pas",
            "cards": [
                [0, "Dis-moi une seule chose", "Dis-moi juste quoi faire"],
                [2.5, "Dis-moi ce que tu en penses, avec les si", "Conseille, mais dis-moi les pièges"],
                [5, "Montre-moi les options", "« Ça dépend » me va très bien"],
            ],
        },
        "numeracy": {
            "statement": "Tu te débrouilles comment avec les chiffres ?",
            "low": "raconte-moi",
            "high": "donne-moi les chiffres",
            "cards": [
                [0, "Raconte-moi", "« Presque tous » me parle plus que « 63 % »"],
                [2.5, "Un ou deux chiffres", "Le chiffre qui compte, sans tableau"],
                [5, "Donne-moi les chiffres", "Je veux refaire le calcul"],
            ],
        },
        "graph_literacy": {
            "statement": "Les graphiques…",
            "low": "plutôt en mots",
            "high": "je les lis tout de suite",
            "cards": [
                [0, "Plutôt en mots", "Les graphiques me ralentissent"],
                [2.5, "Un simple, ça va", "Rien avec deux axes et une légende"],
                [5, "Je les lis tout de suite", "Montre-moi la forme que ça a"],
            ],
        },
        "construal": {
            "statement": "Ce qui m'intéresse le plus…",
            "low": "comment on fait",
            "high": "pourquoi on le fait",
            "cards": [
                [0, "Comment on fait", "Les étapes, concret, pour maintenant"],
                [5, "Pourquoi on le fait", "Le sens, où ça nous mène"],
            ],
        },
        "regulatory_focus": {
            "statement": "Tu préfères que je te présente ça comme…",
            "low": "un risque à surveiller",
            "high": "une occasion à saisir",
            "cards": [
                [0, "Un risque à surveiller", "Ce qui peut mal tourner, on y va doucement"],
                [5, "Une occasion à saisir", "Ce qu'on peut y gagner, on tente"],
            ],
        },
    },
    "knowledge": {
        "domains": [
            "Business", "Stratégie", "Design", "Développement", "Produit",
            "Marketing", "Vente", "Compta", "Opérations", "Données",
            "Personnel", "Contrats et juridique", "Recherche", "Communication",
        ],
        "function_other": "Autre chose",
        "seniority": {
            "ic": "Je le fais moi-même",
            "lead": "Senior / référent",
            "manager": "Responsable d'équipe",
            "director": "Direction",
            "executive": "Direction générale",
            "founder": "Fondateur / patronne",
        },
        "depth_anchors": [
            "pas du tout", "j'en ai entendu parler", "je suis la conversation",
            "je le fais si on m'aide", "je le fais seul", "je saurais l'expliquer",
        ],
        "depth_helpers": [
            "Jamais touché",
            "Je sais ce que c'est, pas plus",
            "Si on en parle, je suis",
            "J'y arrive avec un coup de main",
            "Je me débrouille seul",
            "Je pourrais l'expliquer à quelqu'un",
        ],
        "detail_prompt": "Tu en fais quoi exactement / qu'est-ce qui te plaît ?",
        "recency": {
            "current": "En ce moment",
            "rusty": "Un peu rouillé",
            "long ago": "Il y a longtemps",
        },
    },
}


# ── Italian ─────────────────────────────────────────────────────────
#
# INFORMAL "tu", rewritten 2026-08-03 after a native reader said the first
# pass felt wooden. Two things made it so, and both are worth remembering
# for the next language:
#
#   * "Lei" plus formal imperatives ("Scelga", "Prema", "Mi accompagni")
#     is the register of an official form. It is grammatically perfect and
#     socially wrong for a two-minute questionnaire sent by someone you
#     know — it makes the reader an applicant rather than a guest.
#   * Translating the ENGLISH SENTENCE rather than the QUESTION produces
#     calques: "Quota" for Altitude, "Inquadramento" for Framing, "Mi
#     accompagni nel percorso" for "Walk me through it". Each is defensible
#     word-for-word and none is what an Italian would say.
#
# The rule applied here: write what a person would actually ask out loud,
# then check it still measures the same thing.
_IT: dict[str, Any] = {
    "ui": {
        "eyebrow": "Profilo di comunicazione",
        "start": "Iniziamo",
        "next": "Avanti",
        "next_area": "Prossima area",
        "back": "Indietro",
        "finish": "Ho finito",
        "finish_download": "Finisci e scarica",
        "saving": "Sto salvando…",
        "add": "Aggiungi",
        "keep_going": "Vai avanti",
        "im_done": "Ho finito così",
        "step_of": "Passo {n} di {total}",
        "not_answered": "— non hai risposto",
        "type_and_enter": "Scrivi e premi Invio…",
        "skip_area": "Non saprei — salta questa",
        "no_areas": "Nessuna? Va benissimo — vai avanti.",
        "nothing_here": "Niente? Vai pure avanti.",
        "remove": "Togli {name}",
        "your_name": "Come ti chiami",
        "of_count": "{i} di {n}",
        "thanks_title": "Grazie, ricevuto.",
        "thanks_body": "Da adesso le tue risposte decidono come ti scrivo.",
        "already_title": "L'hai già compilato",
        "already_body": "Hai già risposto, non serve altro.",
        "download_title": "Fatto, grazie.",
        "download_body": "È partito un file. Rimandami {file} così lo carico. Puoi chiudere la pagina.",
        "copy_hint": "Non trovi il file? Copia il testo qui sotto e incollalo nella risposta — va bene uguale.",
        "copy_button": "Copia",
        "copied": "Copiato — ora incollalo nella risposta.",
        "unavailable": "Questo modulo non funziona — il server non ha mandato niente.",
    },
    "steps": {
        "intro": {
            "name": "Ciao",
            "heading": "Profilo di comunicazione",
            "hint": "Tre domande veloci, un minuto scarso. Le tue risposte decidono come ti scrivo: il livello di dettaglio giusto, meno muri di testo. Se ti va, dopo puoi aggiungere altro.",
        },
        "you": {
            "name": "Chi sei",
            "heading": "Due cose su di te",
            "hint": "Giusto per inquadrarti.",
            "profession_label": "Che lavoro fai?",
            "profession_placeholder": "es. designer, direttore finanziario, infermiere",
            "function_label": "A quale ambito somiglia di più?",
            "seniority_label": "Che ruolo hai?",
        },
        "areas": {
            "name": "I tuoi campi",
            "heading": "Di cosa ti occupi?",
            "hint": "Tocca quelli che conosci. Puoi aggiungerne altri.",
        },
        "depth": {
            "name": "Quanto a fondo",
            "heading": "Quanto ne sai di {area}?",
            "hint": "Scegli quello che ti somiglia di più.",
            "recency_label": "Ci lavori ancora?",
        },
        "weak": {
            "name": "Punti deboli",
            "heading": "C'è qualcosa che preferisci ti spieghi — o che salti?",
            "hint": "Se ti va. Argomenti su cui non sei a tuo agio.",
            "placeholder": "es. contabilità, contratti…",
            "explain": "Spiegamelo",
            "skip": "Saltalo pure",
        },
        "nfc": {
            "name": "Spiegazioni",
            "heading": "Quando ti scrivo, quanta spiegazione vuoi?",
            "hint": "Pensa a come va di solito.",
        },
        "decide": {
            "name": "Come decidi",
            "heading": "Quando decidi, su cosa ti basi?",
            "hint": "Sono due cose diverse — rispondi a tutte e due. Possono essere alte entrambe.",
            "group_rational": "Ci ragiono su",
            "group_experiential": "Vado a istinto",
        },
        "format": {
            "name": "Come lo leggi",
            "heading": "Per capire una cosa nuova, preferisci…",
            "hint": "Quello che ti viene più naturale.",
        },
        "density": {
            "name": "Quanto denso",
            "heading": "In una slide o in una pagina, preferisci…",
            "hint": "Quante cose insieme reggi volentieri.",
        },
        "ambiguity": {
            "name": "Quando non è chiaro",
            "heading": "Quando una cosa non è ancora chiara…",
            "hint": "Come preferisci che te la giri?",
        },
        "numeracy": {
            "name": "Numeri",
            "heading": "Quanto te la cavi con i numeri?",
            "hint": "Così ti do le cifre o te la racconto a parole.",
        },
        "graph_literacy": {
            "name": "Grafici",
            "heading": "E i grafici?",
            "hint": "Ti aiutano o ti fanno perdere tempo?",
        },
        "construal": {
            "name": "Cosa guardi",
            "heading": "Ti interessa più…",
            "hint": "Quello che guardi per primo.",
        },
        "framing": {
            "name": "Come te lo dico",
            "heading": "Preferisci che te la metta giù come…",
            "hint": "Cosa ti convince di più?",
        },
        "more": {
            "name": "Ancora due cose?",
            "heading": "L'essenziale c'è — grazie.",
            "hint": "Il resto è facoltativo, serve solo a farlo funzionare meglio.",
        },
        "confidence": {
            "name": "Quanto sei sicuro",
            "heading": "Quanto sei sicuro di quello che hai detto?",
            "hint": "Così, a sensazione.",
            "low": "tirato a indovinare",
            "mid": "abbastanza sicuro",
            "high": "sicurissimo",
            "untouched": "Trascina per rispondere — o vai avanti e salta.",
        },
        "recap": {
            "name": "Riepilogo",
            "heading": "Ecco cosa ho capito — correggi pure.",
            "hint": "Torna indietro se vuoi cambiare qualcosa. Altrimenti hai finito.",
            "you_are": "Sei {bits}.",
            "deep_in": "Ne sai di: {areas}.",
            "want_explained": "Da spiegare: {areas}.",
            "skip": "Da saltare: {areas}.",
            "reasoning": "Spiegazioni",
            "decisions": "Decisioni: ragionamento {a}, istinto {b}.",
            "format": "Come leggi",
            "density": "Quanto denso",
            "uncertainty": "Cose non chiare",
            "numbers": "Numeri",
            "charts": "Grafici",
            "focus": "Cosa guardi",
            "framing": "Come dirtelo",
            "confidence": "Sicurezza",
            "closest_to": "più o meno {fn}",
        },
    },
    "modules": {
        "knowledge": {
            "name": "Quello che sai",
            "blurb": "I tuoi campi e quanto ci sei dentro — così uso le parole giuste su ogni argomento.",
        },
        "register": {
            "name": "Come leggi",
            "blurb": "Quanto denso, cosa fare quando non è chiaro, come decidi.",
        },
        "quant": {
            "name": "Numeri e grafici",
            "blurb": "Se cifre e grafici ti aiutano o ti intralciano.",
        },
        "angle": {
            "name": "Da che parte guardi",
            "blurb": "Passaggi o strategia, rischio o occasione.",
        },
    },
    "axes": {
        "need_for_cognition": {
            "statement": "Quanta spiegazione vuoi?",
            "low": "solo il risultato",
            "high": "tutto il ragionamento",
            "cards": [
                [0, "Solo il risultato", "Dimmi com'è finita, il resto lascialo stare"],
                [2.5, "Il risultato e il perché", "La risposta, più i motivi principali"],
                [5, "Tutto il ragionamento", "Spiegami come ci sei arrivato"],
            ],
        },
        "rational": {
            "statement": "Decido dopo averci ragionato su.",
            "low": "quasi mai",
            "high": "quasi sempre",
            "cards": [
                [0, "Quasi mai", "Non sto lì a ragionarci"],
                [2.5, "Dipende", "Dipende da cosa devo decidere"],
                [5, "Quasi sempre", "Ci ragiono su per bene"],
            ],
        },
        "experiential": {
            "statement": "Decido di pancia.",
            "low": "quasi mai",
            "high": "quasi sempre",
            "cards": [
                [0, "Quasi mai", "Non vado a sensazione"],
                [2.5, "Dipende", "Dipende da cosa devo decidere"],
                [5, "Quasi sempre", "Mi fido della pancia"],
            ],
        },
        "visual_verbal": {
            "statement": "Per capire una cosa nuova, preferisco…",
            "low": "leggerla",
            "high": "vederla disegnata",
            "cards": [
                [0, "Leggerla", "Scritta, con le parole"],
                [5, "Vederla disegnata", "Uno schema, un disegno, una figura"],
            ],
        },
        "density": {
            "statement": "Quante cose insieme reggi volentieri?",
            "low": "una cosa alla volta",
            "high": "tutto insieme",
            "cards": [
                [0, "Una cosa alla volta", "Con aria intorno, facile da scorrere"],
                [2.5, "Una via di mezzo", "Qualche dettaglio, senza esagerare"],
                [5, "Tutto insieme", "Mettimelo tutto sotto gli occhi"],
            ],
        },
        "ambiguity": {
            "statement": "Le cose non ancora chiare e i «dipende»…",
            "low": "voglio una risposta secca",
            "high": "non mi danno fastidio",
            "cards": [
                [0, "Dimmi una cosa sola", "Dimmi che si fa e basta"],
                [2.5, "Dimmi come la vedi, coi se e i ma", "Consigliami, ma dimmi anche i rischi"],
                [5, "Fammi vedere le strade", "«Dipende» per me va bene"],
            ],
        },
        "numeracy": {
            "statement": "Quanto te la cavi con i numeri?",
            "low": "raccontamela",
            "high": "dammi le cifre",
            "cards": [
                [0, "Raccontamela", "«Quasi tutti» mi dice più di «63%»"],
                [2.5, "Un paio di numeri", "Il numero che conta, senza tabelle"],
                [5, "Dammi le cifre", "Voglio rifare io i conti"],
            ],
        },
        "graph_literacy": {
            "statement": "I grafici…",
            "low": "meglio a parole",
            "high": "li leggo al volo",
            "cards": [
                [0, "Meglio a parole", "I grafici mi rallentano"],
                [2.5, "Uno semplice va bene", "Niente robe con due assi e la legenda"],
                [5, "Li leggo al volo", "Fammi vedere che forma ha"],
            ],
        },
        "construal": {
            "statement": "Mi interessa più…",
            "low": "come si fa",
            "high": "perché lo facciamo",
            "cards": [
                [0, "Come si fa", "I passaggi, concreti, da fare adesso"],
                [5, "Perché lo facciamo", "Il senso, dove stiamo andando"],
            ],
        },
        "regulatory_focus": {
            "statement": "Preferisco che me la metta giù come…",
            "low": "un rischio da tenere d'occhio",
            "high": "un'occasione da prendere",
            "cards": [
                [0, "Un rischio da tenere d'occhio", "Cosa può andare storto, andiamoci piano"],
                [5, "Un'occasione da prendere", "Cosa ci guadagniamo, proviamoci"],
            ],
        },
    },
    "knowledge": {
        "domains": [
            "Business", "Strategia", "Design", "Sviluppo", "Prodotto",
            "Marketing", "Vendite", "Contabilità", "Operazioni", "Dati",
            "Personale", "Contratti e legale", "Ricerca", "Comunicazione",
        ],
        "function_other": "Altro",
        "seniority": {
            "ic": "Ci metto le mani",
            "lead": "Senior / di riferimento",
            "manager": "Responsabile",
            "director": "Direzione",
            "executive": "Vertice aziendale",
            "founder": "Fondatore / titolare",
        },
        "depth_anchors": [
            "per niente", "ne ho sentito parlare", "seguo il discorso",
            "lo faccio se qualcuno mi dà una mano", "lo faccio da solo", "lo saprei insegnare",
        ],
        "depth_helpers": [
            "Mai toccato",
            "So cos'è, poco altro",
            "Se ne parlano, capisco",
            "Ci arrivo con un aiuto",
            "Me la cavo da solo",
            "Lo spiegherei a un altro",
        ],
        "detail_prompt": "Cosa ci fai di preciso / cosa ti piace?",
        "recency": {
            "current": "Adesso",
            "rusty": "Un po' arrugginito",
            "long ago": "Tanto tempo fa",
        },
    },
}


CATALOGUES: dict[str, Catalogue] = {
    "en": Catalogue(
        lang="en",
        label="English",
        reviewed=True,
        strings=_EN,
        note="Reference catalogue. Shipped and read by real users since 2026-06.",
    ),
    "de": Catalogue(
        lang="de",
        label="Deutsch",
        reviewed=False,
        strings=_DE,
        note=(
            "REWRITTEN 2026-08-03 to informal 'du' and everyday phrasing, same "
            "pass as Italian. 'Sie' plus formal imperatives read as an authority "
            "writing to a citizen, and calques like 'Flughöhe' for Altitude were "
            "word-for-word defensible and not what anyone says. Standard written "
            "German with 'ss' (Swiss convention, harmless elsewhere). Still "
            "LLM-written and unreviewed: a native speaker should check that the "
            "everyday phrasing did not soften what each item MEASURES, and that "
            "'du' suits the actual recipient."
        ),
    ),
    "fr": Catalogue(
        lang="fr",
        label="Français",
        reviewed=False,
        strings=_FR,
        note=(
            "REWRITTEN 2026-08-03 to informal 'tu' and everyday phrasing, same "
            "pass as Italian and German. 'vous' turned a two-minute questionnaire "
            "into an administrative form, and 'Cadrage'/'Altitude' were calques. "
            "Still LLM-written and unreviewed: a native speaker should check that "
            "the everyday phrasing did not soften what each item MEASURES, and "
            "that 'tu' suits the actual recipient."
        ),
    ),
    "it": Catalogue(
        lang="it",
        label="Italiano",
        reviewed=False,
        # review_failures are NOT declared here — they name a real reviewer
        # and quote them. Loaded from the user's own store at import; see
        # _load_review_failures below.
        strings=_IT,
        note=(
            "REWRITTEN 2026-08-03 after a native reader called the first pass "
            "wooden. Now informal 'tu' and everyday phrasing. The first pass "
            "failed two ways worth remembering: 'Lei' plus formal imperatives "
            "is the register of an official form, and translating the English "
            "SENTENCE rather than the QUESTION produced calques ('Quota' for "
            "Altitude, 'Inquadramento' for Framing). Still LLM-written and "
            "still not formally reviewed — a native speaker should check that "
            "the everyday phrasing did not soften what each item MEASURES, "
            "which is the risk this rewrite introduces in exchange for the one "
            "it removes."
        ),
    ),
}


# ── Recorded review FAILURES — the user's data, not okuro's ──────────────
#
# A failure record names a human and quotes their verdict, so it is
# personal content and cannot ship in the package. The MECHANISM (the
# field, the guard's distinct refusal wording) is general and stays in
# code; only the instances live outside, in the user's own store.

_REVIEW_FAILURES_RELPATH = "surveys/review-failures.json"
_REVIEW_FAILURE_KEYS = ("date", "reviewer", "verdict")


def _review_failures_path():
    """``~/.okuro/surveys/review-failures.json``, honouring ``$OKURO_HOME``."""
    import os
    from pathlib import Path

    home = os.environ.get("OKURO_HOME")
    base = Path(home) if home else okuro_home()
    return base / _REVIEW_FAILURES_RELPATH


def _load_review_failures() -> dict[str, tuple[dict[str, str], ...]]:
    """Load ``{lang: (failure, ...)}`` from the user's store.

    Absent, unreadable or malformed yields ``{}``: the catalogues then
    carry no failures and the guard degrades to its "nobody has read it"
    refusal, which is weaker wording but never a wrong claim.

    Entries missing any of date/reviewer/verdict are DROPPED rather than
    passed through — survey_guard indexes all three to build its refusal,
    so a half-written record would raise inside an error path.
    """
    import json

    path = _review_failures_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — unreadable user data must not break the form
        return {}
    if not isinstance(data, dict):
        return {}

    out: dict[str, tuple[dict[str, str], ...]] = {}
    for lang, entries in data.items():
        if not isinstance(entries, list):
            continue
        good = tuple(
            {k: str(e[k]) for k in _REVIEW_FAILURE_KEYS}
            for e in entries
            if isinstance(e, dict) and all(e.get(k) for k in _REVIEW_FAILURE_KEYS)
        )
        if good:
            out[lang] = good
    return out


def _apply_review_failures() -> None:
    """Merge loaded failures onto the shipped catalogues, in place."""
    for lang, failures in _load_review_failures().items():
        cat = CATALOGUES.get(lang)
        if cat is not None:
            CATALOGUES[lang] = replace(cat, review_failures=failures)


_apply_review_failures()


def catalogue(lang: str | None = None) -> Catalogue:
    """Return a language's catalogue, falling back to English.

    Falls back rather than raising: a recipient opening a link must always
    get a usable form. The fallback is visible in the returned catalogue's
    ``lang``, so a caller that cares can tell it happened.
    """
    key = (lang or DEFAULT_LANGUAGE).lower().split("-")[0]
    return CATALOGUES.get(key) or CATALOGUES[DEFAULT_LANGUAGE]


def language_choices() -> list[dict[str, Any]]:
    """What a language picker needs: code, endonym, and whether it is checked.

    ``reviewed`` travels so the picker can say so BEFORE the download is
    attempted, rather than the user discovering it through a 409.
    """
    return [
        {"code": c.lang, "label": c.label or c.lang, "reviewed": c.reviewed,
         "note": c.note or ""}
        for k in LANGUAGES if (c := CATALOGUES.get(k))
    ]


def reviewed_languages() -> tuple[str, ...]:
    """Languages a human who speaks them has actually checked."""
    return tuple(k for k, c in CATALOGUES.items() if c.reviewed)


def available_languages() -> tuple[str, ...]:
    return tuple(k for k in LANGUAGES if k in CATALOGUES)


__all__ = [
    "LANGUAGES",
    "DEFAULT_LANGUAGE",
    "Catalogue",
    "CATALOGUES",
    "catalogue",
    "language_choices",
    "reviewed_languages",
    "available_languages",
]
