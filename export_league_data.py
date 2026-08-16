"""
Pull your ESPN fantasy basketball league into the JSON shape the
League Pulse dashboard expects, and print it to stdout.

Setup:
    pip install espn_api

Find your credentials:
    league_id  -> the number in your league's ESPN URL (?leagueId=123456)
    swid       -> log into your league on espn.com, open dev tools ->
                  Application/Storage -> Cookies -> espn.com -> "swid"
    espn_s2    -> same place, the "espn_s2" cookie
    (swid/espn_s2 only needed for private leagues)

Usage:
    python export_league_data.py --league-id 123456 --year 2026 \
        --swid "{XXXX-XXXX}" --espn-s2 "AEB..." > league.json

Then paste the contents of league.json into the "Paste my league JSON"
box in the dashboard.

Troubleshooting the player stat lookup:
    ESPN's internal player-stat JSON shape is undocumented and has
    shifted across seasons/espn_api versions. If the "playerValues"
    section of your export comes out mostly empty, run with
    --debug-player "Some Player Name" to print that player's raw
    stats object so you can see what keys are actually available and
    adjust STAT_NAME_VARIANTS / extract_player_averages() below.
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict

from espn_api.basketball import League

CATEGORIES = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "FG%", "FT%", "TO"]
LOWER_BETTER = {"TO"}

# Different espn_api versions / seasons have used slightly different key
# spellings for the same category inside player.stats. Add more variants
# here if --debug-player shows something not covered.
STAT_NAME_VARIANTS = {
    "PTS": ["PTS", "points"],
    "REB": ["REB", "rebounds"],
    "AST": ["AST", "assists"],
    "STL": ["STL", "steals"],
    "BLK": ["BLK", "blocks"],
    "3PM": ["3PM", "threePointersMade", "3PTM"],
    "FG%": ["FG%", "fieldGoalPercentage"],
    "FT%": ["FT%", "freeThrowPercentage"],
    "TO": ["TO", "turnovers"],
}


# ---------- Weekly category lines + real schedule ----------

def normalize_stat_dict(raw_stats):
    """espn_api's box_score home_stats/away_stats is usually
    {category_label: {'score': value, 'result': 'WIN'/'LOSS'/'TIE'}, ...}
    but has also shown up as a flat {category_label: value} in some
    versions. Handle both."""
    out = {}
    if not raw_stats:
        return out
    for cat in CATEGORIES:
        val = None
        for label, entry in raw_stats.items():
            if label.upper().replace(" ", "") != cat.upper().replace(" ", ""):
                continue
            if isinstance(entry, dict):
                val = entry.get("value", entry.get("score"))
            else:
                val = entry
            break
        if val is not None:
            out[cat] = float(val)
    return out


def count_games_played(lineup):
    """Best-effort count of games played by a team's active lineup for
    the week. espn_api doesn't expose a clean per-team weekly
    games-played total, so this counts starters (non-bench, non-IR
    slots) as a proxy. If your espn_api version exposes something like
    player.game_played or a daily lineup breakdown, swap that in here
    for a more accurate number — this is the roughest part of the export."""
    if not lineup:
        return 0
    count = 0
    for p in lineup:
        slot = getattr(p, "slot_position", "") or ""
        if slot.upper() not in ("BE", "IR", "BENCH"):
            count += 1
    return count


def build_weekly_data(league, teams_by_id, current_week):
    """Returns (schedule, weekly_by_team_id) using real box scores per
    week — this replaces any naive/placeholder pairing."""
    schedule = []
    weekly_by_team = defaultdict(list)

    for week in range(1, current_week + 1):
        try:
            box_scores = league.box_scores(week)
        except Exception as e:
            print(f"warning: couldn't fetch box scores for week {week}: {e}", file=sys.stderr)
            break

        pairs = []
        for matchup in box_scores:
            home_id = getattr(matchup.home_team, "team_id", None)
            away_id = getattr(matchup.away_team, "team_id", None)
            if home_id is None or away_id is None or home_id not in teams_by_id or away_id not in teams_by_id:
                continue  # bye week or playoff bracket gap

            home_cats = normalize_stat_dict(getattr(matchup, "home_stats", None))
            away_cats = normalize_stat_dict(getattr(matchup, "away_stats", None))

            # fall back to zeros for any category we couldn't parse, so the
            # dashboard doesn't crash — but this means that category will
            # look artificially low; check stderr warnings if this happens often
            for cat in CATEGORIES:
                home_cats.setdefault(cat, 0)
                away_cats.setdefault(cat, 0)

            home_cats["games"] = count_games_played(getattr(matchup, "home_lineup", None))
            away_cats["games"] = count_games_played(getattr(matchup, "away_lineup", None))

            weekly_by_team[home_id].append(home_cats)
            weekly_by_team[away_id].append(away_cats)
            pairs.append([home_id, away_id])

        if pairs:
            schedule.append(pairs)

    return schedule, weekly_by_team


# ---------- Player z-score values ----------

def extract_player_averages(player, year):
    """Digs through player.stats (shape varies by espn_api version) to
    find a per-game category average dict. Prefers a period whose key
    contains 'avg'; falls back to totals divided by games played if
    only totals are available. Returns {} if nothing usable is found —
    run with --debug-player to see the raw shape for your league."""
    stats = getattr(player, "stats", None) or {}
    best = {}
    best_is_avg = False

    for period_key, period_stats in stats.items():
        if not isinstance(period_stats, dict):
            continue
        inner = period_stats.get("avg") if isinstance(period_stats.get("avg"), dict) else period_stats

        found = {}
        for cat, variants in STAT_NAME_VARIANTS.items():
            for v in variants:
                if v in inner:
                    found[cat] = inner[v]
                    break

        if len(found) < 5:
            continue

        is_avg_period = "avg" in str(period_key).lower() or "avg" in period_stats
        if is_avg_period and not best_is_avg:
            best, best_is_avg = found, True
        elif len(found) > len(best) and not best_is_avg:
            best = found

    return {k: float(v) for k, v in best.items()}


def compute_player_values(league, year, debug_player=None):
    """Builds {player_name: z_score_total} across every rostered
    player in the league. Sums z-scores across the 9 categories
    (turnovers inverted) — same approach as Basketball Monster /
    ESPN's Player Rater."""
    player_pool = []
    for team in league.teams:
        for player in getattr(team, "roster", []):
            if debug_player and player.name.lower() == debug_player.lower():
                print(f"--- raw stats for {player.name} ---", file=sys.stderr)
                print(json.dumps(getattr(player, "stats", {}), indent=2, default=str), file=sys.stderr)
            averages = extract_player_averages(player, year)
            if averages:
                player_pool.append((player.name, averages))

    if not player_pool:
        print("warning: no player averages could be extracted — playerValues will be empty. "
              "Try --debug-player \"Full Player Name\" to inspect the raw stats shape.", file=sys.stderr)
        return {}

    # league mean/stdev per category across all rostered players
    cat_values = defaultdict(list)
    for _, averages in player_pool:
        for cat, val in averages.items():
            cat_values[cat].append(val)

    cat_mean = {c: statistics.mean(v) for c, v in cat_values.items() if len(v) > 1}
    cat_std = {c: (statistics.stdev(v) or 1) for c, v in cat_values.items() if len(v) > 1}

    player_values = {}
    for name, averages in player_pool:
        z_total = 0.0
        for cat, val in averages.items():
            if cat not in cat_mean or cat_std[cat] == 0:
                continue
            z = (val - cat_mean[cat]) / cat_std[cat]
            if cat in LOWER_BETTER:
                z = -z
            z_total += z
        player_values[name] = round(z_total, 2)

    return player_values


