"""Turn a video id into playable formats: VISIONOS first, TVHTML5+OAuth for age-gated videos."""
import json
import os
import re
import socket
import sys
import threading
import time

from . import innertube, net
from .cipher import DEFAULT_REMOTE, Cipher
from .oauth import TvAuth
from .utils import FastDlpError, cache_dir, debug


class Video:
    def __init__(self, video_id, response, client, refetch):
        details = response.get("videoDetails") or {}
        self.id = video_id
        self.title = details.get("title") or video_id
        self.author = details.get("author") or ""
        self.duration = int(details.get("lengthSeconds") or 0)
        self.client = client
        self.formats = [f for f in response.get("streamingData", {}).get("adaptiveFormats", []) if f.get("url")]
        self.raw = response
        self._refetch = refetch

    def fresh_url(self, itag):
        """Re-request the player and return a new URL for itag (used on HTTP 403)."""
        video = self._refetch()
        for f in video.formats:
            if f["itag"] == itag:
                return f["url"]
        raise FastDlpError(f"itag {itag} disappeared on refresh")


class Visitor:
    """visitorData cache: VISIONOS answers some videos with a bot check without one."""
    TTL = 3600
    _PAGE = re.compile(r'"visitorData":"([^"]+)"')

    def __init__(self):
        self.path = os.path.join(cache_dir(), "visitor.json")

    def get(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if time.time() - data["at"] < self.TTL:
                return data["visitorData"]
        except (OSError, ValueError, KeyError):
            pass
        return None

    def keep(self, visitor_data):
        if not visitor_data or visitor_data == self.get():
            return
        # Atomic replace: parallel jobs may read this file while another one writes it.
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"visitorData": visitor_data, "at": time.time()}, fh)
            os.replace(tmp, self.path)
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)

    def mint(self):
        try:
            page = net.get_text("https://www.youtube.com/", {"Accept-Language": "en-US,en;q=0.9"})
        except OSError:
            return None
        found = self._PAGE.search(page)
        # JSON-escaped in the page; read it back as JSON (as CDisc-Backend does).
        return json.loads('"%s"' % found.group(1)) if found else None


