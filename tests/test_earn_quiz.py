"""Tests for the earn-time quiz logic (spelling + maths, by difficulty)."""

from __future__ import annotations

import random
import unittest

from kid_pc_monitor.earn_quiz import (
    EASY,
    EASY_WORDS,
    HARD,
    HARD_WORDS,
    MEDIUM,
    MEDIUM_WORDS,
    WORD_BANK,
    blank_word,
    generate_math_quiz,
    generate_quiz,
    generate_spelling_quiz,
    is_correct,
    normalize_difficulty,
    scramble_word,
)


class DifficultyTests(unittest.TestCase):
    def test_normalize_accepts_valid_levels(self) -> None:
        self.assertEqual(normalize_difficulty("easy"), EASY)
        self.assertEqual(normalize_difficulty("HARD"), HARD)
        self.assertEqual(normalize_difficulty("  Medium "), MEDIUM)

    def test_normalize_defaults_unknown_to_medium(self) -> None:
        for bad in (None, "", "tricky", 5, "expert"):
            self.assertEqual(normalize_difficulty(bad), MEDIUM)  # type: ignore[arg-type]


class SpellingQuizTests(unittest.TestCase):
    def test_scramble_is_an_anagram_that_differs(self) -> None:
        rng = random.Random(1)
        for word in ("apple", "dinosaur", "rainbow", "wonderful"):
            scrambled = scramble_word(word, rng)
            self.assertEqual(sorted(scrambled.lower()), sorted(word.lower()))
            self.assertNotEqual(scrambled.lower(), word.lower())

    def test_blank_word_hides_requested_count_and_keeps_first_letter(self) -> None:
        rng = random.Random(4)
        display = blank_word("dragon", rng, blanks=2)
        self.assertEqual(display.count("_"), 2)
        self.assertTrue(display.startswith("d"))  # first letter never hidden
        # Every shown position matches the word; blanks stand in for the rest.
        shown = display.split(" ")
        self.assertEqual(len(shown), len("dragon"))
        for original, cell in zip("dragon", shown, strict=True):
            self.assertIn(cell, (original, "_"))

    def test_blank_word_never_hides_more_than_available(self) -> None:
        rng = random.Random(4)
        display = blank_word("cat", rng, blanks=99)
        # Only the two non-first letters can ever be hidden.
        self.assertLessEqual(display.count("_"), 2)
        self.assertTrue(display.startswith("c"))

    def test_easy_uses_short_words_and_one_missing_letter(self) -> None:
        items = generate_spelling_quiz(6, random.Random(2), difficulty=EASY)
        for item in items:
            self.assertIn(item.answer, EASY_WORDS)
            self.assertIn("missing letter", item.prompt)
            self.assertEqual(item.prompt.count("_"), 1)

    def test_medium_uses_medium_words_and_multiple_blanks(self) -> None:
        items = generate_spelling_quiz(6, random.Random(2), difficulty=MEDIUM)
        for item in items:
            self.assertIn(item.answer, MEDIUM_WORDS)
            self.assertIn("missing letters", item.prompt)
            self.assertGreaterEqual(item.prompt.count("_"), 1)

    def test_hard_uses_long_words_and_unscramble(self) -> None:
        items = generate_spelling_quiz(6, random.Random(2), difficulty=HARD)
        for item in items:
            self.assertIn(item.answer, HARD_WORDS)
            self.assertIn("Unscramble", item.prompt)

    def test_generate_spelling_count_and_distinct(self) -> None:
        items = generate_spelling_quiz(5, random.Random(2))
        self.assertEqual(len(items), 5)
        self.assertEqual(len({item.answer for item in items}), 5)
        for item in items:
            self.assertIn(item.answer, WORD_BANK)

    def test_generate_spelling_clamps_count_to_bank_size(self) -> None:
        self.assertEqual(len(generate_spelling_quiz(0, random.Random(3))), 1)
        self.assertLessEqual(
            len(generate_spelling_quiz(9999, random.Random(3), difficulty=EASY)),
            len(EASY_WORDS),
        )


class MathQuizTests(unittest.TestCase):
    def _check_answers(self, items) -> None:
        for item in items:
            expr = item.prompt.replace("What is ", "").rstrip("?").strip()
            expr = expr.replace("×", "*").replace("÷", "//")
            self.assertEqual(str(eval(expr)), item.answer)  # noqa: S307 - controlled input

    def test_answers_are_correct_at_every_difficulty(self) -> None:
        for level in (EASY, MEDIUM, HARD):
            self._check_answers(generate_math_quiz(60, random.Random(7), difficulty=level))

    def test_division_is_always_exact(self) -> None:
        for item in generate_math_quiz(100, random.Random(9), difficulty=HARD):
            if "÷" in item.prompt:
                a, b = item.prompt.replace("What is ", "").rstrip("?").split("÷")
                self.assertEqual(int(a) % int(b), 0)

    def test_easy_is_add_subtract_only_with_small_numbers(self) -> None:
        for item in generate_math_quiz(80, random.Random(11), difficulty=EASY):
            self.assertFalse("×" in item.prompt or "÷" in item.prompt)
            numbers = [
                int(n)
                for n in item.prompt.replace("What is ", "")
                .rstrip("?")
                .replace("+", " ")
                .replace("-", " ")
                .split()
            ]
            self.assertTrue(all(n <= 10 for n in numbers))

    def test_hard_includes_multiplication_and_larger_numbers(self) -> None:
        prompts = " ".join(
            item.prompt for item in generate_math_quiz(120, random.Random(13), difficulty=HARD)
        )
        self.assertIn("×", prompts)
        self.assertIn("÷", prompts)


class SharedQuizTests(unittest.TestCase):
    def test_generate_quiz_dispatches_by_subject(self) -> None:
        spelling = generate_quiz("spelling", 3, random.Random(1), EASY)[0].prompt
        self.assertIn("missing letter", spelling)
        self.assertIn("What is", generate_quiz("maths", 3, random.Random(1))[0].prompt)

    def test_generate_quiz_passes_difficulty_through(self) -> None:
        self.assertIn("Unscramble", generate_quiz("spelling", 3, random.Random(1), HARD)[0].prompt)

    def test_is_correct_is_case_and_space_insensitive(self) -> None:
        self.assertTrue(is_correct("Apple", " apple "))
        self.assertTrue(is_correct("56", "56 "))
        self.assertFalse(is_correct("56", "57"))
        self.assertFalse(is_correct("cat", None))


if __name__ == "__main__":
    unittest.main()
