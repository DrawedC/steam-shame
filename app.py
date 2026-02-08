"""
Steam Shame - A web app that calculates your Steam library shame score.
"""

from flask import Flask, redirect, request, session, url_for, render_template, jsonify
import requests
import os
import re
import random
import time
import math
import threading
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this")

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")

# Simple in-memory cache for store API data
_store_cache = {}
_store_cache_lock = threading.Lock()
STORE_CACHE_TTL = 3600


# ============== Steam API Functions ==============

def get_owned_games(steam_id: str) -> dict:
    url = "http://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": STEAM_API_KEY, "steamid": steam_id,
        "include_appinfo": True, "include_played_free_games": True, "format": "json"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def get_player_summary(steam_id: str) -> dict:
    url = "http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
    params = {"key": STEAM_API_KEY, "steamids": steam_id, "format": "json"}
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def resolve_vanity_url(vanity_name: str) -> str:
    url = "http://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
    params = {"key": STEAM_API_KEY, "vanityurl": vanity_name, "format": "json"}
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    if data.get("response", {}).get("success") == 1:
        return data["response"]["steamid"]
    return None


def get_friends_list(steam_id: str) -> list:
    url = "http://api.steampowered.com/ISteamUser/GetFriendList/v1/"
    params = {"key": STEAM_API_KEY, "steamid": steam_id, "relationship": "friend", "format": "json"}
    response = requests.get(url, params=params)
    if response.status_code == 401:
        return []
    response.raise_for_status()
    data = response.json()
    return data.get("friendslist", {}).get("friends", [])


def get_app_details(appid: int) -> dict:
    now = time.time()
    with _store_cache_lock:
        cached = _store_cache.get(appid)
        if cached and (now - cached["ts"]) < STORE_CACHE_TTL:
            return cached["data"]
    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            app_data = data.get(str(appid), {})
            if app_data.get("success"):
                result = app_data.get("data", {})
                with _store_cache_lock:
                    _store_cache[appid] = {"data": result, "ts": now}
                return result
    except Exception:
        pass
    return None


def get_app_details_batch(appids: list, max_workers: int = 5, delay: float = 0.3) -> dict:
    results = {}
    def fetch_one(appid):
        time.sleep(random.uniform(0.1, delay))
        return appid, get_app_details(appid)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, aid): aid for aid in appids}
        for future in as_completed(futures):
            try:
                appid, data = future.result()
                if data:
                    results[appid] = data
            except Exception:
                continue
    return results


def extract_usd_price(details: dict) -> float:
    """Extract price in USD dollars. Returns None if unavailable."""
    price_data = details.get("price_overview")
    if not price_data:
        return None
    currency = price_data.get("currency", "")
    if currency and currency != "USD":
        return None
    price_cents = price_data.get("final", price_data.get("initial", 0))
    price_dollars = price_cents / 100
    if price_dollars > 80:
        return None
    return price_dollars


# ============== Analysis Functions ==============

