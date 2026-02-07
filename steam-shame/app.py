"""
Steam Shame - A web app that calculates your Steam library shame score.
"""

from flask import Flask, redirect, request, session, url_for, render_template, jsonify
import requests
import os
import re
from urllib.parse import urlencode

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this")

# Get these from environment variables
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")


# ============== Steam API Functions ==============

def get_owned_games(steam_id: str) -> dict:
    """Fetch all owned games with playtime info."""
    url = "http://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": STEAM_API_KEY,
        "steamid": steam_id,
        "include_appinfo": True,
        "include_played_free_games": True,
        "format": "json"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def get_player_summary(steam_id: str) -> dict:
    """Get player profile info."""
    url = "http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
    params = {
        "key": STEAM_API_KEY,
        "steamids": steam_id,
        "format": "json"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def resolve_vanity_url(vanity_name: str) -> str:
    """Convert a vanity URL name to Steam ID."""
    url = "http://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
    params = {
        "key": STEAM_API_KEY,
        "vanityurl": vanity_name,
        "format": "json"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    if data.get("response", {}).get("success") == 1:
        return data["response"]["steamid"]
    return None


def get_friends_list(steam_id: str) -> list:
    """Get a user's friends list."""
    url = "http://api.steampowered.com/ISteamUser/GetFriendList/v1/"
    params = {
        "key": STEAM_API_KEY,
        "steamid": steam_id,
        "relationship": "friend",
        "format": "json"
    }
    response = requests.get(url, params=params)
    if response.status_code == 401:
        return []  # Friends list is private
    response.raise_for_status()
    data = response.json()
    return data.get("friendslist", {}).get("friends", [])


# ============== Analysis Functions ==============

def format_playtime(minutes: int) -> str:
    """Convert minutes to readable format."""
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h"
    days = hours / 24
    return f"{days:.1f} days"


def analyze_library(games: list) -> dict:
    """Crunch the numbers on your shame."""
    
    if not games:
        return None
    
    total_games = len(games)
    
    # Categorize games by playtime
    never_played = [g for g in games if g.get("playtime_forever", 0) == 0]
    under_hour = [g for g in games if 0 < g.get("playtime_forever", 0) < 60]
    under_two_hours = [g for g in games if 60 <= g.get("playtime_forever", 0) < 120]
    actually_played = [g for g in games if g.get("playtime_forever", 0) >= 120]
    
    # Total playtime
    total_minutes = sum(g.get("playtime_forever", 0) for g in games)
    total_hours = total_minutes / 60
    
    # Top played games
    sorted_by_playtime = sorted(games, key=lambda x: x.get("playtime_forever", 0), reverse=True)
    top_10 = sorted_by_playtime[:10]
    
    # Games you "tried" but gave up on (10-60 min)
    gave_up = sorted(
        [g for g in games if 10 <= g.get("playtime_forever", 0) < 60],
        key=lambda x: x.get("playtime_forever", 0),
        reverse=True
    )[:10]
    
    # Random sample of never played
    import random
    never_played_sample = random.sample(never_played, min(10, len(never_played))) if never_played else []
    
    # Calculate concentration
    top_5_minutes = sum(g.get("playtime_forever", 0) for g in sorted_by_playtime[:5])
    concentration = (top_5_minutes / total_minutes * 100) if total_minutes > 0 else 0
    
    # Shame score
    shame_score = (len(never_played) / total_games * 100) if total_games > 0 else 0
    
    # Verdict
    if shame_score > 70:
        verdict = "You have a problem. Stop buying games."
    elif shame_score > 50:
        verdict = "Steam sales have claimed another victim."
    elif shame_score > 30:
        verdict = "Not bad, but you know you'll never play those."
    else:
        verdict = "Impressive restraint. Or new account."
    
    return {
        "total_games": total_games,
        "never_played_count": len(never_played),
        "never_played_sample": [{"name": g.get("name", "Unknown"), "appid": g.get("appid")} for g in never_played_sample],
        "under_hour_count": len(under_hour),
        "under_two_hours_count": len(under_two_hours),
        "actually_played_count": len(actually_played),
        "gave_up": [{"name": g.get("name", "Unknown"), "playtime": g.get("playtime_forever", 0), "appid": g.get("appid")} for g in gave_up],
        "total_minutes": total_minutes,
        "total_hours": round(total_hours, 1),
        "top_10": [{"name": g.get("name", "Unknown"), "playtime": g.get("playtime_forever", 0), "playtime_formatted": format_playtime(g.get("playtime_forever", 0)), "appid": g.get("appid")} for g in top_10],
        "concentration": round(concentration, 1),
        "shame_score": round(shame_score, 1),
        "verdict": verdict,
    }


# ============== Routes ==============

@app.route("/")
def index():
    """Home page with lookup form."""
    return render_template("index.html")


@app.route("/lookup", methods=["POST"])
def lookup():
    """Look up a Steam profile by URL or ID."""
    steam_input = request.form.get("steam_input", "").strip()
    
    if not steam_input:
        return redirect(url_for("index"))
    
    steam_id = None
    
    # Check if it's already a Steam ID (17 digit number)
    if re.match(r"^\d{17}$", steam_input):
        steam_id = steam_input
    
    # Check if it's a full Steam URL
    elif "steamcommunity.com" in steam_input:
        # Extract ID or vanity name from URL
        match = re.search(r"steamcommunity\.com/(?:profiles|id)/([^/\?]+)", steam_input)
        if match:
            id_or_vanity = match.group(1)
            if re.match(r"^\d{17}$", id_or_vanity):
                steam_id = id_or_vanity
            else:
                steam_id = resolve_vanity_url(id_or_vanity)
    
    # Assume it's a vanity name
    else:
        steam_id = resolve_vanity_url(steam_input)
    
    if not steam_id:
        return render_template("index.html", error="Could not find that Steam profile. Try pasting your full Steam profile URL.")
    
    return redirect(url_for("results", steam_id=steam_id))


@app.route("/results/<steam_id>")
def results(steam_id: str):
    """Show shame results for a Steam ID."""
    
    try:
        # Get player info
        player_data = get_player_summary(steam_id)
        players = player_data.get("response", {}).get("players", [])
        
        if not players:
            return render_template("index.html", error="Could not find that Steam profile.")
        
        player = players[0]
        player_name = player.get("personaname", "Unknown")
        avatar_url = player.get("avatarfull", "")
        profile_url = player.get("profileurl", "")
        
        # Check if profile is public
        if player.get("communityvisibilitystate") != 3:
            return render_template("error.html", 
                error="This profile is private",
                message="Game details need to be public for Steam Shame to work. Update your privacy settings in Steam and try again."
            )
        
        # Get games
        games_data = get_owned_games(steam_id)
        games = games_data.get("response", {}).get("games", [])
        
        if not games:
            return render_template("error.html",
                error="No games found",
                message="Either this account has no games, or game details are set to private. Check your Steam privacy settings."
            )
        
        # Analyze
        stats = analyze_library(games)
        
        return render_template("results.html",
            player_name=player_name,
            avatar_url=avatar_url,
            profile_url=profile_url,
            steam_id=steam_id,
            stats=stats
        )
        
    except requests.exceptions.HTTPError as e:
        return render_template("error.html", error="Steam API Error", message=str(e))
    except Exception as e:
        return render_template("error.html", error="Something went wrong", message=str(e))


@app.route("/api/stats/<steam_id>")
def api_stats(steam_id: str):
    """JSON API for stats (for potential future use)."""
    try:
        player_data = get_player_summary(steam_id)
        players = player_data.get("response", {}).get("players", [])
        
        if not players:
            return jsonify({"error": "Profile not found"}), 404
        
        player = players[0]
        
        if player.get("communityvisibilitystate") != 3:
            return jsonify({"error": "Profile is private"}), 403
        
        games_data = get_owned_games(steam_id)
        games = games_data.get("response", {}).get("games", [])
        
        if not games:
            return jsonify({"error": "No games found"}), 404
        
        stats = analyze_library(games)
        stats["player_name"] = player.get("personaname")
        stats["avatar_url"] = player.get("avatarfull")
        
        return jsonify(stats)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/friends/<steam_id>")
def api_friends(steam_id: str):
    """JSON API for friends leaderboard - called async from frontend."""
    try:
        # Get the user's stats first
        player_data = get_player_summary(steam_id)
        players = player_data.get("response", {}).get("players", [])
        
        if not players or players[0].get("communityvisibilitystate") != 3:
            return jsonify({"error": "Profile not accessible"}), 403
        
        user_games_data = get_owned_games(steam_id)
        user_games = user_games_data.get("response", {}).get("games", [])
        
        if not user_games:
            return jsonify({"error": "No games"}), 404
        
        user_stats = analyze_library(user_games)
        
        # Get friends
        friends = get_friends_list(steam_id)
        
        if not friends:
            return jsonify({"leaderboard": [], "user_rank": None, "error": "No friends found or friends list is private"})
        
        # Limit to 50 friends
        friend_ids = [f["steamid"] for f in friends[:50]]
        all_ids = [steam_id] + friend_ids
        
        # Get all player summaries
        all_players = []
        for i in range(0, len(all_ids), 100):
            batch = all_ids[i:i+100]
            batch_data = get_player_summary(",".join(batch))
            all_players.extend(batch_data.get("response", {}).get("players", []))
        
        # Build leaderboard
        leaderboard = []
        
        for p in all_players:
            pid = p.get("steamid")
            pname = p.get("personaname", "Unknown")
            pavatar = p.get("avatar", "")
            
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
                    "name": pname,
                    "avatar": pavatar,
                    "shame_score": p_stats["shame_score"],
                    "total_games": p_stats["total_games"],
                    "never_played": p_stats["never_played_count"],
                    "total_hours": p_stats["total_hours"],
                    "is_user": pid == steam_id
                })
            except:
                continue
        
        # Sort by shame score
        leaderboard.sort(key=lambda x: x["shame_score"], reverse=True)
        
        # Add rankings
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
    """Show shame leaderboard for user and their friends."""
    
    try:
        # Get the main player's info
        player_data = get_player_summary(steam_id)
        players = player_data.get("response", {}).get("players", [])
        
        if not players:
            return render_template("error.html", error="Profile not found", message="Could not find that Steam profile.")
        
        player = players[0]
        player_name = player.get("personaname", "Unknown")
        avatar_url = player.get("avatarfull", "")
        
        if player.get("communityvisibilitystate") != 3:
            return render_template("error.html", 
                error="Profile is private",
                message="Your profile needs to be public to compare with friends."
            )
        
        # Get friends list
        friends = get_friends_list(steam_id)
        
        if not friends:
            return render_template("error.html",
                error="No friends found",
                message="Either your friends list is private, or you have no friends on Steam. 😢"
            )
        
        # Collect all steam IDs (user + friends)
        all_ids = [steam_id] + [f["steamid"] for f in friends]
        
        # Get all player summaries in batches of 100
        all_players = []
        for i in range(0, len(all_ids), 100):
            batch = all_ids[i:i+100]
            batch_data = get_player_summary(",".join(batch))
            all_players.extend(batch_data.get("response", {}).get("players", []))
        
        # Build leaderboard
        leaderboard = []
        
        for p in all_players:
            pid = p.get("steamid")
            pname = p.get("personaname", "Unknown")
            pavatar = p.get("avatar", "")
            
            # Skip private profiles
            if p.get("communityvisibilitystate") != 3:
                continue
            
            # Get games
            try:
                games_data = get_owned_games(pid)
                games = games_data.get("response", {}).get("games", [])
                
                if not games:
                    continue
                
                stats = analyze_library(games)
                
                leaderboard.append({
                    "steam_id": pid,
                    "name": pname,
                    "avatar": pavatar,
                    "shame_score": stats["shame_score"],
                    "total_games": stats["total_games"],
                    "never_played": stats["never_played_count"],
                    "total_hours": stats["total_hours"],
                    "is_user": pid == steam_id
                })
            except:
                continue  # Skip friends we can't fetch
        
        # Sort by shame score (highest = most shameful)
        leaderboard.sort(key=lambda x: x["shame_score"], reverse=True)
        
        # Add rankings
        for i, entry in enumerate(leaderboard):
            entry["rank"] = i + 1
        
        # Find user's rank
        user_rank = next((e["rank"] for e in leaderboard if e["is_user"]), None)
        
        return render_template("friends.html",
            player_name=player_name,
            avatar_url=avatar_url,
            steam_id=steam_id,
            leaderboard=leaderboard,
            user_rank=user_rank,
            total_friends=len(leaderboard) - 1
        )
        
    except Exception as e:
        return render_template("error.html", error="Something went wrong", message=str(e))


if __name__ == "__main__":
    if not STEAM_API_KEY:
        print("⚠️  Warning: STEAM_API_KEY not set!")
        print("   Set it with: export STEAM_API_KEY=your_key_here")
    app.run(debug=True, port=5000)
