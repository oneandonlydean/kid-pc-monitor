"""Tests for the earn-time quiz logic (spelling + maths)."""

from __future__ import annotations

import random
import unittest

from kid_pc_monitor.earn_quiz import (
    WORD_BANK,
    generate_math_quiz,
    generate_quiz,
    generate_spelling_quiz,
    is_correct,
    scramble_word,
)


class SpellingQuizTests(unittest.TestCase):
    def test_scramble_is_an_anagram_that_differs(self) -> None:
        rng = random.Random(1)
        for word in ("apple", "dinosaur", "rainbow", "wonderful"):
            scrambled = scramble_word(word, rng)
            self.assertEqual(sorted(scrambled.lower()), sorted(word.lower()))
            self.assertNotEqual(scrambled.lower(), word.lower())

    def test_generate_spelling_count_and_distinct(self) -> None:
        items = generate_spelling_quiz(5, random.Random(2))
        self.assertEqual(len(items), 5)
        self.assertEqual(len({item.answer for item in items}), 5)
        for item in items:
            self.assertIn(item.answer, WORD_BANK)
            self.assertIn("Unscramble", item.prompt)

    def test_generate_spelling_clamps_count(self) -> None:
        self.assertEqual(len(generate_spelling_quiz(0, random.Random(3))), 1)
        self.assertLessEqual(len(generate_spelling_quiz(9999, random.Random(3))), len(WORD_BANK))


class MathQuizTests(unittest.TestCase):
    def test_math_answers_are_correct(self) -> None:
        # Every generated question's stated answer must be the right one.
        items = generate_math_quiz(50, random.Random(7))
        self.assertEqual(len(items), 50)
        for item in items:
            expr = item.prompt.replace("What is ", "").rstrip("?").strip()
            expr = expr.replace("×", "*").replace("÷", "//")
            self.assertEqual(str(eval(expr)), item.answer)  # noqa: S307 - controlled input

    def test_division_is_always_exact(self) -> None:
        for item in generate_math_quiz(100, random.Random(9)):
            if "÷" in item.prompt:
                a, b = item.prompt.replace("What is ", "").rstrip("?").split("÷")
                self.assertEqual(int(a) % int(b), 0)


class SharedQuizTests(unittest.TestCase):
    def test_generate_quiz_dispatches_by_subject(self) -> None:
        self.assertIn("Unscramble", generate_quiz("spelling", 3, random.Random(1))[0].prompt)
        self.assertIn("What is", generate_quiz("maths", 3, random.Random(1))[0].prompt)

    def test_is_correct_is_case_and_space_insensitive(self) -> None:
        self.assertTrue(is_correct("Apple", " apple "))
        self.assertTrue(is_correct("56", "56 "))
        self.assertFalse(is_correct("56", "57"))
        self.assertFalse(is_correct("cat", None))


if __name__ == "__main__":
    unittest.main()
