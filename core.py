"""Shared plumbing for chess-coach: lichess access, request pacing,
puzzle digests, statistics, and the local data files. Used by the MCP
server; also usable by any other frontend."""

import json
import os
import re
import threading
import time
from datetime import datetime

import chess
import chess.engine
import requests

import debrief

API = "https://lichess.org"

TOKEN_CREATE_URL = (API + "/account/oauth/token/create"
                    "?scopes[]=puzzle:read"
                    "&description=chess-coach+companion")

PROJECT_DIR = debrief.PROJECT_DIR

STATE_PATH = os.path.join(PROJECT_DIR, "data", "coach-state.json")

ENGINE_LOCK = threading.Lock()

STATE_LOCK = threading.Lock()


def get_token():
    tok = os.environ.get("LICHESS_TOKEN", "").strip()
    if tok:
        return tok
    path = os.path.join(PROJECT_DIR, ".lichess-token")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return None


def api_headers(token, ndjson=False):
    h = {"User-Agent": debrief.USER_AGENT}
    if ndjson:
        h["Accept"] = "application/x-ndjson"
    if token:
        h["Authorization"] = "Bearer " + token
    return h


REST_LOCK = threading.Lock()

REST_MIN_GAP = 1.0

_last_rest = [0.0]


def rest_request(method, url, **kw):
    kw.setdefault("timeout", 30)
    with REST_LOCK:
        for attempt in (1, 2):
            wait = REST_MIN_GAP - (time.monotonic() - _last_rest[0])
            if wait > 0:
                time.sleep(wait)
            try:
                r = requests.request(method, url, **kw)
            except (requests.ConnectionError, requests.Timeout):
                _last_rest[0] = time.monotonic()
                if attempt == 2:
                    raise
                time.sleep(1)
                continue
            _last_rest[0] = time.monotonic()
            if r.status_code != 429 or attempt == 2:
                return r
            time.sleep(61)  # lichess asks for a full minute after a 429


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"last_puzzle_ms": 0}


def save_state(state):
    with STATE_LOCK:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)


def stats_record_attempt(state, themes, won):
    totals = state.setdefault("attempt_totals", {"tries": 0, "fails": 0})
    totals["tries"] += 1
    if not won:
        totals["fails"] += 1
    ts = state.setdefault("theme_stats", {})
    for theme in themes:
        t = ts.setdefault(theme, {"tries": 0, "fails": 0})
        t["tries"] += 1
        if not won:
            t["fails"] += 1


def out_dir_for(username):
    d = os.path.join(PROJECT_DIR, "reports", username.lower())
    os.makedirs(d, exist_ok=True)
    return d


def append_lesson(username, entry_md):
    """One lessons file per day, so no single file bloats over time."""
    d = out_dir_for(username)
    name = "lessons-{}.md".format(datetime.now().strftime("%Y-%m-%d"))
    path = os.path.join(d, name)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("# Lessons - {}\n\n".format(datetime.now().strftime("%B %-d, %Y")))
    with open(path, "a") as f:
        f.write(entry_md + "\n\n---\n\n")
    with open(os.path.join(d, "coach-latest.md"), "w") as f:
        f.write(entry_md + "\n")
    return path


PUZZLE_URL_RE = re.compile(r"lichess\.org/training/(\w{5})")

PUZZLE_ID_RE = re.compile(r"^\w{5}$")


def fetch_puzzle(puzzle_id):
    r = rest_request("GET", API + "/api/puzzle/" + puzzle_id,
                     headers=api_headers(None))
    r.raise_for_status()
    return r.json()


