"""What happens around a YouTube download: name fields, subtitles, chapters, SponsorBlock,
side files (info.json, description, thumbnail, comments) and the download archive."""
import json
import os
import string
import sys
import threading

from . import formats as fmt
from . import net, utils, ytmeta
from .utils import warn

# -- output template ------------------------------------------------------------------------

DATE_FIELDS = ("upload_date", "date", "year")
TEMPLATE_FIELDS = ("title", "id", "author", "uploader", "channel", "channel_id", "album", "artist",
                   "playlist", "index", "playlist_index", "height", "width", "fps", "resolution", "vcodec",
                   "acodec", "ext", "duration", "duration_string", "view_count", "upload_date", "date",
                   "year", "site", "track_number")


class _Formatter(string.Formatter):
    """str.format with every value made file-name safe after formatting; unknown fields -> NA."""

    def get_value(self, key, args, kwargs):
        value = kwargs.get(key)
        return "NA" if value is None or value == "" and key not in ("playlist", "index", "album") else value

    def format_field(self, value, spec):
        try:
            text = format(value, spec) if spec else str(value)
        except (ValueError, TypeError):
            text = str(value)
        return utils.sanitize_filename(text, edges=False)


FORMATTER = _Formatter()


def template_fields(template):
    return {f for _, f, _, _ in FORMATTER.parse(template) if f}


def fields(video, picked, ext, job, upload_date=None):
    v = next((f for f in picked if fmt.kind(f) == "video"), None)
    a = next((f for f in picked if fmt.kind(f) == "audio"), None)
    track = job.get("track") or {}
    details = (getattr(video, "raw", None) or {}).get("videoDetails") or {}
    duration = int(details.get("lengthSeconds") or track.get("duration") or 0)
    out = {
        "title": video.title, "id": video.id, "author": video.author, "uploader": video.author,
        "channel": video.author, "channel_id": details.get("channelId"),
        "album": track.get("album") or job.get("playlist") or "", "artist": video.author,
        "playlist": job.get("playlist") or "", "index": job.get("index") or "",
        "playlist_index": job.get("position"), "track_number": track.get("track_number"),
        "height": v.get("height") if v else "audio", "width": v.get("width") if v else None,
        "fps": v.get("fps") if v else None,
        "resolution": f"{v.get('width')}x{v.get('height')}" if v else "audio only",
        "vcodec": fmt.codec(v).split(".")[0] if v else "none", "acodec": fmt.codec(a).split(".")[0] if a else "none",
        "ext": ext, "duration": duration,
        "duration_string": f"{duration // 3600}-{duration % 3600 // 60:02d}-{duration % 60:02d}" if duration >= 3600
        else f"{duration // 60}-{duration % 60:02d}",
        "view_count": int(details["viewCount"]) if details.get("viewCount") else None,
        "site": job.get("site") or ("spotify" if track else "youtube"),
    }
    if upload_date:
        out.update(upload_date=upload_date, date=f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}",
                   year=upload_date[:4])
    return out


def render(template, values):
    try:
        name = FORMATTER.vformat(template, (), values)
    except (KeyError, ValueError, IndexError) as e:
        raise utils.FastDlpError(f"bad output template: {e}")
    stem, dot, ext = name.rpartition(".")
    return utils.sanitize_path(stem) + dot + ext if dot else utils.sanitize_path(name)


# -- subtitles, chapters, SponsorBlock --------------------------------------------------------

def wants_watch(args, template):
    return bool(args.embed_chapters or args.sponsorblock_mark or args.write_comments or args.write_info_json
                or template_fields(template) & set(DATE_FIELDS))


def subtitles(video, args):
    """[(lang, name, cues)] for --write-subs / --write-auto-subs / --embed-subs."""
    if not (args.write_subs or args.write_auto_subs or args.embed_subs):
        return []
    tracks, langs = ytmeta.caption_tracks(video.raw)
    if not tracks:
        utils.debug("no captions on this video")
        return []
    out = []
    for lang, track, translate in ytmeta.pick_captions(tracks, langs, args.sub_langs,
                                                       include_auto=args.write_auto_subs):
        vtt = ytmeta.fetch_caption(track, translate)
        if not vtt:
            warn(f"subtitles {lang}{' (translation)' if translate else ''}: YouTube refused "
                 f"{'the machine translation (rate limit)' if translate else 'the track'}")
            continue
        name = f"{track['name']} → {lang}" if translate else track["name"]
        out.append((lang, name, ytmeta.vtt_cues(vtt)))
    return out


