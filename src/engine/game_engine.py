"""Core Game Engine for Motion RPS IoT (Single Source of Truth)."""

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Dict, Optional, Tuple


class Choice(str, Enum):
    ROCK = "ROCK"
    PAPER = "PAPER"
    SCISSORS = "SCISSORS"


class GameState(str, Enum):
    WAITING_FOR_PLAYERS = "WAITING_FOR_PLAYERS"
    READY = "READY"
    COUNTDOWN = "COUNTDOWN"
    FINISHED = "FINISHED"


class RoundOutcome(str, Enum):
    PLAYER_A_WIN = "PLAYER_A_WIN"
    PLAYER_B_WIN = "PLAYER_B_WIN"
    DRAW = "DRAW"
    FORFEIT_A = "FORFEIT_A"          # Player A disconnected/timed out -> Player B wins
    FORFEIT_B = "FORFEIT_B"          # Player B disconnected/timed out -> Player A wins
    VOID_NO_MOVES = "VOID_NO_MOVES"  # Both players timed out -> no score change


PLAYER_A = "PLAYER_A"
PLAYER_B = "PLAYER_B"
VALID_PLAYERS = (PLAYER_A, PLAYER_B)

WIN_MAP = {
    Choice.ROCK: Choice.SCISSORS,
    Choice.SCISSORS: Choice.PAPER,
    Choice.PAPER: Choice.ROCK,
}


@dataclass
class PlayerState:
    player_id: str
    username: str
    connected: bool = False
    score: int = 0
    current_choice: Optional[Choice] = None
    choice_timestamp: Optional[float] = None


@dataclass
class RoundResult:
    round_id: int
    outcome: RoundOutcome
    winner_id: Optional[str]
    winner_username: Optional[str]
    player_a_choice: Optional[str]
    player_b_choice: Optional[str]
    player_a_outcome: str  # "WIN", "LOSE", "DRAW"
    player_b_outcome: str  # "WIN", "LOSE", "DRAW"
    scores: Dict[str, int]
    reason: str = ""


