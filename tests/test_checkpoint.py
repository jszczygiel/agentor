import unittest

from agentor.checkpoint import CheckpointConfig, CheckpointEmitter


class TestCheckpointEmitter(unittest.TestCase):
    def test_soft_threshold_emits_once(self):
        em = CheckpointEmitter(CheckpointConfig(
            soft_turns=60, hard_turns=100, output_tokens=0,
        ))
        emitted: list[str] = []
        for turn in range(1, 71):
            emitted.extend(em.observe(turn, 0))
        self.assertEqual(len(emitted), 1)
        self.assertIn("60", emitted[0])  # turn count interpolated

    def test_soft_and_hard_together(self):
        em = CheckpointEmitter(CheckpointConfig(
            soft_turns=60, hard_turns=100, output_tokens=0,
        ))
        payloads: list[str] = []
        for turn in range(1, 121):
            payloads.extend(em.observe(turn, 0))
        self.assertEqual(len(payloads), 2)
        # Soft fires first.
        self.assertIn("60 turns", payloads[0])
        self.assertIn("100 turns", payloads[1])

    def test_zero_disables_all_thresholds(self):
        cfg = CheckpointConfig(
            soft_turns=0, hard_turns=0, output_tokens=0,
        )
        self.assertTrue(cfg.all_disabled())
        em = CheckpointEmitter(cfg)
        for turn in range(1, 500):
            self.assertEqual(em.observe(turn, 1_000_000), [])
        self.assertFalse(em.any_fired)

    def test_token_gate_independent(self):
        em = CheckpointEmitter(CheckpointConfig(
            soft_turns=0, hard_turns=0, output_tokens=50_000,
        ))
        # Turns stay low but token total crosses the gate.
        self.assertEqual(em.observe(5, 40_000), [])
        emitted = em.observe(6, 55_000)
        self.assertEqual(len(emitted), 1)
        self.assertIn("55000", emitted[0])
        # Further observations do not re-fire.
        self.assertEqual(em.observe(7, 80_000), [])

    def test_idempotent_after_fire(self):
        em = CheckpointEmitter(CheckpointConfig(
            soft_turns=60, hard_turns=100, output_tokens=0,
        ))
        first = em.observe(60, 0)
        self.assertEqual(len(first), 1)
        # Re-observing with the same or higher turn count doesn't re-fire.
        self.assertEqual(em.observe(60, 0), [])
        self.assertEqual(em.observe(90, 0), [])
        # Hard still fires independently when it's own threshold is crossed.
        again = em.observe(100, 0)
        self.assertEqual(len(again), 1)
        self.assertIn("100 turns", again[0])

    def test_custom_templates_use_placeholders(self):
        em = CheckpointEmitter(CheckpointConfig(
            soft_turns=5, hard_turns=0, output_tokens=0,
            soft_template="SOFT {turns}/{output_tokens}",
        ))
        out = em.observe(5, 1234)
        self.assertEqual(out, ["SOFT 5/1234"])


if __name__ == "__main__":
    unittest.main()