def chapters(video, args, watch):
    if not (args.embed_chapters or args.sponsorblock_mark):
        return []
    details = video.raw.get("videoDetails") or {}
    duration = float(details.get("lengthSeconds") or 0)
    found = watch.chapters(duration) or ytmeta.description_chapters(details.get("shortDescription"), duration)
    if args.sponsorblock_mark:
        cats = list(ytmeta.SPONSOR_CATEGORIES) if args.sponsorblock_mark == "all" else args.sponsorblock_mark.split(",")
        segments = ytmeta.sponsorblock(video.id, cats)
        if segments:
            print(f"[sponsorblock] {len(segments)} segment(s) marked as chapters", file=sys.stderr)
        found = ytmeta.merge_sponsor_chapters(found, segments, duration)
    return found


def write_side_files(base, video, args, watch, subs, picked, chapters_list):
    written = []
    if subs and (args.write_subs or args.write_auto_subs):
        for lang, name, cues in subs:
            path = f"{base}.{lang}.{args.sub_format}"
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(ytmeta.to_srt(cues) if args.sub_format == "srt" else ytmeta.to_vtt(cues))
            written.append(path)
    details = video.raw.get("videoDetails") or {}
    if args.write_description:
        path = base + ".description"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(details.get("shortDescription") or "")
        written.append(path)
    if args.write_thumbnail:
        for name in ("maxresdefault", "hqdefault"):
            status, body = net.request(f"https://i.ytimg.com/vi/{video.id}/{name}.jpg", timeout=15)
            if status == 200 and len(body) > 1000:
                path = base + ".jpg"
                with open(path, "wb") as fh:
                    fh.write(body)
                written.append(path)
                break
    comments = None
    if args.write_comments:
        comments = watch.comments(args.max_comments)
        path = base + ".comments.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(comments, fh, ensure_ascii=False, indent=1)
        written.append(path)
    if args.write_info_json:
        info = {
            "id": video.id, "title": video.title, "uploader": video.author,
            "channel_id": details.get("channelId"), "duration": int(details.get("lengthSeconds") or 0),
            "view_count": int(details.get("viewCount") or 0), "upload_date": watch.upload_date(),
            "description": details.get("shortDescription"), "tags": details.get("keywords") or [],
            "is_live": bool(details.get("isLive")), "webpage_url": f"https://www.youtube.com/watch?v={video.id}",
            "thumbnails": (details.get("thumbnail") or {}).get("thumbnails") or [],
            "chapters": [{"start_time": s, "title": t} for s, t in chapters_list],
            "subtitles": [{"lang": l, "name": n, "cues": len(c)} for l, n, c in subs],
            "formats": [{"itag": f["itag"], "ext": fmt.ext(f), "codec": fmt.codec(f), "height": f.get("height"),
                         "fps": f.get("fps"), "bitrate": f.get("bitrate"), "filesize": fmt.size(f) or None}
                        for f in video.formats],
            "requested_formats": [f["itag"] for f in picked], "extractor_client": video.client,
        }
        if comments is not None:
            info["comment_count_fetched"] = len(comments)
        path = base + ".info.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(info, fh, ensure_ascii=False, indent=1)
        written.append(path)
    for p in written:
        print(f"[side] {os.path.basename(p)}", file=sys.stderr)


# -- download archive ------------------------------------------------------------------------

class Archive:
    """--download-archive: one 'site id' line per finished download, skipped next time."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.ids = set()
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.ids = {line.strip() for line in fh if line.strip()}

    @staticmethod
    def key(job):
        if job.get("track") and job["track"].get("id"):
            return f"{job.get('site', 'spotify')} {job['track']['id']}"
        if job["source"] == "direct":
            return f"{job['site']} {job['id']}"
        return f"youtube {job.get('video_id')}"

    def has(self, job):
        return bool(self.path) and self.key(job) in self.ids

    def add(self, job):
        if not self.path:
            return
        with self.lock:
            k = self.key(job)
            if k not in self.ids:
                self.ids.add(k)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(k + "\n")
