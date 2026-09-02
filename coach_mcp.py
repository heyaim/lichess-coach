#!/usr/bin/env python3
"""chess-coach MCP server: turns Claude into a chess coach with access to
your lichess games, puzzle history, and theme statistics.

Speaks the Model Context Protocol over stdio (newline-delimited JSON-RPC).
No dependencies beyond the project's own: python-chess and requests.
Claude itself does the narrating; there are no nested AI calls here.

Register with:  claude mcp add --scope user chess-coach -- /path/to/lichess-coach/coach-mcp
"""

import json
import os
import shutil
import sys
import time

import chess
import chess.engine

import core
import debrief

# stdout carries the protocol; anything else that prints must not touch it.
_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr

SERVER_INFO = {"name": "chess-coach", "version": "1.1.0"}
PROTOCOL_VERSION = "2024-11-05"

_engine = None
_account = {}
_last_sync = [0.0]


def get_engine():
    global _engine
    if _engine is None:
        path = shutil.which("stockfish")
        if path:
            _engine = chess.engine.SimpleEngine.popen_uci(path)
    return _engine


def _reset_engine():
    global _engine
    _engine = None


def _int_arg(args, key, default, lo, hi):
    """Integer argument clamped to [lo, hi]; a non-number raises ValueError
    with a plain message for the caller to return."""
    try:
        return max(lo, min(int(args.get(key, default)), hi))
    except (TypeError, ValueError):
        raise ValueError("{} must be a whole number".format(key))


def get_account():
    if not _account:
        token = core.get_token()
        if token:
            r = core.rest_request("GET", core.API + "/api/account",
                                   headers=core.api_headers(token))
            if r.status_code == 200:
                _account.update(r.json())
    return _account


def sync_puzzles(force=False):
    """Pull fresh puzzle activity into the tracked stats (throttled)."""
    if not force and time.time() - _last_sync[0] < 60:
        return {"synced": False, "reason": "recent sync; using cached stats"}
    token = core.get_token()
    if not token:
        return {"synced": False, "reason": "no lichess token"}
    state = core.load_state()
    seen = core.seed_history(token, state)
    _last_sync[0] = time.time()
    return {"synced": True, "new_attempts": seen}


# ------------------------------------------------------------------ tools


def tool_setup_check(args):
    out = {"stockfish": bool(shutil.which("stockfish"))}
    token = core.get_token()
    out["lichess_token"] = bool(token)
    if token:
        r = core.rest_request("POST", core.API + "/api/token/test",
                               data=token, headers=core.api_headers(None))
        info = (r.json() or {}).get(token) if r.status_code == 200 else None
        if info:
            out["lichess_user"] = info.get("userId")
            out["token_scopes"] = info.get("scopes")
        else:
            out["lichess_token"] = "present but rejected"
    else:
        out["how_to_get_token"] = core.TOKEN_CREATE_URL
    acct = get_account()
    if acct:
        perfs = acct.get("perfs", {})
        out["ratings"] = {
            k: {"rating": v.get("rating"), "games": v.get("games")}
            for k, v in perfs.items()
            if isinstance(v, dict) and v.get("games")}
        out["total_games"] = (acct.get("count") or {}).get("all")
    state = core.load_state()
    out["themes_tracked"] = len(state.get("theme_stats", {}))
    return out


def tool_recent_games(args):
    acct = get_account()
    if not acct:
        return {"error": "no lichess account connected; run setup_check"}
    token = core.get_token()
    try:
        limit = _int_arg(args, "max", 5, 1, 100)
    except ValueError as e:
        return {"error": str(e)}
    r = core.rest_request(
        "GET", core.API + "/api/games/user/" + acct["username"],
        params={"max": limit, "opening": "true",
                "moves": "true"},
        headers=core.api_headers(token, ndjson=True))
    if r.status_code != 200:
        return {"error": "lichess returned HTTP {}".format(r.status_code)}
    games = []
    for line in r.text.splitlines():
        if not line.strip():
            continue
        try:
            g = json.loads(line)
        except ValueError:
            continue
        me_white = g["players"]["white"].get("user", {}).get("id") == acct["id"]
        opp = g["players"]["black" if me_white else "white"]
        games.append({
            "game_id": g["id"],
            "when": time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(g["createdAt"] / 1000)),
            "my_color": "white" if me_white else "black",
            "opponent": opp.get("user", {}).get("name")
            or ("Stockfish level {}".format(opp["aiLevel"])
                if "aiLevel" in opp else "Anonymous"),
            "speed": g.get("speed"),
            "winner": g.get("winner", "draw/none"),
            "opening": (g.get("opening") or {}).get("name"),
            "plies": len(g.get("moves", "").split()),
        })
    if not games:
        return {"games": [], "note": "no games on this account yet - puzzles "
                "only. A first casual game vs the computer would give the "
                "coach real material."}
    return {"games": games}


