import requests
import time

BASE_URL = "https://musicbrainz.org/ws/2/"
HEADERS = {
    "User-Agent": "CycleClassPlaylistBuilder/1.0 (glen@glencook.net)"
}

def get_bpm_by_isrc(isrc):
    """
    Query MusicBrainz using ISRC (most accurate)
    """
    url = f"{BASE_URL}recording/"
    params = {
        "query": f"isrc:{isrc}",
        "fmt": "json",
        "inc": "artist-credits"  # Optional: verify artist match
    }
    
    response = requests.get(url, params=params, headers=HEADERS)
    time.sleep(1)  # Rate limiting
    
    if response.status_code == 200:
        data = response.json()
        if data.get('recordings'):
            recording = data['recordings'][0]
            # BPM is in the recording object if available
            # Need to check the actual structure
            return recording
    return None

def get_bpm_by_title_artist(title, artist):
    """
    Fallback: search by title and artist
    """
    url = f"{BASE_URL}recording/"
    query = f'recording:"{title}" AND artist:"{artist}"'
    params = {
        "query": query,
        "fmt": "json",
        "limit": 5
    }
    
    response = requests.get(url, params=params, headers=HEADERS)
    time.sleep(1)
    
    if response.status_code == 200:
        return response.json()
    return None