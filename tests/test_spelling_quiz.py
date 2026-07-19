"""Tests for the spelling "earn time" quiz logic."""

from __future__ import annotations

import random
import unittest

from kid_pc_monitor.spelling_quiz import (
    WORD_BANK,
    generate_quiz,
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

    def test_scramble_single_letter_unchanged(self) -> None:
        self.assertEqual(scramble_word("a", random.Random(1)), "a")

    def test_generate_quiz_count_and_distinct_words(self) -> None:
        items = generate_quiz(5, random.Random(2))
        self.assertEqual(len(items), 5)
        answers = [item.answer for item in items]
        self.assertEqual(len(set(answers)), 5)
        for item in items:
            self.assertIn(item.answer, WORD_BANK)
            self.assertEqual(sorted(item.scrambled.lower()), sorted(item.answer.lower()))

    def test_generate_quiz_clamps_count(self) -> None:
        self.assertEqual(len(generate_quiz(0, random.Random(3))), 1)
        self.assertLessEqual(len(generate_quiz(9999, random.Random(3))), len(WORD_BANK))

    def test_is_correct_is_case_and_space_insensitive(self) -> None:
        self.assertTrue(is_correct("Apple", " apple "))
        self.assertTrue(is_correct("cat", "CAT"))
        self.assertFalse(is_correct("cat", "dog"))
        self.assertFalse(is_correct("cat", None))


if __name__ == "__main__":
    unittest.main()