class Extractor:
    def __init__(self, refresh_token=None, js_runtime=None, client="auto",
                 cipher_mode="auto", cipher_url=None):
        self.auth = TvAuth(refresh_token)
        self.cipher = Cipher(js_runtime, cipher_mode, cipher_url or DEFAULT_REMOTE)
        self.visitor = Visitor()
        self.client = client
        self._ip_flagged = False
        self.auto_ipv4 = True
        self._route_path = os.path.join(cache_dir(), "route.json")
        if net.family() == 0 and self._remembered_ipv4():
            debug("using IPv4 (remembered: IPv6 was bot-checked)")
            net.set_family(socket.AF_INET)

    ROUTE_TTL = 6 * 3600

    def _remembered_ipv4(self):
        try:
            with open(self._route_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data.get("family") == 4 and time.time() - data.get("at", 0) < self.ROUTE_TTL
        except (OSError, ValueError):
            return False

    def _remember_ipv4(self):
        with open(self._route_path, "w", encoding="utf-8") as fh:
            json.dump({"family": 4, "at": time.time()}, fh)

    def _can_switch(self):
        return self.auto_ipv4 and net.family() == 0 and net.has_ipv4()

    @property
    def ip_flagged(self):
        """True once VISIONOS bot-checked this run even with fresh visitor data."""
        return self._ip_flagged

    def extract(self, video_id):
        if self.client in ("auto", "visionos"):
            t = time.time()
            can_switch = self._can_switch()
            # With IPv4 to fall back on, no visitor-data retries on the default route:
            # switching address is the cheaper and likelier fix.
            video, play = self._visionos(video_id, retry_visitors=not can_switch)
            if play.bot_check and can_switch:
                # The default route (usually this machine's own IPv6 address) is the
                # one being bot-checked; the shared IPv4 address is judged separately.
                debug("bot check on the default route; switching to IPv4")
                net.set_family(socket.AF_INET)
                video, play = self._visionos(video_id, fresh_visitor=True)
                if play.bot_check:
                    net.set_family(0)
                    self.auto_ipv4 = False
                else:
                    self._remember_ipv4()
                    print("note: YouTube bot-checks this machine's IPv6 address; using IPv4 "
                          "(remembered for 6 hours)", file=sys.stderr)
            debug(f"VISIONOS answered in {time.time() - t:.2f}s")
            if play.ok and video.formats:
                return video
            # A bot check is about this IP, not the video: the signed-in TV client still gets
            # through it. Without an account there is nothing else to try.
            if play.bot_check:
                self._ip_flagged = True
            fallback = play.needs_account or play.ok or (play.bot_check and self.auth.has_token)
            if self.client == "visionos" or not fallback:
                hint = (" (YouTube is bot-checking this IP; `fast-dlp login` enables the TV-client "
                        "fallback)" if play.bot_check else "")
                raise FastDlpError(f"YouTube will not play {video_id}: {play}{hint}")
            debug(f"VISIONOS refused ({play}); trying TV with the account")
        return self._tv(video_id)

    def _visionos(self, video_id, fresh_visitor=False, retry_visitors=True):
        """VISIONOS player call; on a bot check, retry with fresh visitorData."""
        visitor = (self.visitor.mint() or self.visitor.get()) if fresh_visitor else self.visitor.get()
        response = innertube.player(innertube.VISIONOS, video_id, visitor_data=visitor)
        play = innertube.Playability(response)
        if play.bot_check and retry_visitors and not self._ip_flagged:
            candidates = [response.get("responseContext", {}).get("visitorData")]
            for candidate in candidates + [None]:
                if candidate is None:
                    candidate = self.visitor.mint()
                if not candidate or candidate == visitor:
                    continue
                debug("bot check: retrying with new visitorData")
                response = innertube.player(innertube.VISIONOS, video_id, visitor_data=candidate)
                play = innertube.Playability(response)
                if not play.bot_check:
                    visitor = candidate
                    break
            # Fresh visitor data did not help: the IP is flagged. Skip these retries for
            # the rest of the run (one plain try per video is still made).
            self._ip_flagged = play.bot_check
        if not play.bot_check:
            self.visitor.keep(visitor or response.get("responseContext", {}).get("visitorData"))
        video = Video(video_id, response, "VISIONOS", lambda: self._visionos(video_id)[0])
        return video, play

    def _tv(self, video_id):
        t = time.time()
        token = self.auth.access_token()
        sts = self.cipher.signature_timestamp()
        client = innertube.tv_client(self.auth.client_version())
        response = innertube.player(client, video_id, oauth_token=token, signature_timestamp=sts)
        play = innertube.Playability(response)
        if not play.ok:
            raise FastDlpError(f"YouTube will not play {video_id} even signed in: {play}")
        formats = response.get("streamingData", {}).get("adaptiveFormats", [])
        if not formats:
            raise FastDlpError(f"TV client returned no formats for {video_id}")
        self.cipher.resolve_formats(formats)
        video = Video(video_id, response, "TVHTML5", lambda: self._tv(video_id))
        # The player can say OK and still hand out links googlevideo refuses (some label
        # and age-restricted videos), so check one to its last byte before promising it.
        if not self._serves(video.formats, client.user_agent):
            raise FastDlpError(f"googlevideo refuses the TV client's links for {video_id} (HTTP 403)")
        if video.title == video_id:
            # The TV client's videoDetails carry no title; oEmbed has it.
            status, body = net.request(f"https://www.youtube.com/oembed?format=json&url="
                                       f"https://www.youtube.com/watch?v={video_id}", timeout=10)
            if status == 200:
                meta = json.loads(body)
                video.title = meta.get("title") or video.title
                video.author = video.author or meta.get("author_name") or ""
        debug(f"TV path took {time.time() - t:.2f}s")
        return video

    @staticmethod
    def _serves(formats, user_agent):
        audio = [f for f in formats if f.get("mimeType", "").startswith("audio/")] or formats
        probe = max(audio, key=lambda f: f.get("bitrate") or 0)
        # The last byte, as CDisc-Backend does: a capped link serves the start and 403s the end.
        last = max(int(probe.get("contentLength") or 1) - 1, 0)
        status, _ = net.request(probe["url"], headers={"User-Agent": user_agent,
                                                       "Range": f"bytes={last}-{last}"}, timeout=10)
        debug(f"TV link probe: HTTP {status}")
        return status in (200, 206)
