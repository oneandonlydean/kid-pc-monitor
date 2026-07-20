"""Earn-time quizzes: spelling (missing letters / unscramble) and maths.

Pure logic with no GUI or OS dependencies so it can be unit-tested anywhere.
The Windows agent presents each item in a centered dialog (see
``WindowsHostPlatform``); the parent panel only configures the reward and the
per-subject difficulty.

Difficulty grades both the content and the presentation:

* Spelling — Easy shows a short word with one missing letter to fill in, Medium
  a longer word with several missing letters, and Hard a full anagram to
  unscramble. The child always types the whole word; grading is unchanged.
* Maths — Easy is addition/subtraction with small numbers, Medium adds
  multiplication and exact division with times-table numbers, and Hard uses
  larger numbers across all four operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random

SPELLING = "spelling"
MATHS = "maths"

EASY = "easy"
MEDIUM = "medium"
HARD = "hard"
DIFFICULTIES: tuple[str, ...] = (EASY, MEDIUM, HARD)


def normalize_difficulty(value: str | None) -> str:
    """Return a valid difficulty, defaulting unknown/blank values to Medium."""
    if isinstance(value, str) and value.strip().lower() in DIFFICULTIES:
        return value.strip().lower()
    return MEDIUM


# Kid-appropriate spelling words grouped by length so difficulty can pick the
# right challenge. Graded case-insensitively. Words avoid repeated-letter
# pathologies so both blanking and unscrambling stay unambiguous.
EASY_WORDS: tuple[str, ...] = (
    "cat",
    "dog",
    "sun",
    "hat",
    "run",
    "red",
    "cup",
    "pen",
    "bed",
    "box",
    "fish",
    "tree",
    "book",
    "star",
    "milk",
    "frog",
    "cake",
    "jump",
    "blue",
    "gold",
    "moon",
    "rain",
    "ship",
    "king",
    "bird",
    "hand",
    "door",
    "lion",
    "bear",
    "duck",
    "goat",
    "nest",
)
MEDIUM_WORDS: tuple[str, ...] = (
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
    "sister",
    "table",
    "water",
    "tiger",
    "horse",
    "mouse",
    "cloud",
    "green",
    "bread",
    "chair",
    "grass",
    "plant",
    "sheep",
    "snake",
    "brush",
    "candle",
)
HARD_WORDS: tuple[str, ...] = (
    "kitchen",
    "brother",
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
    "champion",
    "creature",
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
)

_SPELLING_BANKS: dict[str, tuple[str, ...]] = {
    EASY: EASY_WORDS,
    MEDIUM: MEDIUM_WORDS,
    HARD: HARD_WORDS,
}

# Union of all tiers; kept for callers/tests that want the whole vocabulary.
WORD_BANK: tuple[str, ...] = EASY_WORDS + MEDIUM_WORDS + HARD_WORDS


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
    spelling_difficulty: str = MEDIUM
    maths_difficulty: str = MEDIUM


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


def blank_word(word: str, rng: Random, *, blanks: int) -> str:
    """Show ``word`` spaced out with ``blanks`` interior letters hidden as ``_``.

    The first letter is never hidden, giving the child a clear starting anchor.
    Fewer letters are hidden than requested when the word is too short to spare
    them.
    """
    letters = list(word)
    positions = list(range(1, len(letters)))  # never blank the first letter
    rng.shuffle(positions)
    hidden = set(positions[: max(0, blanks)])
    return " ".join("_" if i in hidden else ch for i, ch in enumerate(letters))


def _spelling_item(word: str, rng: Random, difficulty: str) -> QuizItem:
    if difficulty == HARD:
        prompt = "Unscramble these letters to spell a word:\n\n    " + scramble_word(word, rng)
        return QuizItem(prompt=prompt, answer=word)
    blanks = 1 if difficulty == EASY else max(1, (len(word) - 1) // 2)
    display = blank_word(word, rng, blanks=blanks)
    label = (
        "Fill in the missing letter:" if display.count("_") == 1 else "Fill in the missing letters:"
    )
    return QuizItem(prompt=f"{label}\n\n    {display}", answer=word)


def generate_spelling_quiz(
    count: int,
    rng: Random,
    *,
    difficulty: str = MEDIUM,
    word_bank: tuple[str, ...] | None = None,
) -> list[QuizItem]:
    """Pick ``count`` distinct words and present each per ``difficulty``."""
    difficulty = normalize_difficulty(difficulty)
    bank = word_bank if word_bank is not None else _SPELLING_BANKS[difficulty]
    count = max(1, min(count, len(bank)))
    words = rng.sample(list(bank), count)
    return [_spelling_item(word, rng, difficulty) for word in words]


# Number ranges and operations for each maths difficulty. ``ops`` lists the
# allowed operators (``×`` repeated to weight it); ``add`` bounds addition and
# subtraction operands, ``mul`` the multiplication operands, and ``div`` the
# divisor and quotient of exact divisions.
_MATH_LEVELS: dict[str, dict[str, object]] = {
    EASY: {"ops": ["+", "-"], "add": (1, 10), "mul": (2, 5), "div": (2, 5)},
    MEDIUM: {"ops": ["+", "-", "×", "×", "÷"], "add": (2, 50), "mul": (2, 12), "div": (2, 12)},
    HARD: {"ops": ["+", "-", "×", "÷"], "add": (10, 99), "mul": (6, 15), "div": (3, 15)},
}


def _math_question(rng: Random, difficulty: str) -> QuizItem:
    level = _MATH_LEVELS[difficulty]
    ops: list[str] = level["ops"]  # type: ignore[assignment]
    add_lo, add_hi = level["add"]  # type: ignore[misc]
    mul_lo, mul_hi = level["mul"]  # type: ignore[misc]
    div_lo, div_hi = level["div"]  # type: ignore[misc]
    op = rng.choice(ops)
    if op == "+":
        a, b = rng.randint(add_lo, add_hi), rng.randint(add_lo, add_hi)
        return QuizItem(prompt=f"What is {a} + {b}?", answer=str(a + b))
    if op == "-":
        a, b = rng.randint(add_lo, add_hi), rng.randint(add_lo, add_hi)
        if b > a:
            a, b = b, a
        return QuizItem(prompt=f"What is {a} - {b}?", answer=str(a - b))
    if op == "×":
        a, b = rng.randint(mul_lo, mul_hi), rng.randint(mul_lo, mul_hi)
        return QuizItem(prompt=f"What is {a} × {b}?", answer=str(a * b))
    # Exact division only, so the answer is a whole number.
    divisor, quotient = rng.randint(div_lo, div_hi), rng.randint(div_lo, div_hi)
    return QuizItem(prompt=f"What is {divisor * quotient} ÷ {divisor}?", answer=str(quotient))


def generate_math_quiz(count: int, rng: Random, *, difficulty: str = MEDIUM) -> list[QuizItem]:
    """Generate ``count`` arithmetic questions graded by ``difficulty``."""
    difficulty = normalize_difficulty(difficulty)
    count = max(1, count)
    return [_math_question(rng, difficulty) for _ in range(count)]


def generate_quiz(
    subject: str, count: int, rng: Random, difficulty: str = MEDIUM
) -> list[QuizItem]:
    """Generate a quiz for ``subject`` ('spelling' or 'maths') at ``difficulty``."""
    if subject == MATHS:
        return generate_math_quiz(count, rng, difficulty=difficulty)
    return generate_spelling_quiz(count, rng, difficulty=difficulty)


def is_correct(answer: str, response: str | None) -> bool:
    """Grade a response case-insensitively, ignoring surrounding whitespace."""
    return (response or "").strip().lower() == answer.strip().lower()