def format_playtime(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h"
    days = hours / 24
    return f"{days:.1f} days"


def calculate_shame_score(never_played_count: int, total_games: int) -> float:
    """Shame score that weighs both percentage AND absolute count.
    
    Formula: base_pct * volume_multiplier
    - base_pct: percentage of games never played (0-100)
    - volume_multiplier: scales up with library size using log curve
      - 10 games: 0.5x (lenient)
      - 50 games: 0.75x 
      - 100 games: 0.85x
      - 200 games: 0.92x
      - 500+ games: ~1.0x (full weight)
    
    So 1/2 unplayed = ~25 shame, but 490/1000 unplayed = ~49 shame.
    """
    if total_games == 0:
        return 0.0
    
    base_pct = (never_played_count / total_games) * 100
    
    # Log curve that approaches 1.0 as library grows
    # log2(500) ≈ 9, so we normalize to that
    volume_multiplier = min(1.0, math.log2(max(total_games, 2)) / math.log2(500))
    
    # Floor at 0.4 so tiny libraries still get some score
    volume_multiplier = max(0.4, volume_multiplier)
    
    return round(base_pct * volume_multiplier, 1)


def analyze_library(games: list) -> dict:
    """Crunch the numbers on your shame."""
    if not games:
        return None

    thirty_days_ago = time.time() - (30 * 24 * 60 * 60)

    def is_recent(game):
        last_played = game.get("rtime_last_played", 0)
        if last_played and last_played > thirty_days_ago:
            return True
        if game.get("playtime_2weeks", 0) > 0:
            return True
        return False

    total_games = len(games)

    # Categorize
    never_played = [g for g in games if g.get("playtime_forever", 0) == 0]
    barely_played = [g for g in games if 5 < g.get("playtime_forever", 0) < 60 and not is_recent(g)]
    played = [g for g in games if g.get("playtime_forever", 0) >= 60]

    # For the shame pile: never played excluding recent
    never_played_shameful = [g for g in never_played if not is_recent(g)]
    never_played_sample = random.sample(never_played_shameful, min(10, len(never_played_shameful))) if never_played_shameful else []
    barely_played_sample = sorted(barely_played, key=lambda x: x.get("playtime_forever", 0))[:10]

    # Shame score using new formula
    shame_score = calculate_shame_score(len(never_played), total_games)

    # Verdict
    if shame_score > 60:
        verdict = "You have a problem. Stop buying games."
    elif shame_score > 40:
        verdict = "Steam sales have claimed another victim."
    elif shame_score > 25:
        verdict = "Not bad, but you know you'll never play those."
    else:
        verdict = "Impressive restraint. Or new account."

    return {
        "total_games": total_games,
        "never_played_count": len(never_played),
        "never_played_shameful_count": len(never_played_shameful),
        "never_played_sample": [{"name": g.get("name", "Unknown"), "appid": g.get("appid")} for g in never_played_sample],
        "barely_played_count": len(barely_played),
        "barely_played_sample": [{"name": g.get("name", "Unknown"), "playtime": g.get("playtime_forever", 0), "appid": g.get("appid")} for g in barely_played_sample],
        "played_count": len(played),
        "shame_score": shame_score,
        "verdict": verdict,
    }


# ============== Genre Analysis ==============

GENRE_CATEGORIES = {
    "fps_shooter": {"names": ["FPS", "Shooter", "First-Person Shooter", "Third-Person Shooter"], "label": "Shooter", "emoji": "🔫"},
    "rpg": {"names": ["RPG", "JRPG", "Action RPG", "Turn-Based RPG", "CRPG", "Role-Playing"], "label": "RPG", "emoji": "⚔️"},
    "strategy": {"names": ["Strategy", "Real-Time Strategy", "Turn-Based Strategy", "Tower Defense", "RTS", "4X", "Grand Strategy"], "label": "Strategy", "emoji": "🧠"},
    "survival": {"names": ["Survival", "Survival Horror", "Crafting", "Base Building", "Open World Survival Craft"], "label": "Survival", "emoji": "🏕️"},
    "simulation": {"names": ["Simulation", "Life Sim", "Farming Sim", "Management", "City Builder", "Building"], "label": "Simulation", "emoji": "🏗️"},
    "action": {"names": ["Action", "Hack and Slash", "Beat 'em up", "Action-Adventure"], "label": "Action", "emoji": "💥"},
    "puzzle": {"names": ["Puzzle", "Logic", "Hidden Object"], "label": "Puzzle", "emoji": "🧩"},
    "platformer": {"names": ["Platformer", "2D Platformer", "3D Platformer", "Precision Platformer"], "label": "Platformer", "emoji": "🍄"},
    "horror": {"names": ["Horror", "Psychological Horror", "Survival Horror"], "label": "Horror", "emoji": "👻"},
    "racing": {"names": ["Racing", "Driving", "Automobile Sim"], "label": "Racing", "emoji": "🏎️"},
    "sports": {"names": ["Sports", "Football", "Basketball", "Baseball", "Soccer", "Golf"], "label": "Sports", "emoji": "⚽"},
    "sandbox": {"names": ["Sandbox", "Open World", "Exploration"], "label": "Open World", "emoji": "🌍"},
    "roguelike": {"names": ["Roguelike", "Roguelite", "Roguevania", "Procedural Generation"], "label": "Roguelike", "emoji": "💀"},
    "multiplayer": {"names": ["Massively Multiplayer", "MMO", "MMORPG", "Co-op", "Multiplayer"], "label": "Multiplayer", "emoji": "👥"},
    "casual": {"names": ["Casual", "Clicker", "Idle", "Card Game", "Board Game"], "label": "Casual", "emoji": "🎲"},
    "visual_novel": {"names": ["Visual Novel", "Dating Sim", "Choose Your Own Adventure", "Interactive Fiction"], "label": "Visual Novel", "emoji": "📖"},
    "fighting": {"names": ["Fighting", "Martial Arts"], "label": "Fighting", "emoji": "🥊"},
}


def classify_game_genres(store_data: dict) -> list:
    categories = set()
    genres = [g.get("description", "") for g in store_data.get("genres", [])]
    tags = [c.get("description", "") for c in store_data.get("categories", [])]
    all_labels = genres + tags
    for cat_key, cat_info in GENRE_CATEGORIES.items():
        for name in cat_info["names"]:
            if name.lower() in [l.lower() for l in all_labels]:
                categories.add(cat_key)
                break
    return list(categories)


def detect_badges(stats: dict, store_details: dict, games: list) -> list:
    badges = []

    if stats["total_games"] > 200 and stats["shame_score"] > 40:
        badges.append({"name": "Humble Bundle Victim", "emoji": "📦",
            "description": "200+ games, most untouched. Those bundles got you good."})

    early_access_count = sum(1 for d in store_details.values()
        if "early access" in [g.get("description", "").lower() for g in d.get("genres", [])])
    if early_access_count >= 5:
        badges.append({"name": "Early Access Addict", "emoji": "🚧",
            "description": f"{early_access_count} Early Access games. You love paying to beta test."})

    if stats.get("played_count", 0) > 0:
        total_minutes = sum(g.get("playtime_forever", 0) for g in games)
        top_game = max(games, key=lambda g: g.get("playtime_forever", 0))
        if total_minutes > 0:
            top_pct = (top_game.get("playtime_forever", 0) / total_minutes) * 100
            if top_pct > 50:
                badges.append({"name": "One-Trick Pony", "emoji": "🐴",
                    "description": f"{top_pct:.0f}% of your time in {top_game.get('name', 'one game')}."})

    if stats["total_games"] >= 500:
        badges.append({"name": "Game Collector", "emoji": "🏛️",
            "description": f"{stats['total_games']} games. You don't play games, you collect them."})

    quick_abandon = len([g for g in games if 0 < g.get("playtime_forever", 0) < 30])
    if quick_abandon >= 20:
        badges.append({"name": "Speedrun Abandoner", "emoji": "⏱️",
            "description": f"Opened {quick_abandon} games for under 30 minutes."})

    if stats["total_games"] < 50 and stats["shame_score"] < 20:
        badges.append({"name": "Disciplined Buyer", "emoji": "🎯",
            "description": "Small library, actually played. Impressive self-control."})

    f2p_count = sum(1 for d in store_details.values() if d.get("is_free", False))
    if f2p_count >= 10:
        badges.append({"name": "F2P Warrior", "emoji": "🆓",
            "description": f"{f2p_count} free-to-play games. At least those didn't cost anything."})

    return badges[:6]


# ============== Routes ==============

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/lookup", methods=["POST"])
def lookup():
    steam_input = request.form.get("steam_input", "").strip()
    if not steam_input:
        return redirect(url_for("index"))

    steam_id = None
    if re.match(r"^\d{17}$", steam_input):
        steam_id = steam_input
    elif "steamcommunity.com" in steam_input:
        match = re.search(r"steamcommunity\.com/(?:profiles|id)/([^/\?]+)", steam_input)
        if match:
            id_or_vanity = match.group(1)
            if re.match(r"^\d{17}$", id_or_vanity):
                steam_id = id_or_vanity
            else:
                steam_id = resolve_vanity_url(id_or_vanity)
    else:
        steam_id = resolve_vanity_url(steam_input)

    if not steam_id:
        return render_template("index.html", error="Could not find that Steam profile. Try pasting your full Steam profile URL.")
    return redirect(url_for("results", steam_id=steam_id))


@app.route("/results/<steam_id>")
def results(steam_id: str):
    try:
        player_data = get_player_summary(steam_id)
        players = player_data.get("response", {}).get("players", [])
        if not players:
            return render_template("index.html", error="Could not find that Steam profile.")

        player = players[0]
        player_name = player.get("personaname", "Unknown")
        avatar_url = player.get("avatarfull", "")
        profile_url = player.get("profileurl", "")

        if player.get("communityvisibilitystate") != 3:
            return render_template("error.html",
                error="This profile is private",
                message="Game details need to be public for Steam Shame to work.")

        games_data = get_owned_games(steam_id)
        games = games_data.get("response", {}).get("games", [])
        if not games:
            return render_template("error.html",
                error="No games found",
                message="Either this account has no games, or game details are set to private.")

        stats = analyze_library(games)

        return render_template("results.html",
            player_name=player_name, avatar_url=avatar_url,
            profile_url=profile_url, steam_id=steam_id, stats=stats)

    except requests.exceptions.HTTPError as e:
        return render_template("error.html", error="Steam API Error", message=str(e))
    except Exception as e:
        return render_template("error.html", error="Something went wrong", message=str(e))


# ============== Async API Endpoints ==============

@app.route("/api/value/<steam_id>")
def api_value(steam_id: str):
    """Async: library value + unplayed value. Samples by default."""
    full_scan = request.args.get("full", "0") == "1"

    try:
        games_data = get_owned_games(steam_id)
        games = games_data.get("response", {}).get("games", [])
        if not games:
            return jsonify({"error": "No games found"}), 404

        played_games = [g for g in games if g.get("playtime_forever", 0) > 0]
        unplayed_games = [g for g in games if g.get("playtime_forever", 0) == 0]

        if full_scan:
            sample_played = played_games
            sample_unplayed = unplayed_games
        else:
            sample_played = random.sample(played_games, min(40, len(played_games))) if played_games else []
            sample_unplayed = random.sample(unplayed_games, min(40, len(unplayed_games))) if unplayed_games else []

        all_sample = sample_played + sample_unplayed
        appids = [g["appid"] for g in all_sample]
        store_data = get_app_details_batch(appids, max_workers=5, delay=0.35)

        # Played value
        played_prices = []
        for g in sample_played:
            details = store_data.get(g["appid"])
            if not details:
                continue
            price = extract_usd_price(details)
            if price is not None and price > 0:
                played_prices.append(price)

        # Unplayed value
        unplayed_prices = []
        for g in sample_unplayed:
            details = store_data.get(g["appid"])
            if not details:
                continue
            price = extract_usd_price(details)
            if price is not None and price > 0:
                unplayed_prices.append(price)

        # Extrapolate if sampling
        if full_scan:
            total_played_value = sum(played_prices)
            total_unplayed_value = sum(unplayed_prices)
            is_estimate = False
            played_sampled = len(played_prices)
            unplayed_sampled = len(unplayed_prices)
        else:
            avg_played = (sum(played_prices) / len(played_prices)) if played_prices else 0
            avg_unplayed = (sum(unplayed_prices) / len(unplayed_prices)) if unplayed_prices else 0
            total_played_value = avg_played * len(played_games)
            total_unplayed_value = avg_unplayed * len(unplayed_games)
            is_estimate = True
            played_sampled = len(played_prices)
            unplayed_sampled = len(unplayed_prices)

        total_library_value = total_played_value + total_unplayed_value

        return jsonify({
            "library_value": round(total_library_value),
            "played_value": round(total_played_value),
            "unplayed_value": round(total_unplayed_value),
            "played_count": len(played_games),
            "unplayed_count": len(unplayed_games),
            "played_sampled": played_sampled,
            "unplayed_sampled": unplayed_sampled,
            "is_estimate": is_estimate,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/personality/<steam_id>")
def api_personality(steam_id: str):
    """Async: gamer DNA — library makeup, played, unplayed genres + badges."""
    try:
        games_data = get_owned_games(steam_id)
        games = games_data.get("response", {}).get("games", [])
        if not games:
            return jsonify({"error": "No games found"}), 404

        thirty_days_ago = time.time() - (30 * 24 * 60 * 60)

        played_games = sorted(
            [g for g in games if g.get("playtime_forever", 0) >= 60],
            key=lambda x: x.get("playtime_forever", 0), reverse=True)
        unplayed_games = [g for g in games
            if g.get("playtime_forever", 0) < 5
            and not (g.get("rtime_last_played", 0) > thirty_days_ago)
            and not (g.get("playtime_2weeks", 0) > 0)]

        played_sample = played_games[:40]
        unplayed_sample = random.sample(unplayed_games, min(40, len(unplayed_games)))
        all_sample_games = played_sample + unplayed_sample

        # Also sample some from overall library for "total" makeup
        overall_sample = random.sample(games, min(60, len(games)))
        all_to_fetch = list(set(g["appid"] for g in all_sample_games + overall_sample))

        store_data = get_app_details_batch(all_to_fetch, max_workers=5, delay=0.35)

        # Count genres per bucket
        def count_genres(game_list):
            counts = {}
            game_names = {}
            for g in game_list:
                details = store_data.get(g["appid"])
                if not details:
                    continue
                cats = classify_game_genres(details)
                name = g.get("name", "Unknown")
                for c in cats:
                    counts[c] = counts.get(c, 0) + 1
                    game_names.setdefault(c, []).append(name)
            return counts, game_names

        overall_counts, overall_games = count_genres(overall_sample)
        played_counts, played_game_names = count_genres(played_sample)
        unplayed_counts, unplayed_game_names = count_genres(unplayed_sample)

        def build_breakdown(counts, game_names):
            total = sum(counts.values()) or 1
            items = sorted(counts.items(), key=lambda x: x[1], reverse=True)
            result = []
            for cat_key, count in items[:8]:
                cat_info = GENRE_CATEGORIES.get(cat_key, {})
                result.append({
                    "key": cat_key,
                    "label": cat_info.get("label", cat_key),
                    "emoji": cat_info.get("emoji", "🎮"),
                    "count": count,
                    "pct": round((count / total) * 100, 1),
                    "games": game_names.get(cat_key, [])
                })
            return result

        overall_breakdown = build_breakdown(overall_counts, overall_games)
        played_breakdown = build_breakdown(played_counts, played_game_names)
        unplayed_breakdown = build_breakdown(unplayed_counts, unplayed_game_names)

        # Determine majorities
        overall_majority = overall_breakdown[0] if overall_breakdown else None
        played_majority = played_breakdown[0] if played_breakdown else None
        unplayed_majority = unplayed_breakdown[0] if unplayed_breakdown else None

        # "You think you like" — only if unplayed majority differs from played majority
        show_unplayed_mismatch = False
        if played_majority and unplayed_majority:
            if played_majority["key"] != unplayed_majority["key"]:
                show_unplayed_mismatch = True

        # Badges
        stats = analyze_library(games)
        badges = detect_badges(stats, store_data, games)

        return jsonify({
            "overall": overall_breakdown,
            "played": played_breakdown,
            "unplayed": unplayed_breakdown,
            "overall_majority": overall_majority,
            "played_majority": played_majority,
            "unplayed_majority": unplayed_majority,
            "show_unplayed_mismatch": show_unplayed_mismatch,
            "badges": badges,
            "sample_size": len(store_data)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/friends/<steam_id>")
def api_friends(steam_id: str):
    try:
        player_data = get_player_summary(steam_id)
        players = player_data.get("response", {}).get("players", [])
        if not players or players[0].get("communityvisibilitystate") != 3:
            return jsonify({"error": "Profile not accessible"}), 403

        user_games_data = get_owned_games(steam_id)
        user_games = user_games_data.get("response", {}).get("games", [])
        if not user_games:
            return jsonify({"error": "No games"}), 404

        friends = get_friends_list(steam_id)
        if not friends:
            return jsonify({"leaderboard": [], "user_rank": None, "error": "No friends found or friends list is private"})

        friend_ids = [f["steamid"] for f in friends[:50]]
        all_ids = [steam_id] + friend_ids

        all_players = []
        for i in range(0, len(all_ids), 100):
            batch = all_ids[i:i+100]
            batch_data = get_player_summary(",".join(batch))
            all_players.extend(batch_data.get("response", {}).get("players", []))

        leaderboard = []
        for p in all_players:
            pid = p.get("steamid")
            if p.get("communityvisibilitystate") != 3:
                continue
            try:
                p_games_data = get_owned_games(pid)
                p_games = p_games_data.get("response", {}).get("games", [])
                if not p_games:
                    continue
                p_stats = analyze_library(p_games)
                if p_stats["shame_score"] == 100:
                    continue
                leaderboard.append({
                    "steam_id": pid,
                    "name": p.get("personaname", "Unknown"),
                    "avatar": p.get("avatar", ""),
                    "shame_score": p_stats["shame_score"],
                    "total_games": p_stats["total_games"],
                    "played_count": p_stats["played_count"],
                    "never_played": p_stats["never_played_count"],
                    "is_user": pid == steam_id
                })
            except:
                continue

        leaderboard.sort(key=lambda x: x["shame_score"], reverse=True)
        user_rank = None
        for i, entry in enumerate(leaderboard):
            entry["rank"] = i + 1
            if entry["is_user"]:
                user_rank = i + 1

        return jsonify({
            "leaderboard": leaderboard[:10],
            "total_friends": len(leaderboard) - 1,
            "user_rank": user_rank
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/friends/<steam_id>")
def friends_leaderboard(steam_id: str):
    try:
        player_data = get_player_summary(steam_id)
        players = player_data.get("response", {}).get("players", [])
        if not players:
            return render_template("error.html", error="Profile not found", message="Could not find that Steam profile.")

        player = players[0]
        player_name = player.get("personaname", "Unknown")
        avatar_url = player.get("avatarfull", "")

        if player.get("communityvisibilitystate") != 3:
            return render_template("error.html", error="Profile is private",
                message="Your profile needs to be public to compare with friends.")

        friends = get_friends_list(steam_id)
        if not friends:
            return render_template("error.html", error="No friends found",
                message="Either your friends list is private, or you have no friends on Steam. 😢")

        all_ids = [steam_id] + [f["steamid"] for f in friends]
        all_players = []
        for i in range(0, len(all_ids), 100):
            batch = all_ids[i:i+100]
            batch_data = get_player_summary(",".join(batch))
            all_players.extend(batch_data.get("response", {}).get("players", []))

        leaderboard = []
        for p in all_players:
            pid = p.get("steamid")
            if p.get("communityvisibilitystate") != 3:
                continue
            try:
                games_data = get_owned_games(pid)
                games_list = games_data.get("response", {}).get("games", [])
                if not games_list:
                    continue
                stats = analyze_library(games_list)
                leaderboard.append({
                    "steam_id": pid, "name": p.get("personaname", "Unknown"),
                    "avatar": p.get("avatar", ""),
                    "shame_score": stats["shame_score"],
                    "total_games": stats["total_games"],
                    "played_count": stats["played_count"],
                    "never_played": stats["never_played_count"],
                    "is_user": pid == steam_id
                })
            except:
                continue

        leaderboard.sort(key=lambda x: x["shame_score"], reverse=True)
        for i, entry in enumerate(leaderboard):
            entry["rank"] = i + 1
        user_rank = next((e["rank"] for e in leaderboard if e["is_user"]), None)

        return render_template("friends.html",
            player_name=player_name, avatar_url=avatar_url,
            steam_id=steam_id, leaderboard=leaderboard,
            user_rank=user_rank, total_friends=len(leaderboard) - 1)

    except Exception as e:
        return render_template("error.html", error="Something went wrong", message=str(e))


if __name__ == "__main__":
    if not STEAM_API_KEY:
        print("⚠️  Warning: STEAM_API_KEY not set!")
        print("   Set it with: export STEAM_API_KEY=your_key_here")
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"
    app.run(debug=debug, host="0.0.0.0", port=port)