# ---------- Category MVP + waiver value (reuse the same z-scores) ----------

def build_team_players(league, player_values):
    """Attaches each team's top players (by z-score) with a rough
    per-category contribution share, for the Category MVP tab."""
    out = {}
    for team in league.teams:
        roster_entries = []
        for player in getattr(team, "roster", []):
            val = player_values.get(player.name)
            if val is None:
                continue
            roster_entries.append((player.name, val))
        roster_entries.sort(key=lambda x: x[1], reverse=True)
        # keep the top 3 as the "notable players" set the dashboard expects;
        # contribution is a normalized share of this player's z-score value
        # relative to the team's total — a simplification, not a precise
        # per-category attribution
        top = roster_entries[:3]
        total = sum(max(v, 0.01) for _, v in top) or 1
        players = []
        for name, val in top:
            share = max(val, 0.01) / total
            contribution = {cat: share * 0.35 for cat in CATEGORIES}  # simplified even spread
            players.append({"name": name, "contribution": contribution})
        out[team.team_id] = players
    return out


def build_transactions(league, player_values, limit=10):
    """Recent waiver adds, valued using the player's current z-score.
    This is a proxy for "value added since acquired" — a fully accurate
    version would need to track that player's stats only from their
    acquisition date forward, which needs per-game logs rather than
    season averages. Treat this as "how good is this pickup right now",
    not a precise post-acquisition production number."""
    transactions = []
    try:
        for act in league.recent_activity(size=25):
            for action in act.actions:
                team, move_type, player, _ = action
                if move_type == "FA ADDED":
                    transactions.append({
                        "player": str(player),
                        "team": team.team_name,
                        "valueSince": player_values.get(str(player), 0),
                    })
    except Exception as e:
        print(f"warning: couldn't read recent activity: {e}", file=sys.stderr)

    transactions.sort(key=lambda t: t["valueSince"], reverse=True)
    return transactions[:limit]