def tool_analyze_game(args):
    game_id = str(args.get("game_id", "")).strip()
    if not game_id:
        return {"error": "game_id required"}
    token = core.get_token()
    r = core.rest_request(
        "GET", core.API + "/game/export/" + game_id,
        params={"moves": "true", "clocks": "true", "evals": "true",
                "opening": "true"},
        headers={**core.api_headers(token), "Accept": "application/json"},
        timeout=60)
    if r.status_code != 200:
        return {"error": "could not fetch game {} (HTTP {})".format(
            game_id, r.status_code)}
    g = r.json()
    # lichess fair play forbids outside help during a game; analysis waits
    # until the game is over, enforced here rather than left to good manners
    if g.get("status") in ("created", "started"):
        return {"error": "this game is still in progress. Lichess fair play "
                "forbids outside analysis of ongoing games, so the debrief "
                "unlocks the moment it ends."}
    acct = get_account()
    perspective = args.get("perspective", "auto")
    user = acct.get("username", "") if perspective == "auto" else None
    if perspective in ("white", "black"):
        side = g["players"][perspective]
        user = side.get("user", {}).get("name") or "__side__"
        if "user" not in side:
            side["user"] = {"id": user.lower(), "name": user}
    internal, skip = debrief.json_game_to_internal(g, user or "")
    if not internal:
        return {"error": "cannot analyze: {}. Pass perspective='white' or "
                "'black' to pick a seat.".format(skip)}
    engine = get_engine()
    analysis = debrief.analyze_game(internal, engine, 0.15, 2.0)
    if analysis is None:
        return {"error": "no evals available (stockfish missing and game "
                "has no server analysis)"}
    out_dir = debrief.PROJECT_DIR + "/reports/" + (user or "games").lower()
    os.makedirs(out_dir, exist_ok=True)
    path = out_dir + "/" + debrief.report_filename(internal)
    with open(path, "w") as f:
        f.write(debrief.game_report_md(internal, analysis, None, None))
    return {
        "player": internal["me"], "color":
            "white" if internal["color"] == chess.WHITE else "black",
        "opponent": internal["opp"], "result": debrief.result_word(internal),
        "opening": internal["opening_name"],
        "error_counts": analysis["counts"],
        "errors_by_phase": analysis["phase_errors"],
        "eval_curve_white_pov_cp_every_4_plies": analysis["eval_curve"],
        "critical_moments": [
            {k: v for k, v in m.items() if k != "fen_before"}
            for m in analysis["moments"]],
        "moment_positions": [
            {"move": m["san"], "fen_before": m["fen_before"],
             "explore": debrief.analysis_link(m["fen_before"])}
            for m in analysis["moments"]],
        "moves": " ".join(internal["moves"]),
        "report_file": path,
        "game_url": internal.get("site"),
    }


