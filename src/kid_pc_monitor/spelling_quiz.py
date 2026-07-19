"""Spelling "earn time" quiz: unscramble letters to spell a word.

Pure logic with no GUI or OS dependencies so it can be unit-tested anywhere.
The Windows agent presents each item with a simple text dialog (see
``WindowsHostPlatform``); the parent panel only configures the reward.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random

# A kid-appropriate spelling word bank of varied length. Kept lowercase; the
# quiz grades case-insensitively.
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
    answer: str
    scrambled: str


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


def generate_quiz(
    count: int, rng: Random, *, word_bank: tuple[str, ...] = WORD_BANK
) -> list[QuizItem]:
    """Pick ``count`` distinct words and scramble each into an unscramble task."""
    count = max(1, min(count, len(word_bank)))
    words = rng.sample(list(word_bank), count)
    return [QuizItem(answer=word, scrambled=scramble_word(word, rng)) for word in words]


def is_correct(answer: str, response: str | None) -> bool:
    """Grade a response case-insensitively, ignoring surrounding whitespace."""
    return (response or "").strip().lower() == answer.strip().lower()
