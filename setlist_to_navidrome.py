#!/usr/bin/env python3
"""
setlist_to_navidrome.py

Kräver:
  pip install requests

Miljövariabler / konfiguration (kan också fyllas i direkt):
  SETLISTFM_API_KEY   - ditt setlist.fm API-nyckel
  NAV_BASE_URL        - t.ex. http://navidrome.local:4533
  NAV_USERNAME        - Navidrome-användarnamn
  NAV_PASSWORD        - Navidrome-lösenord (plain, används för token-hash)
  CLIENT_ID           - en valfri client-sträng för Subsonic param 'c' (ex: "setlist-sync")

Anrop:
  python setlist_to_navidrome.py --setlist-id <setlistId> --playlist-name "Band - Tour YYYY"
  eller
  python setlist_to_navidrome.py --setlist-url "https://www.setlist.fm/setlist/artist/..." --playlist-name "..."
"""

import os
import sys
import argparse
import hashlib
import random
import string
import requests
from urllib.parse import urljoin

# --- Config / env ---
SETLISTFM_API_KEY = os.getenv("SETLISTFM_API_KEY")
NAV_BASE_URL = os.getenv("NAV_BASE_URL")  # ex: "http://navidrome.local:4533"
NAV_USERNAME = os.getenv("NAV_USERNAME")
NAV_PASSWORD = os.getenv("NAV_PASSWORD")
CLIENT_ID = os.getenv("CLIENT_ID", "setlist-sync")

# --- Utilities ---
def random_salt(n=8):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

def md5hex(s: str) -> str:
    return hashlib.md5(s.encode('utf-8')).hexdigest()

# --- setlist.fm ---
def fetch_setlist_by_id(setlist_id: str):
    if not SETLISTFM_API_KEY:
        raise RuntimeError("SETLISTFM_API_KEY saknas i miljön.")
    url = f"https://api.setlist.fm/1.0/setlist/{setlist_id}"
    headers = {"x-api-key": SETLISTFM_API_KEY, "Accept": "application/json"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def parse_songs_from_setlist(json_obj):
    songs = []
    # setlist.fm strukturerar sets -> song[]
    for s in json_obj.get("sets", {}).get("set", []) or []:
        for song in s.get("song", []) or []:
            # song may be {"name": "..."} or {"name": "...", "cover": {...}}
            title = song.get("name")
            if title:
                songs.append(title)
    return songs

# --- Navidrome / Subsonic helpers ---
def subsonic_params(username, password):
    s = random_salt(8)
    t = md5hex(password + s)
    return {"u": username, "t": t, "s": s, "v": "1.16.1", "c": CLIENT_ID}

def subsonic_call(base_url, endpoint, params=None):
    if params is None:
        params = {}
    url = urljoin(base_url, f"/rest/{endpoint}.view")
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r

def search_song(base_url, username, password, artist, title, max_results=5):
    # använder search3 med artist+title (finns i Subsonic API)
    params = subsonic_params(username, password)
    # search3 supports artist and title query parts (use search3 with combined query)
    q = f"{artist} {title}" if artist else title
    params.update({"query": q, "songCount": str(max_results), "artistCount": "0", "albumCount": "0"})
    r = subsonic_call(base_url, "search3", params=params)
    # accept JSON? API returns XML by default; request json by adding f=json param 'f=json'
    # Some servers support f=json; add it to params if needed
    return r.text  # we will parse XML minimally below

def extract_song_ids_from_search_xml(xml_text, title):
    # Very lightweight parsing to extract <id> attributes for <song id="..."> entries
    # Avoid full XML dependency to keep script simple; for production use use xml.etree.ElementTree
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    ns = {'s': 'http://subsonic.org/restapi'}
    ids = []
    # try both namespaced and non-namespaced
    for song in root.findall('.//{http://subsonic.org/restapi}song') + root.findall('.//song'):
        sid = song.get('id')
        sname = song.get('title') or song.get('name')
        if sid:
            ids.append((sid, sname))
    return ids

def create_playlist(base_url, username, password, playlist_name, song_ids):
    params = subsonic_params(username, password)
    params["name"] = playlist_name
    # add songId parameters (one per song), order preserved
    for i, sid in enumerate(song_ids):
        params[f"songId[{i}]"] = sid
    r = requests.post(urljoin(base_url, "/rest/createPlaylist.view"), params=params, timeout=30)
    r.raise_for_status()
    return r

# --- Main flow ---
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setlist-id", help="setlist.fm setlistId (ex: 7bd6f1ac)")
    parser.add_argument("--setlist-url", help="full setlist.fm URL")
    parser.add_argument("--playlist-name", required=True, help="namn på spellistan som skapas i Navidrome")
    parser.add_argument("--artist", help="artistname (för att förbättra sökning om behövs)")
    parser.add_argument("--max-songs", type=int, default=200, help="max antal låtar i spellistan")
    args = parser.parse_args()

    if not NAV_BASE_URL or not NAV_USERNAME or not NAV_PASSWORD:
        print("NAV_BASE_URL, NAV_USERNAME och NAV_PASSWORD måste vara satta som miljövariabler.", file=sys.stderr)
        sys.exit(2)
    if not SETLISTFM_API_KEY:
        print("SETLISTFM_API_KEY måste vara satt som miljövariabel.", file=sys.stderr)
        sys.exit(2)

    setlist_id = args.setlist_id
    if not setlist_id and args.setlist_url:
        # extrahera sista path-delen som setlistId (ofta /setlist/<artist>/<id>)
        setlist_id = args.setlist_url.rstrip("/").split("/")[-1]

    if not setlist_id:
        print("Ingen setlist-id angiven.", file=sys.stderr)
        sys.exit(2)

    print(f"Hämtar setlist {setlist_id} från setlist.fm...")
    js = fetch_setlist_by_id(setlist_id)
    songs = parse_songs_from_setlist(js)
    if not songs:
        print("Ingen låt hittades i setlisten.", file=sys.stderr)
        sys.exit(1)

    # begränsa
    songs = songs[: args.max_songs]
    print(f"Hittade {len(songs)} låtar i setlisten. Försöker matcha mot Navidrome...")

    matched_ids = []
    for title in songs:
        # Sök efter låten i Navidrome via search3
        try:
            xml = search_song(NAV_BASE_URL, NAV_USERNAME, NAV_PASSWORD, args.artist or js.get("artist", {}).get("name"), title)
            ids = extract_song_ids_from_search_xml(xml, title)
            if ids:
                # välj första match som standard
                chosen_id = ids[0][0]
                matched_ids.append(chosen_id)
                print(f"Matched: {title} -> {ids[0][1]} (id {chosen_id})")
            else:
                print(f"INGEN match för: {title}")
        except Exception as e:
            print(f"Fel vid sökning för '{title}': {e}")

    if not matched_ids:
        print("Inga låtar matchades; avbryter skapandet av spellista.", file=sys.stderr)
        sys.exit(1)

    print(f"Skapar spellista '{args.playlist_name}' med {len(matched_ids)} spår...")
    r = create_playlist(NAV_BASE_URL, NAV_USERNAME, NAV_PASSWORD, args.playlist_name, matched_ids)
    print("Spellista skapad. Server response:")
    print(r.text)

if __name__ == "__main__":
    main()