def tool_puzzle_report(args):
    sync = sync_puzzles()
    state = core.load_state()
    ts = state.get("theme_stats", {})
    skip = {"short", "long", "oneMove", "veryLong", "master",
            "masterVsMaster", "superGM"}
    rows = [(t, d["tries"], d["fails"], round(100.0 * d["fails"] / d["tries"]))
            for t, d in ts.items() if t not in skip and d["tries"] >= 10]
    weakest = sorted(rows, key=lambda x: -x[3])[:8]
    strongest = sorted(rows, key=lambda x: x[3])[:5]
    totals = state.get("attempt_totals", {"tries": 0, "fails": 0})
    total_tries = totals["tries"]
    total_fails = totals["fails"]
    return {
        "sync": sync,
        "lifetime_recorded_attempts": total_tries,
        "lifetime_solve_pct": round(100.0 * (total_tries - total_fails)
                                    / max(1, total_tries)),
        "weakest_themes": [
            {"theme": t, "seen": n, "missed": f, "miss_pct": p,
             "replay_your_fails": "https://lichess.org/training/replay/30/" + t,
             "train_more": "https://lichess.org/training/" + t}
            for t, n, f, p in weakest],
        "strongest_themes": [
            {"theme": t, "seen": n, "missed": f, "miss_pct": p}
            for t, n, f, p in strongest],
        "weakness_dashboard": "https://lichess.org/training/dashboard/30",
    }


def tool_explain_puzzle(args):
    ref = str(args.get("puzzle", "")).strip()
    m = core.PUZZLE_URL_RE.search(ref)
    pid = m.group(1) if m else ref
    if not core.PUZZLE_ID_RE.match(pid):
        return {"error": "pass a lichess puzzle id or training URL"}
    digest = core.puzzle_digest(core.fetch_puzzle(pid), get_engine())
    digest["training_url"] = "https://lichess.org/training/" + pid
    return digest


def tool_explain_position(args):
    fen = str(args.get("fen", "")).strip()
    if len(fen) > 100:
        return {"error": "not a FEN: real ones are under 100 characters"}
    goal = args.get("goal")
    try:
        board = chess.Board(fen)
    except ValueError as e:
        return {"error": "bad FEN: {}".format(e)}
    out = {"fen": fen,
           "side_to_move": "white" if board.turn == chess.WHITE else "black",
           "explore": debrief.analysis_link(fen)}
    try:
        out["quick_scan"] = core.quick_scan(fen)
    except ValueError:
        pass
    engine = get_engine()
    if engine and board.king(chess.WHITE) is not None \
            and board.king(chess.BLACK) is not None:
        infos = engine.analyse(board, chess.engine.Limit(time=2.0), multipv=3)
        out["engine_lines"] = [
            {"line": board.variation_san(i.get("pv", [])[:6]),
             "eval": debrief.fmt_eval(debrief.score_to_cp(i["score"]))}
            for i in infos if i.get("pv")]
        if goal and "check" in str(goal).lower():
            checks = []
            for mv in board.legal_moves:
                if board.gives_check(mv):
                    checks.append(board.san(mv))
                if len(checks) >= 10:
                    break
            out["checking_moves"] = checks
    if goal:
        out["exercise_goal"] = goal
    return out


def tool_failed_puzzles(args):
    token = core.get_token()
    if not token:
        return {"error": "no lichess token; run setup_check"}
    try:
        limit = _int_arg(args, "max", 30, 1, 100)
    except ValueError as e:
        return {"error": str(e)}
    r = core.rest_request("GET", core.API + "/api/puzzle/activity",
                          params={"max": 1000},
                          headers=core.api_headers(token, ndjson=True))
    if r.status_code != 200:
        return {"error": "lichess returned HTTP {}".format(r.status_code)}
    fails, solved_later = {}, set()
    for line in r.text.splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        pz = e.get("puzzle") or {}
        pid = pz.get("id")
        if not pid:
            continue
        if e.get("win"):
            solved_later.add(pid)
        elif pid not in fails:
            fails[pid] = {
                "id": pid,
                "failed_on": time.strftime("%Y-%m-%d",
                                           time.gmtime(e.get("date", 0) / 1000)),
                "themes": pz.get("themes") or [],
                "attempt_url": "https://lichess.org/training/" + pid,
                "solved_since": pid in solved_later,
            }
    out = list(fails.values())[:limit]
    theme_counts = {}
    for f in fails.values():
        for t in f["themes"]:
            theme_counts[t] = theme_counts.get(t, 0) + 1
    top_themes = sorted(theme_counts.items(), key=lambda kv: -kv[1])[:10]
    return {"failed_puzzles": out, "total_recorded_fails": len(fails),
            "themes_among_fails": [{"theme": t, "misses": n} for t, n in top_themes],
            "note": "newest first, from the most recent 1000 recorded "
                    "attempts; use explain_puzzle on an id for the "
                    "engine-verified walkthrough"}


