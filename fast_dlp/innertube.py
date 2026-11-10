"""InnerTube /player requests for the VISIONOS and TVHTML5 clients."""
from dataclasses import dataclass

from . import net
from .utils import debug

PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"

VISION_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/26.0 Safari/605.1.15")
TIZEN_UA = ("Mozilla/5.0 (SMART-TV; Linux; Tizen 2.4.0) AppleWebKit/538.1 "
            "(KHTML, like Gecko) Version/2.4.0 TV Safari/538.1")


@dataclass(frozen=True)
class Client:
    name: str
    name_id: int
    version: str
    os_name: str
    os_version: str
    user_agent: str
    device_make: str
    device_model: str


VISIONOS = Client("VISIONOS", 101, "1.02", "visionOS", "26.5.23O471", VISION_UA,
                  "Apple", "RealityDevice17,1")


def tv_client(version):
    return Client("TVHTML5", 7, version, "Tizen", "2.4.0", TIZEN_UA, "Samsung", "UKS9800")


def player(client, video_id, visitor_data=None, oauth_token=None, signature_timestamp=0):
    ctx = {
        "clientName": client.name,
        "clientVersion": client.version,
        "osName": client.os_name,
        "osVersion": client.os_version,
        "hl": "en",
        "gl": "US",
        "userAgent": client.user_agent,
        "deviceMake": client.device_make,
        "deviceModel": client.device_model,
    }
    if visitor_data:
        ctx["visitorData"] = visitor_data
    body = {
        "context": {"client": ctx},
        "videoId": video_id,
        "contentCheckOk": True,
        "racyCheckOk": True,
    }
    if signature_timestamp:
        body["playbackContext"] = {"contentPlaybackContext": {
            "html5Preference": "HTML5_PREF_WANTS",
            "signatureTimestamp": signature_timestamp,
        }}
    headers = {
        "user-agent": client.user_agent,
        "origin": "https://www.youtube.com",
        "referer": "https://www.youtube.com/",
        "x-youtube-client-name": str(client.name_id),
        "x-youtube-client-version": client.version,
    }
    if visitor_data:
        headers["x-goog-visitor-id"] = visitor_data
    if oauth_token:
        headers["authorization"] = "Bearer " + oauth_token
    status, data = net.post_json(PLAYER_URL, body, headers)
    debug(f"{client.name} player {video_id}: HTTP {status}, "
          f"{data.get('playabilityStatus', {}).get('status')}")
    if status != 200:
        raise IOError(f"player endpoint answered {status} for {client.name}")
    return data


class Playability:
    def __init__(self, response):
        ps = response.get("playabilityStatus") or {}
        self.status = ps.get("status")
        self.reason = ps.get("reason") or ""
        sub = ps.get("errorScreen", {}).get("playerErrorMessageRenderer", {}).get("subreason", {})
        self.subreason = "".join(r.get("text", "") for r in sub.get("runs", [])) or sub.get("simpleText", "")

    @property
    def ok(self):
        return self.status == "OK"

    @property
    def bot_check(self):
        text = (self.reason + " " + self.subreason).lower()
        return "not a bot" in text

    @property
    def needs_account(self):
        return not self.bot_check and self.status in (
            "LOGIN_REQUIRED", "AGE_VERIFICATION_REQUIRED", "AGE_CHECK_REQUIRED")

    def __str__(self):
        out = str(self.status)
        if self.reason:
            out += " - " + self.reason
        if self.subreason:
            out += f" ({self.subreason})"
        return out