class GameEngine:
    """Authoritative state machine and rule evaluator for Motion RPS."""

    def __init__(self, session_id: str, countdown_seconds: float = 5.0) -> None:
        if not session_id or not session_id.strip():
            raise ValueError("session_id cannot be empty.")
        self.session_id: str = session_id.strip()
        self.countdown_seconds: float = float(countdown_seconds)
        self.state: GameState = GameState.WAITING_FOR_PLAYERS
        self.round_id: int = 0
        self.round_deadline: Optional[float] = None
        self._round_scored: bool = False
        self.last_result: Optional[RoundResult] = None

        self.players: Dict[str, PlayerState] = {
            PLAYER_A: PlayerState(player_id=PLAYER_A, username="Player A"),
            PLAYER_B: PlayerState(player_id=PLAYER_B, username="Player B"),
        }

    def _validate_player_id(self, player_id: str) -> None:
        if player_id not in VALID_PLAYERS:
            raise ValueError(f"Invalid player_id '{player_id}'. Expected one of {VALID_PLAYERS}.")

    def _refresh_connection_gate(self) -> None:
        """Transition between WAITING_FOR_PLAYERS and READY based on connections."""
        both_connected = all(p.connected for p in self.players.values())
        if self.state == GameState.WAITING_FOR_PLAYERS and both_connected:
            self.state = GameState.READY
        elif self.state == GameState.READY and not both_connected:
            self.state = GameState.WAITING_FOR_PLAYERS

    def set_player_connection(self, player_id: str, connected: bool) -> None:
        """Update connection status of a player and handle mid-round disconnects."""
        self._validate_player_id(player_id)
        self.players[player_id].connected = bool(connected)
        self._refresh_connection_gate()

    def set_username(self, player_id: str, username: str) -> str:
        """Update player's display username safely."""
        self._validate_player_id(player_id)
        cleaned = username.strip() if username else ""
        if not cleaned:
            raise ValueError("Username cannot be empty.")
        self.players[player_id].username = cleaned[:32]
        return self.players[player_id].username

    def start_round(self, now: Optional[float] = None) -> int:
        """Start a new round countdown if both players are connected."""
        if not all(p.connected for p in self.players.values()):
            raise RuntimeError("Cannot start round: both PLAYER_A and PLAYER_B must be connected.")
        if self.state == GameState.COUNTDOWN:
            raise RuntimeError("A round is already in progress.")

        current_time = time.time() if now is None else float(now)
        self.round_id += 1
        self.state = GameState.COUNTDOWN
        self.round_deadline = current_time + self.countdown_seconds
        self._round_scored = False
        self.last_result = None

        for player in self.players.values():
            player.current_choice = None
            player.choice_timestamp = None

        return self.round_id

    def submit_choice(
        self,
        player_id: str,
        raw_choice: str,
        round_id: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Record and lock a player's choice for the active round.
        Returns (accepted, reason). Automatically resolves the round if both choices are in.
        """
        self._validate_player_id(player_id)

        if self.state != GameState.COUNTDOWN:
            return False, "IGNORED_OUTSIDE_ACTIVE_ROUND"

        if round_id is not None and int(round_id) != self.round_id:
            return False, "IGNORED_WRONG_ROUND_ID"

        current_time = time.time() if now is None else float(now)
        if self.round_deadline is not None and current_time > self.round_deadline:
            return False, "IGNORED_LATE_SUBMISSION"

        player = self.players[player_id]
        if not player.connected:
            return False, "IGNORED_PLAYER_DISCONNECTED"

        if player.current_choice is not None:
            return False, "IGNORED_DUPLICATE_CHOICE"

        try:
            normalized = Choice(raw_choice.strip().upper())
        except (ValueError, AttributeError):
            return False, "INVALID_CHOICE"

        player.current_choice = normalized
        player.choice_timestamp = current_time

        # Auto-evaluate if both players have locked in their choices
        if all(p.current_choice is not None for p in self.players.values()):
            self.evaluate_round(force_timeout=False)

        return True, "ACCEPTED"

    def check_timeout(self, now: Optional[float] = None) -> Optional[RoundResult]:
        """Check if active countdown has expired and resolve the round if so."""
        if self.state != GameState.COUNTDOWN or self.round_deadline is None:
            return None
        current_time = time.time() if now is None else float(now)
        if current_time >= self.round_deadline:
            return self.evaluate_round(force_timeout=True)
        return None

    def evaluate_round(self, force_timeout: bool = False) -> RoundResult:
        """
        Evaluate the current round and update scores exactly once.
        Can be called when both choices are locked or when the countdown expires.
        """
        if self._round_scored and self.last_result is not None:
            return self.last_result

        if self.state != GameState.COUNTDOWN:
            raise RuntimeError("No active round to evaluate.")

        p_a = self.players[PLAYER_A]
        p_b = self.players[PLAYER_B]

        if not force_timeout and (p_a.current_choice is None or p_b.current_choice is None):
            raise RuntimeError("Cannot evaluate before timeout when choices are incomplete.")

        choice_a = p_a.current_choice
        choice_b = p_b.current_choice

        winner_id: Optional[str] = None
        reason = "NORMAL"

        if choice_a is not None and choice_b is not None:
            if choice_a == choice_b:
                outcome = RoundOutcome.DRAW
                a_out, b_out = "DRAW", "DRAW"
            elif WIN_MAP[choice_a] == choice_b:
                outcome = RoundOutcome.PLAYER_A_WIN
                winner_id = PLAYER_A
                a_out, b_out = "WIN", "LOSE"
            else:
                outcome = RoundOutcome.PLAYER_B_WIN
                winner_id = PLAYER_B
                a_out, b_out = "LOSE", "WIN"
        elif choice_a is not None and choice_b is None:
            outcome = RoundOutcome.FORFEIT_B
            winner_id = PLAYER_A
            a_out, b_out = "WIN", "LOSE"
            reason = "PLAYER_B_TIMEOUT_OR_DISCONNECT"
        elif choice_a is None and choice_b is not None:
            outcome = RoundOutcome.FORFEIT_A
            winner_id = PLAYER_B
            a_out, b_out = "LOSE", "WIN"
            reason = "PLAYER_A_TIMEOUT_OR_DISCONNECT"
        else:
            outcome = RoundOutcome.VOID_NO_MOVES
            winner_id = None
            a_out, b_out = "DRAW", "DRAW"
            reason = "BOTH_PLAYERS_TIMEOUT"

        # Score update guard: strictly once per round
        if not self._round_scored:
            if winner_id == PLAYER_A:
                p_a.score += 1
            elif winner_id == PLAYER_B:
                p_b.score += 1
            self._round_scored = True

        self.state = GameState.FINISHED
        winner_username = self.players[winner_id].username if winner_id else None

        self.last_result = RoundResult(
            round_id=self.round_id,
            outcome=outcome,
            winner_id=winner_id,
            winner_username=winner_username,
            player_a_choice=choice_a.value if choice_a else None,
            player_b_choice=choice_b.value if choice_b else None,
            player_a_outcome=a_out,
            player_b_outcome=b_out,
            scores={PLAYER_A: p_a.score, PLAYER_B: p_b.score},
            reason=reason,
        )
        return self.last_result

    def reset_scores(self, confirm: bool = False) -> None:
        """Reset both players' scores and return engine to READY/WAITING state."""
        if not confirm:
            raise ValueError("Score reset requires explicit confirmation (confirm=True).")

        for player in self.players.values():
            player.score = 0
            player.current_choice = None
            player.choice_timestamp = None

        self.round_id = 0
        self.round_deadline = None
        self._round_scored = False
        self.last_result = None
        self.state = GameState.WAITING_FOR_PLAYERS
        self._refresh_connection_gate()

    def get_state_snapshot(self, reveal_choices: bool = False) -> dict:
        """Return a serializable dictionary of the current game state."""
        show_choices = reveal_choices or (self.state == GameState.FINISHED)
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "round_id": self.round_id,
            "round_deadline": self.round_deadline,
            "players": {
                pid: {
                    "player_id": p.player_id,
                    "username": p.username,
                    "connected": p.connected,
                    "score": p.score,
                    "has_chosen": p.current_choice is not None,
                    "choice": p.current_choice.value if (show_choices and p.current_choice) else None,
                }
                for pid, p in self.players.items()
            },
            "last_result": (
                {
                    "round_id": self.last_result.round_id,
                    "outcome": self.last_result.outcome.value,
                    "winner_id": self.last_result.winner_id,
                    "winner_username": self.last_result.winner_username,
                    "player_a_choice": self.last_result.player_a_choice,
                    "player_b_choice": self.last_result.player_b_choice,
                    "player_a_outcome": self.last_result.player_a_outcome,
                    "player_b_outcome": self.last_result.player_b_outcome,
                    "scores": self.last_result.scores,
                    "reason": self.last_result.reason,
                }
                if self.last_result
                else None
            ),
        }