def tool_recent_activity(args):
    acct = get_account()
    if not acct:
        return {"error": "no lichess account connected; run setup_check"}
    r = core.rest_request("GET", core.API + "/api/user/" + acct["username"] + "/activity",
                          headers=core.api_headers(core.get_token()))
    if r.status_code != 200:
        return {"error": "lichess returned HTTP {}".format(r.status_code)}
    try:
        feed = r.json()
    except ValueError:
        feed = None
    if not isinstance(feed, list):
        return {"error": "unexpected activity response from lichess"}
    days = []
    for d in feed:
        if not isinstance(d, dict):
            continue
        day = {"date": time.strftime(
            "%Y-%m-%d",
            time.gmtime((d.get("interval") or {}).get("start", 0) / 1000))}
        score = (d.get("puzzles") or {}).get("score") or {}
        if score:
            rp = score.get("rp") or {}
            day["puzzles"] = {
                "attempts": score.get("win", 0) + score.get("loss", 0),
                "wins": score.get("win", 0),
                "losses": score.get("loss", 0),
                "rating_before": rp.get("before"),
                "rating_after": rp.get("after"),
            }
        practice = d.get("practice")
        if practice:
            day["practice"] = [
                {"name": p.get("name"), "positions": p.get("nbPositions"),
                 "url": p.get("url")} for p in practice]
        if d.get("storm"):
            day["storm"] = d["storm"]
        if d.get("games"):
            day["games"] = d["games"]
        days.append(day)
    return {"days": days,
            "note": "daily puzzle totals include unrated and replayed attempts; "
                    "the per-theme statistics in puzzle_report track rated "
                    "solves only"}


def tool_puzzle_dashboard(args):
    token = core.get_token()
    if not token:
        return {"error": "no lichess token; run setup_check"}
    try:
        days = _int_arg(args, "days", 30, 1, 365)
    except ValueError as e:
        return {"error": str(e)}
    r = core.rest_request("GET", core.API + "/api/puzzle/dashboard/{}".format(days),
                          headers=core.api_headers(token))
    if r.status_code != 200:
        return {"error": "lichess returned HTTP {}".format(r.status_code)}
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"error": "unexpected dashboard response from lichess"}

    def trim(res):
        res = res if isinstance(res, dict) else {}
        return {"attempts": res.get("nb", 0),
                "first_try_wins": res.get("firstWins", 0),
                "replay_wins": res.get("replayWins", 0),
                "performance": res.get("performance"),
                "avg_puzzle_rating": res.get("puzzleRatingAvg")}

    themes = []
    for key, t in (data.get("themes") or {}).items():
        if not isinstance(t, dict):
            continue
        row = {"theme": key, "name": t.get("theme")}
        row.update(trim(t.get("results")))
        themes.append(row)
    rated = sorted((t for t in themes if t["performance"] is not None),
                   key=lambda t: t["performance"])
    return {
        "days": days,
        "overall": trim(data.get("global")),
        "weakest_by_performance": rated[:8],
        "strongest_by_performance": list(reversed(rated[-5:])),
        "dashboard_url": "https://lichess.org/training/dashboard/{}".format(days),
        "note": "lichess's own dashboard figures; replay_wins counts misses "
                "later solved in replay, the one place replay outcomes are "
                "recorded",
    }


def tool_save_note(args):
    title = str(args.get("title", "note")).strip()[:80]
    text = str(args.get("text", "")).strip()[:4000]
    if not text:
        return {"error": "text required"}
    acct = get_account()
    user = acct.get("username", "student")
    path = core.append_lesson(user, "## {}\n\n{}".format(title, text))
    return {"saved_to": path}


