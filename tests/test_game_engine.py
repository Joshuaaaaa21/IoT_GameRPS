"""Unit tests for the core Motion RPS GameEngine."""

import unittest
from src.engine.game_engine import (
    Choice,
    GameEngine,
    GameState,
    PLAYER_A,
    PLAYER_B,
    RoundOutcome,
)


class TestGameEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = GameEngine(session_id="test-session-123", countdown_seconds=5.0)
        self.engine.set_player_connection(PLAYER_A, True)
        self.engine.set_player_connection(PLAYER_B, True)

    def test_initial_connection_and_ready_state(self) -> None:
        engine = GameEngine(session_id="s1")
        self.assertEqual(engine.state, GameState.WAITING_FOR_PLAYERS)
        engine.set_player_connection(PLAYER_A, True)
        self.assertEqual(engine.state, GameState.WAITING_FOR_PLAYERS)
        engine.set_player_connection(PLAYER_B, True)
        self.assertEqual(engine.state, GameState.READY)
        engine.set_player_connection(PLAYER_A, False)
        self.assertEqual(engine.state, GameState.WAITING_FOR_PLAYERS)

    def test_cannot_start_round_without_both_players(self) -> None:
        self.engine.set_player_connection(PLAYER_B, False)
        with self.assertRaises(RuntimeError):
            self.engine.start_round(now=100.0)

    def test_all_rps_rule_combinations(self) -> None:
        cases = [
            ("ROCK", "SCISSORS", RoundOutcome.PLAYER_A_WIN, PLAYER_A, 1, 0),
            ("SCISSORS", "PAPER", RoundOutcome.PLAYER_A_WIN, PLAYER_A, 1, 0),
            ("PAPER", "ROCK", RoundOutcome.PLAYER_A_WIN, PLAYER_A, 1, 0),
            ("SCISSORS", "ROCK", RoundOutcome.PLAYER_B_WIN, PLAYER_B, 0, 1),
            ("PAPER", "SCISSORS", RoundOutcome.PLAYER_B_WIN, PLAYER_B, 0, 1),
            ("ROCK", "PAPER", RoundOutcome.PLAYER_B_WIN, PLAYER_B, 0, 1),
            ("ROCK", "ROCK", RoundOutcome.DRAW, None, 0, 0),
            ("PAPER", "PAPER", RoundOutcome.DRAW, None, 0, 0),
            ("SCISSORS", "SCISSORS", RoundOutcome.DRAW, None, 0, 0),
        ]
        for choice_a, choice_b, expected_outcome, expected_winner, score_a, score_b in cases:
            with self.subTest(a=choice_a, b=choice_b):
                self.engine.reset_scores(confirm=True)
                self.engine.start_round(now=10.0)
                ok_a, _ = self.engine.submit_choice(PLAYER_A, choice_a, now=11.0)
                ok_b, _ = self.engine.submit_choice(PLAYER_B, choice_b, now=12.0)
                self.assertTrue(ok_a)
                self.assertTrue(ok_b)
                self.assertEqual(self.engine.state, GameState.FINISHED)
                res = self.engine.last_result
                self.assertIsNotNone(res)
                self.assertEqual(res.outcome, expected_outcome)
                self.assertEqual(res.winner_id, expected_winner)
                self.assertEqual(self.engine.players[PLAYER_A].score, score_a)
                self.assertEqual(self.engine.players[PLAYER_B].score, score_b)

    def test_duplicate_choice_and_single_score_update_per_round(self) -> None:
        r_id = self.engine.start_round(now=50.0)
        ok1, reason1 = self.engine.submit_choice(PLAYER_A, "ROCK", round_id=r_id, now=51.0)
        self.assertTrue(ok1)
        self.assertEqual(reason1, "ACCEPTED")

        # Duplicate choice from Player A before Player B moves
        ok2, reason2 = self.engine.submit_choice(PLAYER_A, "PAPER", round_id=r_id, now=51.5)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "IGNORED_DUPLICATE_CHOICE")
        self.assertEqual(self.engine.players[PLAYER_A].current_choice, Choice.ROCK)

        # Player B submits -> auto evaluates round
        ok_b, _ = self.engine.submit_choice(PLAYER_B, "SCISSORS", round_id=r_id, now=52.0)
        self.assertTrue(ok_b)
        self.assertEqual(self.engine.players[PLAYER_A].score, 1)

        # Extra evaluation call or post-round submission must not increment score again
        self.engine.evaluate_round(force_timeout=True)
        ok_late, reason_late = self.engine.submit_choice(PLAYER_A, "ROCK", round_id=r_id, now=53.0)
        self.assertFalse(ok_late)
        self.assertEqual(reason_late, "IGNORED_OUTSIDE_ACTIVE_ROUND")
        self.assertEqual(self.engine.players[PLAYER_A].score, 1)

    def test_ignore_choice_outside_round_or_wrong_round_id(self) -> None:
        # Before round starts
        ok, reason = self.engine.submit_choice(PLAYER_A, "ROCK", now=1.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "IGNORED_OUTSIDE_ACTIVE_ROUND")

        # During round but wrong round_id
        self.engine.start_round(now=10.0)
        ok_wrong, reason_wrong = self.engine.submit_choice(PLAYER_A, "ROCK", round_id=999, now=11.0)
        self.assertFalse(ok_wrong)
        self.assertEqual(reason_wrong, "IGNORED_WRONG_ROUND_ID")

    def test_late_submission_and_timeout_forfeit(self) -> None:
        self.engine.start_round(now=100.0)  # deadline = 105.0
        ok_a, _ = self.engine.submit_choice(PLAYER_A, "PAPER", now=102.0)
        self.assertTrue(ok_a)

        # Player B sends after deadline
        ok_b, reason_b = self.engine.submit_choice(PLAYER_B, "SCISSORS", now=106.0)
        self.assertFalse(ok_b)
        self.assertEqual(reason_b, "IGNORED_LATE_SUBMISSION")

        # Engine checks timeout at t=106.0
        result = self.engine.check_timeout(now=106.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, RoundOutcome.FORFEIT_B)
        self.assertEqual(result.winner_id, PLAYER_A)
        self.assertEqual(self.engine.players[PLAYER_A].score, 1)
        self.assertEqual(self.engine.players[PLAYER_B].score, 0)

    def test_both_players_timeout_void_round(self) -> None:
        self.engine.start_round(now=200.0)
        result = self.engine.check_timeout(now=206.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, RoundOutcome.VOID_NO_MOVES)
        self.assertIsNone(result.winner_id)
        self.assertEqual(self.engine.players[PLAYER_A].score, 0)
        self.assertEqual(self.engine.players[PLAYER_B].score, 0)

    def test_username_edit_and_reset_score_confirmation(self) -> None:
        self.engine.set_username(PLAYER_A, "  Andi  ")
        self.engine.set_username(PLAYER_B, "Budi")
        self.assertEqual(self.engine.players[PLAYER_A].username, "Andi")

        self.engine.start_round(now=10.0)
        self.engine.submit_choice(PLAYER_A, "ROCK", now=11.0)
        self.engine.submit_choice(PLAYER_B, "SCISSORS", now=12.0)
        self.assertEqual(self.engine.last_result.winner_username, "Andi")
        self.assertEqual(self.engine.players[PLAYER_A].score, 1)

        # Reset without confirmation must fail
        with self.assertRaises(ValueError):
            self.engine.reset_scores(confirm=False)
        self.assertEqual(self.engine.players[PLAYER_A].score, 1)

        # Reset with confirmation succeeds
        self.engine.reset_scores(confirm=True)
        self.assertEqual(self.engine.players[PLAYER_A].score, 0)
        self.assertEqual(self.engine.round_id, 0)
        self.assertEqual(self.engine.state, GameState.READY)


if __name__ == "__main__":
    unittest.main()
