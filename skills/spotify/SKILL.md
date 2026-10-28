---
name: spotify
description: Spotify integration - auth, playlists, track metadata, song recommendations, and music discovery sources. Trigger words - spotify, playlist, track metadata, song recommendations, music discovery, find music, recommend music, similar tracks, dj recommendations, discover by sven.
---

# Spotify

Spotify integration for the dj-buyer pipeline and general music discovery/recommendation.

> ⚠️ **Check first:** `~/code/dj-buyer` (which holds the token DB and the client secret) is not present on every machine. If `ls ~/code/dj-buyer` fails, the Spotify API is unavailable here. Answer from web search or general knowledge, say that the Spotify results are unverified, and do not try the re-auth flow.

**Credentials**: Stored in `~/code/dj-buyer/state.db` (table: `spotify_auth`)
**Client ID**: `43d9bf46f34d48bb80cc23803c8db2a8`
**Account**: see Contacts.app for owner details

## Quick Commands

```bash
cd ~/code/dj-buyer && uv run dj-buyer auth           # Check/trigger re-auth
cd ~/code/dj-buyer && uv run dj-buyer list-tracks <playlist_id>
cd ~/code/dj-buyer && uv run dj-buyer poll           # Poll playlist for new tracks
```

## Token Management

**Token storage**: Access token, refresh token, and expiry in `~/code/dj-buyer/state.db` (`spotify_auth` table). Refresh token also backed up to macOS Keychain: service `spotify-refresh-token`, account `dj-buyer`.

**Auto-refresh**: `get_spotify_client()` refreshes automatically when within 5 minutes of expiry.

**Manual token refresh** (if token stale but refresh token still valid):
```python
import sqlite3, time, requests, base64
# Run from ~/code/dj-buyer
from src.dj_buyer.spotify.auth import get_spotify_secret

client_id = '43d9bf46f34d48bb80cc23803c8db2a8'
client_secret = get_spotify_secret()
db = sqlite3.connect('state.db')
refresh_token = db.execute('SELECT refresh_token FROM spotify_auth LIMIT 1').fetchone()[0]

creds = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
resp = requests.post('https://accounts.spotify.com/api/token',
    headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'},
    data={'grant_type': 'refresh_token', 'refresh_token': refresh_token})
data = resp.json()
expires_at = int(time.time()) + data['expires_in']
db.execute('UPDATE spotify_auth SET access_token=?, expires_at=?', (data['access_token'], expires_at))
db.commit()
```

**Keychain recovery**:
```bash
security find-generic-password -a "dj-buyer" -s "spotify-refresh-token" -w
```

**Token health check**: Before background Spotify tasks, validate token first. If expired and refresh fails, notify admin via SMS with re-auth URL — don't silently fail.

## Re-auth Flow (when refresh token is revoked)

Redirect URI: `http://127.0.0.1:5432/api/spotify_callback` — **CRITICAL: must be `127.0.0.1` not `localhost`**

1. Generate auth URL and SMS to admin:
   ```bash
   cd ~/code/dj-buyer && uv run python -c "
   from src.dj_buyer.config import Config
   from src.dj_buyer.spotify.auth import get_auth_url
   print(get_auth_url(Config.load()))
   "
   ```
