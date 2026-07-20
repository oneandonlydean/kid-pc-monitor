"""Earn-time quizzes: spelling (unscramble) and maths questions.

Pure logic with no GUI or OS dependencies so it can be unit-tested anywhere.
The Windows agent presents each item in a centered dialog (see
``WindowsHostPlatform``); the parent panel only configures the reward.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random

SPELLING = "spelling"
MATHS = "maths"

# A kid-appropriate spelling word bank of varied length. Graded case-insensitively.
WORD_BANK: tuple[str, ...] = (
    "apple",
    "banana",
    "orange",
    "purple",
    "yellow",
    "garden",
    "planet",
    "rocket",
    "dragon",
    "castle",
    "friend",
    "school",
    "pencil",
    "window",
    "kitchen",
    "brother",
    "sister",
    "morning",
    "evening",
    "rainbow",
    "monster",
    "picture",
    "science",
    "history",
    "library",
    "holiday",
    "October",
    "chicken",
    "elephant",
    "dinosaur",
    "computer",
    "keyboard",
    "mountain",
    "sandwich",
    "birthday",
    "favorite",
    "vacation",
    "treasure",
    "adventure",
    "beautiful",
    "chocolate",
    "wonderful",
    "butterfly",
    "breakfast",
    "furniture",
    "important",
    "invisible",
    "telephone",
    "champion",
    "creature",
)


@dataclass(frozen=True)
class QuizItem:
    prompt: str
    answer: str


@dataclass(frozen=True)
class EarnSession:
    """A configured quiz offer, produced by the agent when the kid clicks Earn."""

    questions: int
    reward_minutes: int
    remaining_minutes: int  # how many more minutes may be earned today


def scramble_word(word: str, rng: Random) -> str:
    """Return the word's letters in a shuffled order that differs from the word."""
    if len(word) < 2:
        return word
    letters = list(word)
    for _ in range(20):
        rng.shuffle(letters)
        candidate = "".join(letters)
        if candidate.lower() != word.lower():
            return candidate
    return word[::-1]  # fallback for pathological cases (e.g. "aaaa")


def generate_spelling_quiz(
    count: int, rng: Random, *, word_bank: tuple[str, ...] = WORD_BANK
) -> list[QuizItem]:
    """Pick ``count`` distinct words, each shown as an unscramble task."""
    count = max(1, min(count, len(word_bank)))
    words = rng.sample(list(word_bank), count)
    return [
        QuizItem(
            prompt="Unscramble these letters to spell a word:\n\n    " + scramble_word(word, rng),
            answer=word,
        )
        for word in words
    ]


def _math_question(rng: Random) -> QuizItem:
    op = rng.choice(["+", "-", "×", "×", "÷"])
    if op == "+":
        a, b = rng.randint(2, 50), rng.randint(2, 50)
        return QuizItem(prompt=f"What is {a} + {b}?", answer=str(a + b))
    if op == "-":
        a, b = rng.randint(2, 50), rng.randint(2, 50)
        if b > a:
            a, b = b, a
        return QuizItem(prompt=f"What is {a} - {b}?", answer=str(a - b))
    if op == "×":
        a, b = rng.randint(2, 12), rng.randint(2, 12)
        return QuizItem(prompt=f"What is {a} × {b}?", answer=str(a * b))
    # Exact division only, so the answer is a whole number.
    divisor, quotient = rng.randint(2, 12), rng.randint(2, 12)
    return QuizItem(prompt=f"What is {divisor * quotient} ÷ {divisor}?", answer=str(quotient))


def generate_math_quiz(count: int, rng: Random) -> list[QuizItem]:
    """Generate ``count`` arithmetic questions (+, -, ×, exact ÷)."""
    count = max(1, count)
    return [_math_question(rng) for _ in range(count)]


def generate_quiz(subject: str, count: int, rng: Random) -> list[QuizItem]:
    """Generate a quiz for ``subject`` ('spelling' or 'maths')."""
    if subject == MATHS:
        return generate_math_quiz(count, rng)
    return generate_spelling_quiz(count, rng)


def is_correct(answer: str, response: str | None) -> bool:
    """Grade a response case-insensitively, ignoring surrounding whitespace."""
    return (response or "").strip().lower() == answer.strip().lower()