TOOLS = [
    ("setup_check", "Check what is connected: lichess token, account, "
     "Stockfish, tracked stats. Run this first in a session.",
     {"type": "object", "properties": {}}, tool_setup_check),
    ("recent_games", "List the student's recent lichess games with ids for "
     "analyze_game.",
     {"type": "object", "properties": {
         "max": {"type": "integer", "description": "how many (default 5)"}}},
     tool_recent_games),
    ("analyze_game", "Full engine debrief of one game: error counts, "
     "critical moments with best lines, eval curve. Narrate from this data "
     "only - never invent moves or evals.",
     {"type": "object", "required": ["game_id"], "properties": {
         "game_id": {"type": "string"},
         "perspective": {"type": "string",
                         "enum": ["auto", "white", "black"]}}},
     tool_analyze_game),
    ("puzzle_report", "The student's puzzle training report: volume, solve "
     "rate, weakest and strongest themes. Syncs latest activity first.",
     {"type": "object", "properties": {}}, tool_puzzle_report),
    ("puzzle_dashboard", "Lichess's own puzzle dashboard for the last N "
     "days: per-theme performance, first-try wins, and replay wins, the one "
     "record of how replays went. Weakest and strongest themes by "
     "performance.",
     {"type": "object", "properties": {
         "days": {"type": "integer",
                  "description": "window in days (default 30)"}}},
     tool_puzzle_dashboard),
    ("recent_activity", "The student's recent lichess activity by day: total "
     "puzzle attempts including unrated replays, practice sessions by name, "
     "Puzzle Storm, and games. Broader than puzzle_report's rated-only "
     "record.",
     {"type": "object", "properties": {}}, tool_recent_activity),
    ("failed_puzzles", "The student's recently failed puzzles with ids, "
     "themes, and links: the material behind lichess's replay queue. Use "
     "explain_puzzle on an id to coach one.",
     {"type": "object", "properties": {
         "max": {"type": "integer",
                 "description": "how many fails to return (default 30)"}}},
     tool_failed_puzzles),
    ("explain_puzzle", "Engine-verified digest of a lichess puzzle: solution "
     "with per-move facts, themes, and the tempting wrong move refuted.",
     {"type": "object", "required": ["puzzle"], "properties": {
         "puzzle": {"type": "string",
                    "description": "puzzle id or lichess.org/training URL"}}},
     tool_explain_puzzle),
    ("explain_position", "Engine facts about any position: hanging pieces "
     "scan, best lines, checking moves when the goal involves check.",
     {"type": "object", "required": ["fen"], "properties": {
         "fen": {"type": "string"},
         "goal": {"type": "string",
                  "description": "the exercise's stated goal, if any"}}},
     tool_explain_position),
    ("save_note", "Append a coaching insight to the student's lessons file "
     "so it persists between sessions.",
     {"type": "object", "required": ["text"], "properties": {
         "title": {"type": "string"}, "text": {"type": "string"}}},
     tool_save_note),
]


# --------------------------------------------------------------- protocol


def send(msg):
    _PROTOCOL_OUT.write(json.dumps(msg, allow_nan=False) + "\n")
    _PROTOCOL_OUT.flush()


def _reject_constant(name):
    raise ValueError("non-finite number in message: " + name)


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None and method != "notifications/initialized":
        # no id means a notification: execute nothing, reply never
        return
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO}})
    elif method == "notifications/initialized":
        pass
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": d, "inputSchema": s}
            for n, d, s, _ in TOOLS]}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = next((f for n, _, _, f in TOOLS if n == name), None)
        if fn is None:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps({"error": "unknown tool"})}],
                "isError": True}})
            return
        try:
            try:
                result = fn(args)
            except chess.engine.EngineTerminatedError:
                _reset_engine()  # Stockfish died; respawn on the retry
                result = fn(args)
        except Exception as e:  # tool errors go back as data, never crash
            result = {"error": "{}: {}".format(type(e).__name__, str(e)[:200])}
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": bool(isinstance(result, dict) and result.get("error"))}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid,
              "error": {"code": -32601, "message": "method not found"}})


def main():
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                if not isinstance(m, dict):
                    continue
                try:
                    handle(m)
                except Exception as e:
                    sys.stderr.write("chess-coach mcp error: {}\n".format(e))
    finally:
        # atexit cannot do this: Python joins the engine's non-daemon
        # thread before atexit handlers run, so quit must happen here.
        if _engine is not None:
            try:
                _engine.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
