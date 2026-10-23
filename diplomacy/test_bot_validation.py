import unittest

import bot


class GeneratedClaimValidationTests(unittest.TestCase):
    def test_rejects_merz_as_german_president(self):
        valid, reason = bot.validate_generated_claims(
            "Президент Німеччини Фрідріх Мерц відвідає Китай",
            "У тексті джерела згадано Франк-Вальтера Штайнмаєра.",
        )
        self.assertFalse(valid)
        self.assertIn("канцлером", reason)

    def test_rejects_steinmeier_as_german_chancellor(self):
        valid, reason = bot.validate_generated_claims(
            "Канцлер Німеччини Франк-Вальтер Штайнмаєр відвідає Китай",
            "Заплановано офіційний візит.",
        )
        self.assertFalse(valid)
        self.assertIn("президентом", reason)

    def test_accepts_correct_roles_even_when_both_people_are_mentioned(self):
        valid, reason = bot.validate_generated_claims(
            "Канцлер Німеччини Фрідріх Мерц відвідає Японію",
            "Президент Німеччини Франк-Вальтер Штайнмаєр планує візит до Китаю.",
        )
        self.assertTrue(valid)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