2. Admin opens URL on phone, logs in, gets redirected to `http://127.0.0.1:5432/...?code=XXX` (won't load)
3. Admin copies full redirect URL from address bar and pastes back via SMS
4. Exchange code for tokens:
   ```bash
   CLIENT_SECRET=$(cd ~/code/dj-buyer && uv run python -c "from src.dj_buyer.config import get_spotify_secret; print(get_spotify_secret())")
   CREDS=$(echo -n "43d9bf46f34d48bb80cc23803c8db2a8:$CLIENT_SECRET" | base64)
   curl -s -X POST "https://accounts.spotify.com/api/token" \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -H "Authorization: Basic $CREDS" \
     -d "grant_type=authorization_code&code=AUTH_CODE&redirect_uri=http://127.0.0.1:5432/api/spotify_callback"
   ```
5. Save to state.db and keychain:
   ```bash
   EXPIRES_AT=$(($(date +%s) + 3600))
   sqlite3 ~/code/dj-buyer/state.db "DELETE FROM spotify_auth; INSERT INTO spotify_auth (access_token, refresh_token, expires_at) VALUES ('ACCESS_TOKEN', 'REFRESH_TOKEN', $EXPIRES_AT);"
   security add-generic-password -a "dj-buyer" -s "spotify-refresh-token" -w "REFRESH_TOKEN" -U
   ```

## Working API Endpoints

```python
sp.track(id)                  # Full track metadata
sp.tracks([ids])              # Batch, up to 50 IDs
sp.artist(id)                 # Artist metadata + genres + followers
sp.artist_top_tracks(id)      # Top 10 tracks by popularity
sp.artist_albums(id)          # Full discography
sp.album(id)                  # Album metadata + label + copyrights
sp.album_tracks(id)           # Track listing for album
sp.search(q, type)            # Types: track, artist, album, playlist
sp.playlist(id)               # Playlist name, tracks, owner
sp.current_user_saved_tracks()# User's liked songs
sp.current_user_playlists()   # User's playlists
sp.user_playlist_create(user, name, public, description)  # Create playlist
sp.playlist_add_items(playlist_id, [track_uris])          # Add tracks
sp.me()                       # User profile
```

## Deprecated/Blocked Endpoints (don't use)

| Endpoint | Status | Replacement |
|----------|--------|-------------|
| `sp.audio_features(ids)` | **403** | ReccoBeats API |
| `sp.audio_analysis(id)` | **403** | ReccoBeats API |
| `sp.recommendations(seed_tracks)` | **404** | See Music Discovery Sources below |
| `sp.artist_related_artists(id)` | **404** | Last.fm API |
| `sp.current_user_recently_played()` | **403** | Scope not in auth |
| `sp.current_user_top_tracks()` | **403** | Scope not in auth |

## ReccoBeats API (BPM, Key, Energy — Spotify Replacement)

Free, no auth needed. Accepts Spotify track IDs.

```bash
# Track metadata
curl -s "https://api.reccobeats.com/v1/track?ids=SPOTIFY_ID1,SPOTIFY_ID2"

# Audio features (BPM, key, energy, etc.)
curl -s "https://api.reccobeats.com/v1/audio-features?ids=SPOTIFY_ID1,SPOTIFY_ID2"
```

Returns per track: `tempo` (BPM), `key` (0=C...11=B), `mode` (0=minor, 1=major), `energy`, `danceability`, `valence`, `acousticness`, `instrumentalness`, `liveness`, `loudness`, `speechiness`

Batch limit: 50 IDs per request. Results in `content` array.

Key mapping: 0=C, 1=C#/Db, 2=D, 3=D#/Eb, 4=E, 5=F, 6=F#/Gb, 7=G, 8=G#/Ab, 9=A, 10=A#/Bb, 11=B

## Creating Playlists

```python
import spotipy, sqlite3, time

db = sqlite3.connect('/Users/sven/code/dj-buyer/state.db')
row = db.execute('SELECT access_token, expires_at FROM spotify_auth LIMIT 1').fetchone()
sp = spotipy.Spotify(auth=row[0])

user_id = sp.me()['id']

# Create playlist
playlist = sp.user_playlist_create(
    user=user_id,
    name="Discover by Sven",
    public=True,
    description="Underground house music curated by Sven"
)
playlist_id = playlist['id']

# Add tracks (list of spotify:track:ID URIs)
sp.playlist_add_items(playlist_id, track_uris)
```

---

# Music Discovery & Recommendation Sources

When Spotify's recommendation endpoints are unavailable, use these sources to find new tracks.

## Tier 1: Programmatic APIs (automatable)

### Beatport API
- **What**: Electronic music store. Genre charts, new releases, artist/label catalogs
- **API**: OAuth REST API — requires approval at `oauth-api.beatport.com`
- **Cost**: Free API; $1.49–2.49/track to buy
- **Best for**: Real-time bestseller charts by genre, discovering what DJs are actually buying
- **DJ relevance**: Core signal for electronic music

### Last.fm API
- **What**: Scrobbling-based taste profiling. Artist similarity via collaborative filtering
- **API**: Free public REST API — `ws.audioscrobbler.com/2.0/`
- **Cost**: Free
- **Best for**: Artist similarity, tag-based discovery
- **Key endpoints**:
  ```bash
  # Similar artists
  curl "http://ws.audioscrobbler.com/2.0/?method=artist.getSimilar&artist=ARTIST&api_key=KEY&format=json"
  # Top tracks by tag
  curl "http://ws.audioscrobbler.com/2.0/?method=tag.getTopTracks&tag=house&api_key=KEY&format=json"
  # Similar tracks
  curl "http://ws.audioscrobbler.com/2.0/?method=track.getSimilar&artist=ARTIST&track=TITLE&api_key=KEY&format=json"
  ```

### Discogs API
- **What**: Database of 15M+ releases, labels, artists. Best for catalog research
- **API**: Free public REST API — `api.discogs.com`
- **Cost**: Free
- **Best for**: All releases on a label, genre/style tag browsing, quality signal (community ratings)
- **Key endpoints**:
  ```bash
  # All releases on a label
  curl "https://api.discogs.com/labels/LABEL_ID/releases"
  # Search by genre/style
  curl "https://api.discogs.com/database/search?genre=Electronic&style=Deep+House&format=Vinyl"
  ```

### SoundCloud API
- **What**: Promo/unreleased tracks from labels, often pre-release
- **API**: REST API — requires app approval
- **Best for**: Discovering tracks circulating as promos before store release

### AudD API (Track ID from Audio)
- **What**: Audio fingerprinting — identify tracks from audio samples
- **API**: `api.audd.io` — 100 req/month free; ~$0.001–0.005/req paid
- **Best for**: ID'ing tracks heard in Boiler Room sets, mixes, radio shows
- **Example**:
  ```bash
  curl -F "url=https://example.com/audio_sample.mp3" -F "api_token=TOKEN" https://api.audd.io/
  ```

### MusicBrainz API
- **What**: Open music encyclopedia. ISRCs, label relationships, cover/remix relationships
- **API**: Free REST API — `musicbrainz.org/ws/2/` (1 req/sec without auth)
- **Best for**: Canonical metadata enrichment, finding remixes/covers

### Tunebat API
- **What**: BPM + Camelot key data for 70M+ tracks
- **API**: Commercial — `tunebat.com/API`
- **Best for**: Filtering recommendations by BPM range and harmonic key compatibility

### Soundcharts API
- **What**: Audio features (BPM, key, energy) + chart performance + streaming stats. Resolves by ISRC
- **API**: Commercial — `soundcharts.com`
- **Best for**: Combining audio features + chart/popularity to identify rising tracks

## Tier 2: Editorial / Curatorial (high quality signal)

### Resident Advisor (RA) DJ Charts
- **URL**: `ra.co/dj/[artist]/charts`
- **What**: Working DJs submit charts of tracks they're playing
- **Best for**: What underground DJs are actually playing right now — gold standard

### Boomkat
- **URL**: `boomkat.com`
- **What**: UK specialist store, tight curated selection — quality filter
- **Best for**: Leftfield, experimental, techno, dub, ambient, post-club

### Bandcamp Daily
- **URL**: `daily.bandcamp.com`
- **What**: Bandcamp's editorial — genre roundups, label spotlights
- **Best for**: Independent/underground releases 3–6 months before Beatport

### NTS Radio Tracklists
- **URL**: `nts.live`
- **What**: 700+ resident DJs globally, all shows archived with tracklists
- **Best for**: Discovering what DJs across global underground are playing

### Juno Download
- **URL**: `junodownload.com`
- **What**: UK underground store. Weekly "Juno Recommends" chart
- **Best for**: Techno, D&B, jungle, grime, UK bass

### Traxsource
- **URL**: `traxsource.com`
- **What**: House-focused store. Charts influential in deep/afro/tech house
- **Best for**: House music specifically

### Boiler Room Set Tracklists
- **URL**: `boilerroom.tv`
- **What**: Archived DJ sets from underground clubs. Track IDs in comments
- **Best for**: What artists play in clubs

### FACT Magazine
- **URL**: `factmag.com`
- **What**: Reviews, premieres, mix series. Bass, techno, house, experimental
- **Best for**: Track premieres before wide release

### XLR8R
- **URL**: `xlr8r.com`
- **What**: Electronic music magazine since 1993
- **Best for**: Underground electronic, especially techno and experimental

### Rate Your Music (RYM)
- **URL**: `rateyourmusic.com`
- **What**: 1.3M+ users rating releases with ultra-granular micro-genre taxonomy
- **Best for**: Quality browsing by hyper-specific genre tags

### Every Noise at Once
- **URL**: `everynoise.com`
- **What**: 6,000+ genre map. **Frozen December 2023** (creator laid off from Spotify)
- **Best for**: Static reference for understanding genre relationships

## Tier 3: Community Signals

### Reddit Genre Communities
- r/techno, r/deephouse, r/jungle, r/drumnbass, r/electronicmusic, r/ambientmusic, r/DJs, r/listentothis
- **API**: Reddit API (rate-limited, requires auth)
- **Best for**: Early buzz from specialists before charts

## Recommended Pipeline for Track Discovery

| Stage | Source | Signal |
|-------|--------|--------|
| **Real-time buy signal** | Beatport genre charts | What DJs are buying now |
| **Tastemaker signal** | RA DJ charts (scrape) | What working DJs are playing |
| **Early discovery** | SoundCloud + Bandcamp Daily | Pre-release promos |
| **Track ID from mixes** | AudD API | ID tracks from Boiler Room/NTS sets |
| **Label deep-dive** | Discogs API | All releases from a quality label |
| **BPM/Key filter** | ReccoBeats | Harmonic compatibility check |
| **Quality filter** | Boomkat editorial | Underground quality bar |
| **Artist similarity** | Last.fm API | What to explore next from liked artists |

---

# Nikhil's Taste Profile (as of Apr 2026)

Analyzed 4,268 liked songs across 3,696 unique artists.

## Genre Breakdown

| Rank | Macro Genre | Key Tags | Approx Tag-Hits |
|------|-------------|----------|-----------------|
| 1 | **UK Bass ecosystem** | bass music, dubstep, drumstep, bassline, uk garage, uk funky, grime, stutter house, riddim, bass house, future bass | ~2,800 |
| 2 | **Drum & Bass / Jungle** | drum and bass, jungle, liquid funk, breakbeat | ~1,830 |
| 3 | **Ambient / Experimental / Downtempo** | dub, glitch, downtempo, IDM, trip-hop, lo-fi, footwork | ~1,040 |
| 4 | **House / Techno** | tech house, house, melodic house, melodic techno, g-house | ~505 |
| 5 | **Hip Hop / R&B** | jazz rap, alt R&B, alt hip hop, experimental hip hop, nu jazz | ~318 |
| 6 | **Classical** | classical, classical piano | ~183 |

**tl;dr**: Fundamentally a UK bass/DnB/jungle head with a strong ambient/experimental side. House is the *smallest* electronic bucket — playlists pushing into house are intentionally expanding his range.

## Analysis Methodology

Spotify killed audio features (BPM, energy, danceability) in Nov 2024, so genre-by-artist is the primary signal available.

```python
import sqlite3, spotipy
from collections import Counter

db = sqlite3.connect('/Users/sven/code/dj-buyer/state.db')  # adjust path
row = db.execute('SELECT access_token FROM spotify_auth LIMIT 1').fetchone()
sp = spotipy.Spotify(auth=row[0])

# 1. Fetch all liked songs (paginated)
all_tracks = []
offset = 0
while True:
    results = sp.current_user_saved_tracks(limit=50, offset=offset)
    items = results['items']
    if not items: break
    all_tracks.extend(items)
    offset += len(items)
    if len(items) < 50: break

# 2. Collect unique artist IDs
artist_ids = set()
for item in all_tracks:
    if item['track']:
        for a in item['track']['artists']:
            artist_ids.add(a['id'])

# 3. Batch-fetch artist genres (50 at a time)
artist_genres = {}
for i in range(0, len(artist_ids), 50):
    batch = list(artist_ids)[i:i+50]
    for artist in sp.artists(batch)['artists']:
        if artist:
            artist_genres[artist['id']] = artist.get('genres', [])

# 4. Count genre occurrences weighted by liked tracks
genre_counter = Counter()
for item in all_tracks:
    if item['track']:
        for a in item['track']['artists']:
            for g in artist_genres.get(a['id'], []):
                genre_counter[g] += 1

# 5. Top genres
for genre, count in genre_counter.most_common(40):
    print(f'{count:3d}  {genre}')
```

**Note**: Spotify tags *artists* not individual tracks. Each liked track gives +1 to all genres of all its artists. For per-track audio features (BPM, key, energy), use ReccoBeats API instead.
