#!/usr/bin/env python3
"""chess-coach analysis engine: post-game debriefs for chess games.

Fetches games from lichess (or reads local PGN files), finds the critical
moments with Stockfish, and writes a plain-language coach's debrief per game
plus a recurring-habits summary across the batch.

Usage:
  python3 debrief.py <lichess-username> [options]
  python3 debrief.py <game.pgn> [more.pgn ...] [--as name] [options]

Options:
  --max N         max games to fetch from lichess (default 5)
  --since DATE    only games on/after this date, YYYY-MM-DD (lichess mode)
  --as NAME       whose games these are, for PGN files (matches White/Black header)
  --no-ai         skip the coach prose, engine report only
  --redo          re-analyze games already debriefed
  --scan-time S   seconds per move for the eval scan when the game has no
                  lichess server analysis (default 0.15)
  --deep-time S   seconds spent on each critical position (default 2.0)
  --model M       model name passed through to the claude CLI (default: CLI default)
  --out DIR       report directory (default: <project>/reports)
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import chess
import chess.engine
import chess.pgn
import requests

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
USER_AGENT = "lichess-coach/1.1.0 (github.com/heyaim/lichess-coach)"
LICHESS_API = "https://lichess.org/api/games/user/{user}"

# lichess winning-chances model: cp (White POV) -> [0, 1] for White.
WC_K = -0.00368208
MATE_CP = 10000

INACCURACY, MISTAKE, BLUNDER = 0.10, 0.20, 0.30
MAX_MOMENTS = 3

PIECE_VALUES = {chess.QUEEN: 9, chess.ROOK: 5, chess.BISHOP: 3, chess.KNIGHT: 3}


def wc_white(cp):
    """Winning chances for White, 0..1, from a White-POV centipawn eval."""
    cp = max(-1500, min(1500, cp))
    return 0.5 + 0.5 * (2 / (1 + math.exp(WC_K * cp)) - 1)


def score_to_cp(score_obj):
    """chess.engine PovScore (White POV) -> centipawns with mates clamped."""
    return score_obj.white().score(mate_score=MATE_CP)


def lichess_eval_to_cp(entry):
    """One lichess server-analysis entry -> White-POV centipawns."""
    if "mate" in entry:
        m = entry["mate"]
        return (MATE_CP - abs(m)) * (1 if m > 0 else -1)
    return entry.get("eval", 0)


def classify(delta):
    if delta >= BLUNDER:
        return "blunder"
    if delta >= MISTAKE:
        return "mistake"
    if delta >= INACCURACY:
        return "inaccuracy"
    return None


def phase_of(board, opening_ply):
    if board.ply() <= opening_ply + 2:
        return "opening"
    material = sum(
        len(board.pieces(pt, color)) * val
        for pt, val in PIECE_VALUES.items()
        for color in (chess.WHITE, chess.BLACK)
    )
    return "endgame" if material <= 13 else "middlegame"


def fmt_eval(cp):
    if cp >= MATE_CP - 100:
        return "mate for White"
    if cp <= -MATE_CP + 100:
        return "mate for Black"
    return "{:+.1f}".format(cp / 100.0)


def fmt_time_control(clock):
    """lichess clock dict -> display like '3+2' (minutes+increment)."""
    initial, inc = clock.get("initial"), clock.get("increment", 0)
    if initial is None:
        return "?"
    minutes = initial / 60
    shown = str(int(minutes)) if minutes == int(minutes) else "{:g}".format(minutes)
    return "{}+{}".format(shown, inc)


# ---------------------------------------------------------------- fetching


def fetch_lichess_games(user, max_games, since, token):
    params = {
        "max": max_games,
        "moves": "true",
        "opening": "true",
        "clocks": "true",
        "evals": "true",
    }
    if since:
        dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        params["since"] = int(dt.timestamp() * 1000)
    headers = {"Accept": "application/x-ndjson", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token

    url = LICHESS_API.format(user=user)
    for attempt in (1, 2):
        resp = requests.get(url, params=params, headers=headers, timeout=120)
        if resp.status_code == 429:
            if attempt == 2:
                sys.exit("lichess is rate limiting us twice in a row; try again later.")
            print("lichess rate limit hit; waiting the required 60 seconds...")
            time.sleep(61)
            continue
        if resp.status_code == 404:
            sys.exit("lichess returned 404 for user '{}' (bad username?).".format(user))
        resp.raise_for_status()
        break

    games = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    if len(games) == 1 and "error" in games[0] and "id" not in games[0]:
        sys.exit("lichess API error: {}".format(games[0]["error"]))
    raw_path = os.path.join(PROJECT_DIR, "data", "{}-{}.ndjson".format(user, datetime.now().strftime("%Y%m%d-%H%M%S")))
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)
    with open(raw_path, "w") as f:
        f.write(resp.text)
    return games


def json_game_to_internal(g, user):
    """Normalize one lichess NDJSON game into the internal shape."""
    if g.get("variant", "standard") != "standard":
        return None, "variant '{}' not supported yet".format(g.get("variant"))
    moves = g.get("moves", "").split()
    if len(moves) < 10:
        return None, "too short ({} plies)".format(len(moves))

    def side_name(p):
        if "user" in p:
            return p["user"]["name"]
        if "aiLevel" in p:
            return "Stockfish level {}".format(p["aiLevel"])
        return "Anonymous"

    white, black = g["players"]["white"], g["players"]["black"]
    uid = user.lower()
    if white.get("user", {}).get("id") == uid:
        color = chess.WHITE
    elif black.get("user", {}).get("id") == uid:
        color = chess.BLACK
    else:
        return None, "user not in this game"

    me, opp = (white, black) if color == chess.WHITE else (black, white)
    clock = g.get("clock") or {}
    opening = g.get("opening") or {}
    result_map = {"white": "1-0", "black": "0-1"}
    return {
        "id": g["id"],
        "site": "https://lichess.org/" + g["id"],
        "date": datetime.fromtimestamp(g["createdAt"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        "speed": g.get("speed", "?"),
        "time_control": fmt_time_control(clock) if clock else g.get("speed", "?"),
        "initial_sec": clock.get("initial"),
        "increment_sec": clock.get("increment"),
        "me": side_name(me),
        "me_rating": me.get("rating"),
        "opp": side_name(opp),
        "opp_rating": opp.get("rating"),
        "color": color,
        "result": result_map.get(g.get("winner"), "1/2-1/2" if g.get("status") == "draw" else "*"),
        "winner_color": g.get("winner"),
        "status": g.get("status"),
        "opening_name": "{} {}".format(opening.get("eco", ""), opening.get("name", "")).strip() or None,
        "opening_ply": opening.get("ply", 16),
        "moves": moves,
        "server_analysis": g.get("analysis"),
        "server_accuracy": {
            "me": me.get("analysis", {}).get("accuracy"),
            "opp": opp.get("analysis", {}).get("accuracy"),
        },
        "clocks_cs": g.get("clocks"),
    }, None


AI_NAME_RE = re.compile(r"stockfish|computer|lichess ai|maia|\bbot\b|\bengine\b", re.I)


def pgn_game_to_internal(game, as_name, source_label):
    h = game.headers
    if h.get("Variant", "Standard").lower() not in ("standard", "from position"):
        return None, "variant '{}' not supported yet".format(h.get("Variant"))

    node_moves = list(game.mainline())
    if len(node_moves) < 10:
        return None, "too short ({} plies)".format(len(node_moves))

    white_n, black_n = h.get("White", "White"), h.get("Black", "Black")
    color = None
    note = None
    if as_name:
        a = as_name.lower()
        if a in white_n.lower():
            color = chess.WHITE
        elif a in black_n.lower():
            color = chess.BLACK
        else:
            return None, "--as '{}' matches neither player".format(as_name)
    elif AI_NAME_RE.search(black_n) and not AI_NAME_RE.search(white_n):
        color = chess.WHITE
    elif AI_NAME_RE.search(white_n) and not AI_NAME_RE.search(black_n):
        color = chess.BLACK
    else:
        color = chess.WHITE
        note = "could not tell whose game this is; analyzing White (use --as to override)"

    me_n, opp_n = (white_n, black_n) if color == chess.WHITE else (black_n, white_n)

    evals, clocks, sans = [], [], []
    board = game.board()
    for node in node_moves:
        sans.append(board.san(node.move))
        board.push(node.move)
        ev = node.eval()
        if ev is None:
            evals.append(None)
        else:
            evals.append({"eval": score_to_cp(ev)} if ev.white().score() is not None
                         else {"mate": ev.white().mate()})
        ck = node.clock()
        clocks.append(int(ck * 100) if ck is not None else None)
    # lichess PGN exports omit [%eval] on the final mating move; tolerate a
    # missing tail but not gaps in the middle.
    while evals and evals[-1] is None:
        evals.pop()
    have_evals = bool(evals) and None not in evals and len(evals) >= len(sans) - 1

    site = h.get("Site", "")
    m = re.match(r"https?://lichess\.org/(\w{8})", site)
    tc = h.get("TimeControl", "?")
    initial = increment = None
    tm = re.match(r"(\d+)\+(\d+)", tc)
    if tm:
        initial, increment = int(tm.group(1)), int(tm.group(2))

    gid = m.group(1) if m else hashlib.sha1(
        (white_n + black_n + h.get("UTCDate", h.get("Date", "")) + "".join(sans)).encode()
    ).hexdigest()[:8]

    return {
        "id": gid,
        "site": "https://lichess.org/" + m.group(1) if m else None,
        "date": h.get("UTCDate", h.get("Date", "????")).replace(".", "-"),
        "speed": "pgn:" + source_label,
        "time_control": tc,
        "initial_sec": initial,
        "increment_sec": increment,
        "me": me_n,
        "me_rating": h.get("WhiteElo" if color == chess.WHITE else "BlackElo"),
        "opp": opp_n,
        "opp_rating": h.get("BlackElo" if color == chess.WHITE else "WhiteElo"),
        "color": color,
        "result": h.get("Result", "*"),
        "winner_color": {"1-0": "white", "0-1": "black"}.get(h.get("Result")),
        "status": h.get("Termination", ""),
        "opening_name": h.get("Opening") if h.get("Opening", "?") != "?" else None,
        "opening_ply": 16,
        "moves": sans,
        "server_analysis": [dict(e) for e in evals] if have_evals else None,
        "server_accuracy": {"me": None, "opp": None},
        "clocks_cs": clocks if all(c is not None for c in clocks) else None,
        "note": note,
    }, None


# ---------------------------------------------------------------- analysis


def eval_series(game, engine, scan_time):
    """White-POV centipawn eval after each ply. Server evals when present."""
    sa = game["server_analysis"]
    if sa and len(sa) >= len(game["moves"]) - 1:
        series = [lichess_eval_to_cp(e) for e in sa]
        # a finished mate has no eval entry for the final ply
        while len(series) < len(game["moves"]):
            series.append(series[-1] if series else 0)
        return series, "lichess server analysis"
    if engine is None:
        return None, None
    board = chess.Board()
    series = []
    for san in game["moves"]:
        board.push_san(san)
        if board.is_game_over():
            if board.is_checkmate():
                series.append(-MATE_CP if board.turn == chess.WHITE else MATE_CP)
            else:
                series.append(0)
            continue
        info = engine.analyse(board, chess.engine.Limit(time=scan_time))
        series.append(score_to_cp(info["score"]))
    return series, "local stockfish ({}s/move scan)".format(scan_time)


def time_spent(game, ply):
    """Seconds spent on this ply, from lichess remaining-time arrays."""
    cs = game["clocks_cs"]
    if not cs or ply >= len(cs) or game["initial_sec"] is None:
        return None
    inc = game["increment_sec"] or 0
    prev = cs[ply - 2] if ply >= 2 else game["initial_sec"] * 100
    return round(max(0.0, (prev - cs[ply] + inc * 100) / 100.0), 1)


def analyze_game(game, engine, scan_time, deep_time):
    series, source = eval_series(game, engine, scan_time)
    if series is None:
        return None

    my_color = game["color"]
    start_cp = 15  # small conventional White edge for the position before ply 0

    def wc_me(cp):
        w = wc_white(cp)
        return w if my_color == chess.WHITE else 1.0 - w

    candidates = []
    all_records = []
    counts = {"inaccuracy": 0, "mistake": 0, "blunder": 0}
    phase_errors = {"opening": 0, "middlegame": 0, "endgame": 0}
    total_loss = 0.0
    my_moves = 0

    board = chess.Board()
    for i, san in enumerate(game["moves"]):
        before_cp = series[i - 1] if i > 0 else start_cp
        mover = board.turn
        fen_before = board.fen()
        phase = phase_of(board, game["opening_ply"])
        board.push_san(san)
        after_cp = series[i]
        if mover != my_color:
            continue
        my_moves += 1
        delta = wc_me(before_cp) - wc_me(after_cp)
        total_loss += max(0.0, delta)
        label = classify(delta)
        if label:
            counts[label] += 1
            phase_errors[phase] += 1
        if delta > 0.02:
            opp_prev_delta = 0.0
            if i >= 1:
                opp_before = series[i - 2] if i > 1 else start_cp
                opp_prev_delta = wc_me(series[i - 1]) - wc_me(opp_before)
            record = {
                "ply": i,
                "move_no": i // 2 + 1,
                "san": san,
                "fen_before": fen_before,
                "label": label,
                "delta": round(delta, 3),
                "phase": phase,
                "cp_before": before_cp,
                "cp_after": after_cp,
                "missed_chance": wc_me(before_cp) >= 0.60 and opp_prev_delta >= 0.15,
                "spent_sec": time_spent(game, i),
            }
            all_records.append(record)
            if label:
                candidates.append(record)

    turning_point = max(all_records, key=lambda c: c["delta"]) if all_records else None
    candidates.sort(key=lambda c: c["delta"], reverse=True)
    moments = candidates[:MAX_MOMENTS]

    # Always include the game's single worst drop, even when it never crossed
    # the inaccuracy threshold: in a game lost gradually (or won without a
    # stumble from the opponent) it is still the closest thing to a turning
    # point, and a debrief with zero moments teaches nothing.
    if turning_point and turning_point["ply"] not in {m["ply"] for m in moments}:
        moments.append(turning_point)
    for mo in moments:
        if mo["label"] is None:
            mo["label"] = "turning point"
    moments = sorted(moments, key=lambda c: c["ply"])

    for mo in moments:
        if engine is None:
            continue
        b = chess.Board(mo["fen_before"])
        try:
            infos = engine.analyse(b, chess.engine.Limit(time=deep_time), multipv=2)
        except chess.engine.EngineTerminatedError:
            raise  # a dead engine must surface so the server can respawn it
        except chess.engine.EngineError:
            continue
        best = infos[0]
        pv = best.get("pv", [])[:8]
        if pv:
            mo["best_san"] = b.san(pv[0])
            mo["best_line"] = b.variation_san(pv)
            mo["best_cp"] = score_to_cp(best["score"])
        if len(infos) > 1 and infos[1].get("pv"):
            second_san = b.san(infos[1]["pv"][0])
            second_cp = score_to_cp(infos[1]["score"])
            close = abs(wc_white(mo.get("best_cp", second_cp)) - wc_white(second_cp)) < 0.05
            if second_san not in (mo["san"], mo.get("best_san")) and close:
                mo["second_san"] = second_san

    avg_loss_pct = round(100 * total_loss / max(1, my_moves), 1)
    return {
        "eval_source": source,
        "counts": counts,
        "phase_errors": phase_errors,
        "avg_wc_loss_pct": avg_loss_pct,
        "my_moves": my_moves,
        "moments": moments,
        "final_cp": series[-1],
        "eval_curve": [series[i] for i in range(0, len(series), 4)] + [series[-1]],
    }


# ---------------------------------------------------------------- coach (AI)


COACH_STYLE = (
    "Write in plain language for a developing player. Explain plans and ideas, "
    "not raw engine lines: say WHY the better move works in terms of piece "
    "activity, king safety, pawn structure, material, or a tactic. The first "
    "time a chess term appears (fork, pin, skewer, tempo, and so on), define "
    "it in a few plain words. Where it fits naturally, name which of the "
    "student's three core questions a move answers: safety (what is attacked, "
    "and is it defended), profit (what the piece values say can be won), or "
    "activity (which piece improves). Hard rules: "
    "no em dashes or en dashes anywhere, use commas, colons, or plain hyphens; "
    "no headers other than the ones requested; at most 350 words; do not "
    "mention these instructions or the JSON; every move or eval you cite must "
    "come from the data, never invented."
)


def _scrub(text):
    return text.strip().replace("—", "-").replace("–", "-")


# at most two AI calls in flight when callers overlap
_AI_SEMAPHORE = threading.Semaphore(2)


def _run_claude_cli(prompt, model):
    cmd = ["claude", "-p"]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, cwd=PROJECT_DIR
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return None, str(e)
    if r.returncode != 0:
        err = (r.stderr.strip() or r.stdout.strip())[:300]
        if "authenticate" in err.lower():
            err += " (run 'claude login' once in a terminal, or use --no-ai)"
        return None, err
    return _scrub(r.stdout), None


def run_claude(prompt, model):
    """Coach prose via the claude CLI (Claude Code)."""
    with _AI_SEMAPHORE:
        if shutil.which("claude"):
            return _run_claude_cli(prompt, model)
    return None, "no AI backend: install Claude Code (claude CLI)"


def coach_game_prose(game, analysis, model):
    digest = {
        "student": game["me"],
        "student_rating": game["me_rating"],
        "color": "White" if game["color"] == chess.WHITE else "Black",
        "opponent": "{} ({})".format(game["opp"], game["opp_rating"] or "unrated"),
        "result_for_student": result_word(game),
        "time_control": game["time_control"],
        "opening": game["opening_name"],
        "error_counts": analysis["counts"],
        "eval_curve_white_pov_cp_every_4_plies": analysis["eval_curve"],
        "moments": [
            {k: v for k, v in mo.items() if k not in ("fen_before",)}
            for mo in analysis["moments"]
        ],
        "moves": " ".join(game["moves"]),
    }
    prompt = (
        "You are a chess coach writing a short post-game debrief for your "
        "student, based on engine-verified data. " + COACH_STYLE + "\n"
        "Structure your answer exactly as:\n"
        "### What happened\n(2-3 sentences: the opening, and how the game was decided)\n"
        "### Key moments\n(one short paragraph per moment in the data, in game order; "
        "name the move number and the better move, then the idea behind it; if a "
        "moment is marked missed_chance, frame it as a chance not taken; if "
        "spent_sec shows a snap decision on a critical position, say so)\n"
        "### One thing to work on\n(a single concrete habit or drill drawn from THIS game)\n\n"
        "GAME DATA:\n" + json.dumps(digest)
    )
    return run_claude(prompt, model)


def coach_summary_prose(entries, model):
    digests = []
    for game, analysis in entries:
        digests.append({
            "game": game["id"],
            "date": game["date"],
            "color": "White" if game["color"] == chess.WHITE else "Black",
            "opponent_rating": game["opp_rating"],
            "result_for_student": result_word(game),
            "opening": game["opening_name"],
            "error_counts": analysis["counts"],
            "phase_errors": analysis["phase_errors"],
            "moments": [
                {k: mo.get(k) for k in ("move_no", "san", "label", "phase", "best_san", "missed_chance", "spent_sec")}
                for mo in analysis["moments"]
            ],
        })
    prompt = (
        "You are a chess coach reviewing a batch of your student's recent games, "
        "using engine-verified data. " + COACH_STYLE + "\n"
        "Structure your answer exactly as:\n"
        "### Recurring habits\n(2-3 patterns you actually see in the data, each "
        "backed by specific games and moves; a pattern needs at least two occurrences)\n"
        "### This week's focus\n(at most 3 concrete practice items aimed at those habits)\n\n"
        "DATA:\n" + json.dumps(digests)
    )
    return run_claude(prompt, model)


def result_word(game):
    if game["winner_color"] is None:
        return "draw"
    won = (game["winner_color"] == "white") == (game["color"] == chess.WHITE)
    return "won" if won else "lost"


# ---------------------------------------------------------------- reports


def board_image_url(fen, my_color, last_move_uci=None):
    q = "fen=" + fen.replace(" ", "%20")
    if last_move_uci:
        q += "&lastMove=" + last_move_uci
    if my_color == chess.BLACK:
        q += "&color=black"
    return "https://lichess.org/export/fen.gif?" + q


def analysis_link(fen):
    return "https://lichess.org/analysis/" + fen.replace(" ", "_")


def game_report_md(game, analysis, prose, prose_err):
    my_color_name = "White" if game["color"] == chess.WHITE else "Black"
    lines = []
    lines.append("# {} ({}) vs {} ({})".format(
        game["me"], game["me_rating"] or "?", game["opp"], game["opp_rating"] or "?"))
    lines.append("")
    meta = "**{}** as {} | {} {} | {}".format(
        result_word(game).upper(), my_color_name, game["speed"],
        game["time_control"], game["date"])
    if game["opening_name"]:
        meta += " | " + game["opening_name"]
    lines.append(meta)
    acc = game["server_accuracy"]["me"]
    counts = analysis["counts"]
    stat = "{} blunders, {} mistakes, {} inaccuracies".format(
        counts["blunder"], counts["mistake"], counts["inaccuracy"])
    if acc:
        stat += " | lichess accuracy {}%".format(acc)
    lines.append("")
    lines.append(stat + "  \n*evals: {}*".format(analysis["eval_source"]))
    if game["site"]:
        lines.append("")
        lines.append("[Open on lichess]({}) | [replay from your side]({})".format(
            game["site"], game["site"] + "/" + my_color_name.lower()))
    if game.get("note"):
        lines.append("")
        lines.append("> note: " + game["note"])
    lines.append("")

    if prose:
        lines.append("## Coach's debrief")
        lines.append("")
        lines.append(prose)
        lines.append("")
    elif prose_err:
        lines.append("*(coach prose skipped: {})*".format(prose_err))
        lines.append("")

    lines.append("## Critical moments (engine)")
    lines.append("")
    if not analysis["moments"]:
        lines.append("No move lost meaningful winning chances. Clean game.")
    for mo in analysis["moments"]:
        move_label = "{}{}".format(mo["move_no"], "." if mo["ply"] % 2 == 0 else "...")
        title = "### Move {} {} : {}".format(move_label, mo["san"], mo["label"])
        if mo.get("missed_chance"):
            title += " (missed chance)"
        lines.append(title)
        lines.append("")
        b = chess.Board(mo["fen_before"])
        try:
            played_uci = b.parse_san(mo["san"]).uci()
        except ValueError:
            played_uci = None
        lines.append("![position before move {}]({})".format(
            move_label, board_image_url(mo["fen_before"], game["color"], played_uci)))
        lines.append("")
        detail = "Winning chances lost: **{}%** ({} to {}). Phase: {}.".format(
            int(round(mo["delta"] * 100)), fmt_eval(mo["cp_before"]),
            fmt_eval(mo["cp_after"]), mo["phase"])
        if mo.get("spent_sec") is not None:
            detail += " Time spent: {}s.".format(round(mo["spent_sec"], 1))
        lines.append(detail)
        lines.append("")
        if mo.get("best_san"):
            better = "Better was **{}** ({}): `{}`".format(
                mo["best_san"], fmt_eval(mo["best_cp"]), mo["best_line"])
            if mo.get("second_san") and mo["second_san"] != mo["best_san"]:
                better += " (also fine: {})".format(mo["second_san"])
            lines.append(better)
            lines.append("")
        links = "[explore this position]({})".format(analysis_link(mo["fen_before"]))
        if game["site"]:
            links += " | [this moment on lichess]({}#{})".format(game["site"], mo["ply"])
        lines.append(links)
        lines.append("")

    return "\n".join(lines)


def summary_md(user, entries, prose, prose_err):
    lines = ["# Debrief summary: {} ({} game{})".format(
        user, len(entries), "" if len(entries) == 1 else "s"), ""]
    lines.append("| game | date | color | vs | result | opening | errors (B/M/I) | worst moment |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for game, analysis in entries:
        c = analysis["counts"]
        worst = "-"
        if analysis["moments"]:
            w = max(analysis["moments"], key=lambda m: m["delta"])
            worst = "{}{} {} (-{}%)".format(
                w["move_no"], "." if w["ply"] % 2 == 0 else "...",
                w["san"], int(round(w["delta"] * 100)))
        lines.append("| [{}]({}) | {} | {} | {} ({}) | {} | {} | {}/{}/{} | {} |".format(
            game["id"], "./" + report_filename(game), game["date"],
            "W" if game["color"] == chess.WHITE else "B",
            game["opp"], game["opp_rating"] or "?", result_word(game),
            game["opening_name"] or "?", c["blunder"], c["mistake"], c["inaccuracy"], worst))
    lines.append("")
    if prose:
        lines.append(prose)
    elif prose_err:
        lines.append("*(habits prose skipped: {})*".format(prose_err))
    lines.append("")
    return "\n".join(lines)


def report_filename(game):
    return "{}-{}.md".format(game["date"], game["id"])


# ---------------------------------------------------------------- main


def load_seen(out_dir):
    p = os.path.join(out_dir, ".seen.json")
    if os.path.exists(p):
        with open(p) as f:
            return set(json.load(f))
    return set()


def save_seen(out_dir, seen):
    with open(os.path.join(out_dir, ".seen.json"), "w") as f:
        json.dump(sorted(seen), f)


def main():
    ap = argparse.ArgumentParser(description="Post-game chess debriefs.")
    ap.add_argument("target", nargs="+", help="lichess username, or PGN file(s)")
    ap.add_argument("--max", type=int, default=5)
    ap.add_argument("--since")
    ap.add_argument("--as", dest="as_name")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--scan-time", type=float, default=0.15)
    ap.add_argument("--deep-time", type=float, default=2.0)
    ap.add_argument("--model")
    ap.add_argument("--out")
    args = ap.parse_args()

    pgn_mode = all(os.path.exists(t) for t in args.target)
    if not pgn_mode and len(args.target) > 1:
        sys.exit("Pass one lichess username, or one or more existing PGN files.")

    games = []
    if pgn_mode:
        for path in args.target:
            with open(path) as f:
                while True:
                    g = chess.pgn.read_game(f)
                    if g is None:
                        break
                    internal, skip = pgn_game_to_internal(
                        g, args.as_name, os.path.basename(path))
                    if internal:
                        games.append(internal)
                    else:
                        print("skipping a game in {}: {}".format(path, skip))
        user = args.as_name or (games[0]["me"] if games else "pgn")
    else:
        user = args.target[0]
        print("fetching up to {} games for {} from lichess...".format(args.max, user))
        raw = fetch_lichess_games(user, args.max, args.since, os.environ.get("LICHESS_TOKEN"))
        if not raw:
            sys.exit("lichess returned no games for '{}'. If your practice games are "
                     "played while logged out or against the computer offline, export "
                     "them as PGN and run: python3 debrief.py mygames.pgn "
                     "--as {}".format(user, user))
        for g in raw:
            internal, skip = json_game_to_internal(g, user)
            if internal:
                games.append(internal)
            else:
                print("skipping game {}: {}".format(g.get("id"), skip))

    if not games:
        sys.exit("No analyzable games.")

    out_dir = args.out or os.path.join(PROJECT_DIR, "reports", user.lower())
    os.makedirs(out_dir, exist_ok=True)
    seen = load_seen(out_dir)
    todo = [g for g in games if args.redo or g["id"] not in seen]
    if not todo:
        sys.exit("All {} fetched games are already debriefed (use --redo to regenerate).".format(len(games)))

    engine = None
    engine_path = shutil.which("stockfish")
    if engine_path:
        engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    else:
        print("warning: stockfish not found; games without lichess server analysis "
              "will be skipped, and best-move lines will be missing.")

    use_ai = not args.no_ai and shutil.which("claude")
    if not args.no_ai and not use_ai:
        print("warning: claude CLI not found; writing engine-only reports.")

    entries = []
    try:
        for i, game in enumerate(todo, 1):
            print("[{}/{}] analyzing {} vs {} ({})...".format(
                i, len(todo), game["me"], game["opp"], game["id"]))
            analysis = analyze_game(game, engine, args.scan_time, args.deep_time)
            if analysis is None:
                print("  no evals available (needs stockfish or lichess analysis); skipped.")
                continue
            prose = prose_err = None
            if use_ai:
                print("  writing coach's debrief...")
                prose, prose_err = coach_game_prose(game, analysis, args.model)
            path = os.path.join(out_dir, report_filename(game))
            with open(path, "w") as f:
                f.write(game_report_md(game, analysis, prose, prose_err))
            print("  -> {}".format(path))
            seen.add(game["id"])
            entries.append((game, analysis))
    finally:
        if engine:
            engine.quit()

    if entries:
        prose = prose_err = None
        if use_ai and len(entries) >= 2:
            print("looking for recurring habits across {} games...".format(len(entries)))
            prose, prose_err = coach_summary_prose(entries, args.model)
        spath = os.path.join(out_dir, "summary-{}.md".format(datetime.now().strftime("%Y-%m-%d")))
        with open(spath, "w") as f:
            f.write(summary_md(user, entries, prose, prose_err))
        print("summary -> {}".format(spath))
    save_seen(out_dir, seen)


if __name__ == "__main__":
    main()
