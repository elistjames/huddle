"""Labelled conversations for the real-model decider eval (Deep Dive §5, §11).

Each case is a short chat ending at the moment the agent decides whether to speak, plus what a
good participant would do. Labels come from the spec's policy, not from what a model does:
speak for a direct address, an answer to its own question, a question nobody has answered or a
stuck conversation, and a tangle; stay silent for greetings, acknowledgements, small talk between
people, and anything the people are already handling.

`turns` are (speaker, text). The speaker is a person's name or "agent" (an earlier message by
Huddle AI). Keep cases realistic and short; add a case whenever manual testing finds a miss.
"""

from __future__ import annotations

from dataclasses import dataclass

AGENT = "agent"


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    expect_speak: bool
    turns: tuple[tuple[str, str], ...]
    note: str = ""


def _c(
    id_: str, category: str, expect_speak: bool, *turns: tuple[str, str], note: str = ""
) -> Case:
    return Case(id_, category, expect_speak, tuple(turns), note)


CASES: tuple[Case, ...] = (
    # ---- talking to the agent: it must take a turn --------------------------------------------
    _c(
        "addr-nickname",
        "addressed",
        True,
        ("Eli", "what should we talk about today?"),
        ("Sam", "I dont know"),
        ("Sam", "Hey huddle are you there?"),
        note="manual QA",
    ),
    _c(
        "addr-the-ai",
        "addressed",
        True,
        ("Eli", "what should we talk about today?"),
        ("Sam", "I dont know"),
        ("Eli", "Maybe the AI has an idea"),
        note="manual QA",
    ),
    _c(
        "addr-full-name",
        "addressed",
        True,
        ("Maya", "Huddle AI, can you summarize where we landed on the venue?"),
    ),
    _c(
        "addr-at-sign", "addressed", True, ("Sam", "@huddle what's a good icebreaker for 8 people?")
    ),
    _c("addr-comma", "addressed", True, ("Eli", "huddle, thoughts on Friday vs Monday?")),
    # ---- answering the agent's own question ---------------------------------------------------
    _c(
        "answer-open-question",
        "answered",
        True,
        ("Eli", "I am not sure"),
        (AGENT, "Was it a specific topic or a general suggestion?"),
        ("Eli", "general suggestion"),
        note="manual QA",
    ),
    _c(
        "answer-yes",
        "answered",
        True,
        ("Sam", "should we book the venue?"),
        (AGENT, "Do you want me to draft a short agenda for it?"),
        ("Sam", "yes please"),
    ),
    _c(
        "answer-late",
        "answered",
        True,
        (AGENT, "Which day works better, Friday or Monday?"),
        ("Eli", "lol"),
        ("Sam", "Friday for me"),
    ),
    # ---- not addressing the agent -------------------------------------------------------------
    _c(
        "not-addr-chat-word",
        "not-addressed",
        False,
        ("Eli", "Start the huddle when everyone is ready"),
        ("Sam", "ok"),
    ),
    _c(
        "not-addr-noun",
        "not-addressed",
        False,
        ("Maya", "that huddle yesterday was really productive"),
        ("Sam", "agreed"),
    ),
    # ---- greetings and acknowledgements -------------------------------------------------------
    _c("greeting", "trivial", False, ("Eli", "Hello Sam"), ("Sam", "Hey there")),
    _c("ack", "trivial", False, ("Eli", "ok sounds good"), ("Sam", "thanks")),
    _c("goodbye", "trivial", False, ("Sam", "thanks all, bye")),
    _c(
        "courtesy-after-agent-question",
        "trivial",
        False,
        ("Sam", "what should we do for lunch?"),
        (AGENT, "How about the taco place near the office?"),
        ("Sam", "sounds good"),
    ),
    # ---- small talk and things people are handling --------------------------------------------
    _c(
        "smalltalk-weekend",
        "smalltalk",
        False,
        ("Eli", "how was your weekend?"),
        ("Sam", "pretty good, went hiking"),
        ("Eli", "nice, where did you go?"),
    ),
    _c(
        "smalltalk-lunch",
        "smalltalk",
        False,
        ("Maya", "I'm starving"),
        ("Sam", "same, lunch soon?"),
        ("Maya", "yes, pizza?"),
    ),
    _c(
        "joke",
        "smalltalk",
        False,
        ("Eli", "why did the chicken cross the road lol"),
        ("Sam", "to get to the other side haha"),
    ),
    _c(
        "handled-plans",
        "handled",
        False,
        ("Eli", "I am picking up the cake at 5"),
        ("Sam", "I'll bring the drinks"),
    ),
    _c(
        "handled-answered",
        "handled",
        False,
        ("Eli", "what time is the meeting?"),
        ("Sam", "3pm in room 4"),
    ),
    _c(
        "handled-answered-thanks",
        "handled",
        False,
        ("Maya", "does anyone know where the charger is?"),
        ("Sam", "it's on my desk"),
        ("Maya", "got it, thanks"),
    ),
    _c(
        "handled-tech",
        "handled",
        False,
        ("Eli", "how do I undo my last git commit?"),
        ("Sam", "git reset --soft HEAD~1"),
    ),
    _c(
        "handled-status",
        "handled",
        False,
        ("Maya", "I pushed the draft to the shared folder"),
        ("Sam", "thanks, will look after lunch"),
    ),
    _c("handled-logistics", "handled", False, ("Sam", "running 5 min late"), ("Eli", "no worries")),
    _c(
        "closure",
        "closure",
        False,
        ("Eli", "so we're going with Friday"),
        ("Sam", "perfect, Friday it is"),
        ("Eli", "great, thanks everyone"),
    ),
    # ---- a question nobody has answered, or people stuck: it should help ----------------------
    _c(
        "stuck-topic",
        "stuck",
        True,
        ("Eli", "what should we talk about today?"),
        ("Sam", "I dont know"),
        note="manual QA",
    ),
    _c(
        "stuck-decision",
        "stuck",
        True,
        ("Eli", "should we do the offsite in June or September?"),
        ("Sam", "I don't know, both work"),
        ("Eli", "yeah I can't decide"),
    ),
    _c(
        "stuck-party",
        "stuck",
        True,
        ("Sam", "we need to plan the birthday party for Dana"),
        ("Eli", "yes, what should we do?"),
        ("Sam", "no idea"),
    ),
    _c(
        "stuck-name",
        "stuck",
        True,
        ("Maya", "we need a name for the new feature, any ideas?"),
        ("Sam", "hmm not sure"),
    ),
    _c(
        "unanswered-concept",
        "unanswered",
        True,
        ("Eli", "What's the difference between a hot and cold standby for a database?"),
    ),
    _c(
        "unanswered-choice",
        "unanswered",
        True,
        ("Sam", "is it better to use SQLite or Postgres for a small side project?"),
        ("Eli", "no idea honestly"),
    ),
    _c(
        "unanswered-capacity",
        "unanswered",
        True,
        (
            "Maya",
            "anyone know roughly how many people fit in a standard conference room for a workshop?",
        ),
    ),
    _c(
        "help-request",
        "unanswered",
        True,
        ("Sam", "can anyone help me write a polite reply declining a meeting?"),
    ),
    _c(
        "brainstorm",
        "unanswered",
        True,
        ("Eli", "we need a few icebreaker ideas for Friday"),
        ("Sam", "yeah that would be good"),
    ),
    _c(
        "wrong-fact",
        "tangle",
        True,
        ("Eli", "the capital of Australia is Sydney right?"),
        ("Sam", "yeah I think so"),
    ),
    _c(
        "tangle-db",
        "tangle",
        True,
        ("Eli", "we should use Postgres for the new service"),
        ("Sam", "no, MySQL is simpler and we already run it"),
        ("Eli", "but Postgres has better JSON support"),
        ("Maya", "actually the JSON support in MySQL is fine now"),
        ("Sam", "however our ops team only knows MySQL"),
        ("Eli", "I thought we agreed on Postgres last week?"),
        ("Maya", "wait, did we? I don't remember that"),
    ),
)