# ---------- Main payload ----------

def build_payload(league, year, debug_player=None):
    teams = []
    teams_by_id = {}
    for t in league.teams:
        team_entry = {
            "id": t.team_id,
            "name": t.team_name,
            "manager": (t.owners[0].get("firstName", "Manager")
                        if getattr(t, "owners", None) else "Manager"),
            "wins": t.wins,
            "losses": t.losses,
        }
        teams.append(team_entry)
        teams_by_id[t.team_id] = team_entry

    current_week = league.currentMatchupPeriod
    schedule, weekly_by_team = build_weekly_data(league, teams_by_id, current_week)

    for team in teams:
        team["weekly"] = weekly_by_team.get(team["id"], [])

    player_values = compute_player_values(league, year, debug_player=debug_player)
    team_players = build_team_players(league, player_values)
    for team in teams:
        team["players"] = team_players.get(team["id"], [])

    transactions = build_transactions(league, player_values)

    return {
        "leagueName": league.settings.name,
        "week": len(schedule),
        "teams": teams,
        "schedule": schedule,
        "transactions": transactions,
        "trades": [],  # log trades manually in the dashboard — see note below
        "playerValues": player_values,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--league-id", type=int, required=True)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--swid", default=None)
    p.add_argument("--espn-s2", default=None)
    p.add_argument("--debug-player", default=None,
                    help="Print the raw stats object for this player (exact name) to stderr, "
                         "to help adapt extract_player_averages() to your league's data shape.")
    args = p.parse_args()

    league = League(
        league_id=args.league_id,
        year=args.year,
        swid=args.swid,
        espn_s2=args.espn_s2,
    )
    payload = build_payload(league, args.year, debug_player=args.debug_player)
    print(json.dumps(payload, indent=2))

    if not payload["playerValues"]:
        print("\nnote: playerValues came back empty — the dashboard's Trade tab and "
              "Category MVP tab will fall back to sample/manual data until this is fixed. "
              "Run again with --debug-player \"Some Player On Your League\" to investigate.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