def puzzle_digest(data, engine, per_move_time=1.0):
    """Engine-verified digest of a lichess puzzle object."""
    pz = data["puzzle"]
    board = chess.Board(pz["fen"])
    solver = "White" if board.turn == chess.WHITE else "Black"
    per_move = []
    fens = [board.fen()]
    gain = 0
    with ENGINE_LOCK:
        first_ok = None
        tempting = None
        for n, uci in enumerate(pz["solution"]):
            move = chess.Move.from_uci(uci)
            san = board.san(move)
            is_solver = (n % 2 == 0)
            captured = board.piece_at(move.to_square)
            if n == 0 and engine:
                infos = engine.analyse(board, chess.engine.Limit(time=per_move_time),
                                       multipv=2)
                top = infos[0].get("pv", [None])[0]
                first_ok = (top == move)
                tempting = _tempting_alternative(board, move, infos, engine,
                                                 per_move_time)
            board.push(move)
            fens.append(board.fen())
            entry = {"san": san, "uci": uci, "by": "solver" if is_solver else "opponent"}
            if board.is_check():
                entry["gives_check"] = True
            if captured:
                name = chess.piece_name(captured.piece_type)
                entry["captures"] = name
                if is_solver:
                    gain += {"pawn": 1, "knight": 3, "bishop": 3,
                             "rook": 5, "queen": 9}.get(name, 0)
            if engine and not board.is_game_over():
                info = engine.analyse(board, chess.engine.Limit(time=per_move_time / 2))
                entry["eval_after"] = debrief.fmt_eval(debrief.score_to_cp(info["score"]))
            elif board.is_checkmate():
                entry["eval_after"] = "checkmate"
            per_move.append(entry)
    return {
        "id": pz["id"],
        "solver_plays": solver,
        "fen": pz["fen"],
        "fens": fens,
        "rating": pz.get("rating"),
        "themes": pz.get("themes", []),
        "solution": per_move,
        "material_won_by_solver": gain,
        "engine_confirms_first_move": first_ok,
        "tempting_alternative": tempting,
    }


def _tempting_alternative(board, solution_move, infos, engine, per_move_time):
    """The engine's second choice at the puzzle start: the natural-looking
    move a student most plausibly tried, with its refutation."""
    if len(infos) < 2 or not infos[1].get("pv"):
        return None
    alt = infos[1]["pv"][0]
    if alt == solution_move:
        return None
    best_wc = debrief.wc_white(debrief.score_to_cp(infos[0]["score"]))
    alt_wc = debrief.wc_white(debrief.score_to_cp(infos[1]["score"]))
    solver_white = board.turn == chess.WHITE
    gap = (best_wc - alt_wc) if solver_white else (alt_wc - best_wc)
    if gap < 0.10:
        return None  # the alternative is nearly as good; nothing to warn about
    b = board.copy()
    alt_san = b.san(alt)
    b.push(alt)
    info = engine.analyse(b, chess.engine.Limit(time=per_move_time))
    refutation = b.variation_san(info.get("pv", [])[:4]) if info.get("pv") else ""
    return {
        "san": alt_san,
        "eval_after": debrief.fmt_eval(debrief.score_to_cp(info["score"])),
        "refutation": refutation,
        "winning_chances_worse_by_pct": int(round(gap * 100)),
    }


def quick_scan(fen):
    """Millisecond-fast facts about a position: no engine, no AI. Hanging
    pieces, undefended targets, and available checks."""
    board = chess.Board(fen)
    mover = board.turn
    mine_hanging, theirs_undefended = [], []
    for color in (chess.WHITE, chess.BLACK):
        for sq in chess.SQUARES:
            piece = board.piece_at(sq)
            if not piece or piece.color != color or piece.piece_type == chess.KING:
                continue
            name = "{} on {}".format(
                chess.piece_name(piece.piece_type), chess.square_name(sq))
            defended = bool(board.attackers(color, sq))
            attacked = bool(board.attackers(not color, sq))
            if color == mover and attacked and not defended:
                mine_hanging.append(name)      # danger: it can be taken for free
            elif color != mover and not defended:
                theirs_undefended.append(name)  # target: nobody guards it
    checks = sum(1 for mv in board.legal_moves if board.gives_check(mv))
    return {
        "side": "White" if mover == chess.WHITE else "Black",
        "your_loose_pieces": mine_hanging,
        "their_loose_pieces": theirs_undefended,
        "checks_available": checks,
    }


def seed_history(token, state):
    """One startup pull of recent puzzle activity: records theme stats
    from past attempts and marks the history as seen. A state file from
    before attempt_totals existed is rebuilt from the full history, since
    its per-theme counts cannot be turned back into attempt counts."""
    rebuild = "attempt_totals" not in state
    if rebuild:
        state["theme_stats"] = {}
        state["last_puzzle_ms"] = 0
        state["attempt_totals"] = {"tries": 0, "fails": 0}
    r = rest_request("GET", API + "/api/puzzle/activity",
                     params={} if rebuild else {"max": 30},
                     headers=api_headers(token, ndjson=True))
    if r.status_code != 200:
        return 0
    entries = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    seeded = 0
    for e in entries:
        if e.get("date", 0) <= state["last_puzzle_ms"]:
            continue
        pz = e.get("puzzle") or {}
        stats_record_attempt(state, pz.get("themes", []), bool(e.get("win")))
        seeded += 1
    if entries:
        state["last_puzzle_ms"] = max(state["last_puzzle_ms"],
                                      max(e.get("date", 0) for e in entries))
    save_state(state)
    return seeded
