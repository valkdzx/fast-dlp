<p align="center">
  <img src="https://raw.githubusercontent.com/valkdzx/fast-dlp/main/assets/logo.svg" width="128" alt="fast-dlp">
</p>

<h1 align="center">fast-dlp</h1>

<p align="center"><b>English</b> · <a href="https://github.com/valkdzx/fast-dlp/blob/main/README.ru.md">Русский</a></p>

<p align="center">
  A fast alternative to yt-dlp. Pure Python, no dependencies.<br>
  YouTube · Spotify · Yandex Music · SoundCloud · TikTok · VK · Pinterest · Pornhub · Steam Workshop
</p>

- **Speed:** a 345 MiB 4K video downloads in 7.3 s (~47 MiB/s).
- **No ffmpeg:** video and audio are joined by a built-in muxer in under a second.
- **Music:** tags, cover art, lyrics and ReplayGain are written automatically.
- **Search:** one query searches every source at once.
- **Picker:** tick tracks in the terminal; they download in parallel.

---

## Install

Requires Python 3.9+.

```
pip install fast-dlp
```

Update: `pip install -U fast-dlp` (if pip keeps seeing the old version, add
`--no-cache-dir`). To keep the command isolated from other packages, install it with pipx:
`pipx install fast-dlp`.

From source:

```
git clone https://github.com/valkdzx/fast-dlp
cd fast-dlp
pip install -e .
```

Without installing: `py -m fast_dlp ...` or `fast-dlp.cmd ...` from the project folder.

**If `fast-dlp` is "not recognized as a command".** pip puts `fast-dlp.exe` into Python's
`Scripts` folder, which is often not on PATH: Python installed without "Add python.exe to
PATH", `pip install --user`, or Python from the Microsoft Store. Run once

```
python -m fast_dlp path
```

or just any download through `python -m fast_dlp ...`: fast-dlp adds that folder to your
user PATH itself, and plain `fast-dlp` works in a new terminal window. This happens once;
`FAST_DLP_NO_PATH=1` keeps PATH untouched, and inside a virtualenv it is never changed. On Linux and macOS fast-dlp prints the line to
add to `~/.profile`.

Optional programs:

- **ffmpeg** — only for MP3 and mixed containers;
- **Deno, Node.js or Bun** — only for age-restricted videos through `login`.

## Examples

```
fast-dlp https://youtu.be/VIDEO_ID                  # best video + audio
fast-dlp https://youtu.be/VIDEO_ID -q 1080          # up to 1080p
fast-dlp https://youtu.be/VIDEO_ID -x               # audio only
fast-dlp https://youtu.be/VIDEO_ID --audio-format mp3
fast-dlp https://youtu.be/VIDEO_ID -F               # list formats
fast-dlp https://youtu.be/VIDEO_ID -f 137+140       # exact itags
fast-dlp https://youtu.be/VIDEO_ID -g               # print stream URLs
fast-dlp https://open.spotify.com/track/TRACK_ID    # a Spotify track
fast-dlp https://open.spotify.com/album/ALBUM_ID    # an album, with the picker
fast-dlp "https://www.youtube.com/playlist?list=PLAYLIST_ID"
fast-dlp artist - title                             # search every source
fast-dlp URL -N 32 -P D:\Video -o "{author} - {title}.{ext}"
fast-dlp web                                        # downloader website at http://127.0.0.1:8000
```

---

## Downloader website

```
fast-dlp web                              # http://127.0.0.1:8000
fast-dlp web --host 0.0.0.0 --port 8080   # reachable from the local network
```

A black page: paste a link, pick the options, press "Download". The page follows the
browser's language (English or Russian); the RU · EN switch in the corner remembers your
choice, and server messages come in the same language.

The site takes everything `fast-dlp <link>` takes:

- YouTube: videos, playlists, channels, `ytsearch:`;
- Spotify and Yandex Music: tracks, albums, playlists (the audio is found on YouTube; tags
  and cover art come from the source);
- VK, SoundCloud, TikTok, Pinterest, Pornhub;
- direct links to files and `.m3u8`;
- plain words: then every source is searched.

Lists (playlist, album, search) come with checkboxes. The ticked items download three at a
time, each with its own one-time link. Files from other sites reach you as they are: a
direct file is fetched in parallel ranges, HLS segment by segment, in order. With ffmpeg on
the server any of them can be turned into MP3 on the way. Steam Workshop goes through
steamcmd, so it is command line only.

Options for YouTube:

- what to get: video with audio, audio only or video only;
- format: MP4 (H.264 / AV1 + AAC) or WebM (VP9 + Opus); for audio M4A, Opus or MP3;
- quality from 144p to 4K, with the size of each variant;
- codec and MP3 bitrate;
- a time section;
- tags, cover art, ReplayGain;
- a file name template;
- the number of connections.

**Nothing is stored on the server.** The file is streamed (HTTP streaming):

1. First only the formats' headers and indexes (`sidx` / `Cues`) are fetched. From them the
   whole file is planned ahead: a new header, the fragment order and the exact size, so the
   browser sees `Content-Length` right away and shows progress.
2. Fragments are fetched from googlevideo in parallel, within a bounded memory window
   (~80 MiB per download). Track numbers and timestamps are patched on the fly and
   everything goes straight into the response. The result is byte-identical to the file the
   built-in muxer would produce.
3. MP3 is encoded by ffmpeg straight from the stream and sent in chunks
   (`Transfer-Encoding: chunked`).

**Delivery confirmation.** After the last byte the server closes its side of the
connection and waits for the browser to close its side. A browser closes only after it has
read everything, so a file is marked "Delivered" only on real confirmation.

Other outcomes:

- a connection reset means "Stopped", and the same link can be opened again;
- if the browser stays silent for 60 s, the status is "Sent".

A download link is one-time and lives an hour. The page follows the status through
`/api/status`.

| Request | What it does |
|---|---|
| `GET /api/info?url=` | title, duration, every format with its size; or a list; or a file |
| `POST /api/jobs` | options (+ `entry` for a list item) → token, file name, exact size |
| `GET /dl/<token>` | the file itself, streamed |
| `GET /api/status?t=a,b` | download states: bytes sent, speed, status |

Send `X-Lang: en` or `X-Lang: ru` to choose the language of error messages.

Measured: a 232 MiB 4K video reaches the browser in 3.8 s (~60 MiB/s).

## Stream URLs for developers: `--get-url`

```
fast-dlp -g https://youtu.be/VIDEO_ID                # two lines: video, then audio
fast-dlp -g -x https://youtu.be/VIDEO_ID             # one line: the best audio
fast-dlp -g -q 720 https://youtu.be/VIDEO_ID
fast-dlp -g https://open.spotify.com/track/TRACK_ID  # audio of the track found on YouTube
fast-dlp -g LIVE_URL                                 # a live stream: its HLS playlist
fast-dlp -g https://example.com/video.m3u8           # other sites: the file or .m3u8
```

Instead of downloading, fast-dlp prints the stream URLs to stdout, one per line. Formats
are chosen exactly as for a download (`-f`, `-q`, `-x`, `--codec`). All logging goes to
stderr, so the output can go straight into a script:

```
url=$(fast-dlp -g -x https://youtu.be/VIDEO_ID)
curl -L -o audio.m4a "$url"
ffplay "$(fast-dlp -g -q 1080 https://youtu.be/VIDEO_ID | head -1)"
```

googlevideo URLs live a few hours and are bound to the IP that requested them. `Range`
requests work; a full GET is answered with a redirect, so curl needs `-L`.

## Why it is fast

- **One API request.** No watch page and no base.js: the `VISIONOS` InnerTube client
  returns direct URLs (up to 4K, no signature and no `n` parameter) in about 0.25 s.
- **Parallel download.** A file is fetched over 16 connections, each a persistent HTTPS
  connection.
- **Chunks sized to the network.** Each connection measures its own speed and takes the next
  chunk worth about 2 seconds:
  - at 60 Mbit/s that is ~1 MiB;
  - on gigabit it is 8 MiB, the ceiling: googlevideo throttles ranges larger than ~10 MB;
  - near the end chunks get smaller so that all connections finish together.
- **Straight into the final file.** YouTube video and audio are not downloaded into `.part`
  files and joined afterwards. From the formats' indexes (`sidx` / `Cues`) the final file is
  planned ahead, and each chunk is written straight to its place. Afterwards only the
  fragment headers (a few KiB) are patched. Every byte is downloaded and written once.
- **No writing zeros.** When a file grows, Windows physically writes zeros: 3 GB of zeros
  before the download looks like a "background download". The file is created sparse
  (NTFS sparse) and its size is set instantly.
- **MP3 without an intermediate file.** Audio goes from the network straight into ffmpeg.
- **No restarts.** An expired URL (HTTP 403) is renewed with a new `/player` request and the
  download continues where it was.

## IPv4 and IPv6

YouTube judges each address separately. A home connection shares one IPv4 address, while
every device has its own IPv6, and Windows prefers IPv6 by default. If YouTube answers an
IPv6 address with a bot check ("Sign in to confirm you're not a bot"), fast-dlp switches the
whole process to IPv4 and remembers that for 6 hours (`%LOCALAPPDATA%\fast-dlp\route.json`).
The whole process switches at once because stream URLs are bound to the address that
requested them. `-4` / `-6` choose the address family by hand.

## Age-restricted videos

If VISIONOS answers `LOGIN_REQUIRED`, fast-dlp moves to the `TVHTML5` client (Tizen,
version `5.<date>`) with the account's OAuth token:

- client_id and client_secret come from the `youtube.com/tv` bundle (cached 6 h);
- the refresh token is exchanged for an access token (cached until it expires);
- `signatureCipher` and `n` are solved through base.js: top-level side effects are cut out,
  and the URL builder with `.set("alr","yes")` is called in an external JS runtime.

```
fast-dlp login                      # device code at google.com/device
fast-dlp URL --refresh-token TOKEN  # or a ready refresh token (saved)
```

The token can also be passed in `FAST_DLP_REFRESH_TOKEN`.

## Spotify

```
fast-dlp https://open.spotify.com/track/TRACK_ID
fast-dlp https://open.spotify.com/playlist/PLAYLIST_ID -I 1-10,15
```

The audio comes from YouTube; Spotify only provides the track data. No keys are needed: the
data (with ISRC) comes through the CDisc backend
(`https://2281273.xyz/spotify?url=…&beta=true`, Spotify's raw JSON). Set your own address
with `FAST_DLP_SPOTIFY_BACKEND`.

You can also use your own Web API keys: create an app at
https://developer.spotify.com/dashboard and enter the keys with `fast-dlp spotify-setup`, or
set `FAST_DLP_SPOTIFY_ID` / `FAST_DLP_SPOTIFY_SECRET`. With keys, Spotify also takes part in
the all-source search.

Data sources are tried in order: your keys → the backend → the public embed page (no ISRC).

How a track is found on YouTube Music:

1. By **ISRC**. A result is accepted only if the duration differs by at most 5 s and the
   title or the artist match.
2. A search for "artist + title" with the "songs" filter: title and artist similarity and
   the duration difference (at most 15 s) are scored.
3. The same with the "videos" filter: music videos and re-uploads.

Playlist tracks from the backend have no cover or date, so these are fetched before each
track downloads. Spotify's editorial playlists (`37i9…`) are read from the embed page.

## Other sources

| Source | What it does | Sign-in |
|---|---|---|
| **Yandex Music** | track, album, playlist. With a token the file comes straight from Yandex (MP3 320 with Plus); without one the track is found on YouTube | `fast-dlp yandex-login`, optional |
| **VK Music** | track, playlist, album. AES-128 HLS is decrypted by the built-in AES; MP3 is taken out of MPEG-TS without re-encoding | `fast-dlp vk-setup` |
| **SoundCloud** | track, set (playlist/album), tags and 500×500 cover art | none |
| **TikTok** | videos without the watermark (h264, `--codec hevc` for h265), photo posts, `-x` for the original sound, short links | none |
| **Pinterest** | videos, stories, images in the original size | none |
| **Pornhub** | videos: the best MP4 up to `-q`, fetched in parallel ranges (the site throttles a single connection); HLS when there is no MP4. Where a country requires a real age check, fast-dlp says so and bypasses nothing | none |
| **Steam Workshop** | an item or a collection. Old items through a direct link, SteamPipe through `steamcmd`, downloaded automatically. Paid games: `--steam-user LOGIN`; the password and Steam Guard go into steamcmd once | for paid games |
| **Any file / .m3u8** | direct links over N ranged connections; HLS segments in parallel, with AES-128 | none |

Tokens live in `%APPDATA%\fast-dlp\` (`yandex.json`, `vk.json`); they can also be set with
`FAST_DLP_YANDEX_TOKEN` / `FAST_DLP_VK_TOKEN`.

AES is pure Python, checked against the FIPS-197 and NIST SP 800-38A vectors. One thread
does ~1.6 MiB/s, so large amounts are decrypted in a process pool on all cores.

## Search every source at once

```
fast-dlp artist - title
```

If the arguments contain no link, they are a search query. YouTube Music, Spotify, Yandex
Music, YouTube and SoundCloud are asked in parallel; the whole search takes about 1 s. The
results open in the picker: each shows its source and duration, and the best one is ticked.
With `-y` or without a terminal the best result downloads right away.

## Picking tracks in the terminal

An album or playlist link opens the picker. The picker and the download board follow the
system language: Russian on a Russian Windows/Linux/macOS, English elsewhere. Set it by hand
with `--lang ru` / `--lang en` or `FAST_DLP_LANG`.

```
 Spotify · “Album” · 12   11 of 12 selected
 ─ Tracks ─────────────────────────────────────────────
 ▸ [x]  1  Track 1 — Artist                        3:41
   [ ]  2  Track 2 — Artist                        4:05
 ─ Options ────────────────────────────────────────────
   Format             ◀ mp3 ▶
   MP3 quality        ◀ 320k ▶
   In parallel        ◀ 4 ▶
   Tags / Cover       ◀ yes ▶
   Already downloaded ◀ skip ▶
```

| Key | Action |
|---|---|
| ↑ ↓, Home/End, PgUp/PgDn | move |
| Space | tick / untick a track |
| ← → | on an option: change the value; on tracks: page |
| A / N / I | all / none / invert |
| Enter | download the ticked ones |
| Esc / Q | cancel |

After Enter a live board opens: each track has its own status (queued / downloading / done /
already there / failed), with the latest log lines at the bottom.

`-y` downloads everything without the picker, `--select` always opens it. With `-I` or
without a terminal it does not open.

## Playlists and albums

```
fast-dlp "https://www.youtube.com/playlist?list=PLAYLIST_ID"
fast-dlp https://open.spotify.com/album/ALBUM_ID
fast-dlp https://open.spotify.com/playlist/PLAYLIST_ID -I 1-10,15   # only these numbers
```

A YouTube playlist is read through InnerTube `/browse` with every continuation (913 videos in
6.8 s). Files go into a `{playlist}/` folder, numbered with `{index}`. Items download 4 at a
time (`-J`): a 14-track album as MP3 takes 18 s instead of 52. Files already downloaded are
skipped before any request to YouTube; `-w` downloads everything again.

## Playlist sync

```
fast-dlp sync https://open.spotify.com/playlist/PLAYLIST_ID
fast-dlp sync "https://www.youtube.com/playlist?list=PLAYLIST_ID" --sync-dir D:\Music\Mix -x
```

New tracks are downloaded, tracks removed from the playlist are deleted. Only files that
sync created are deleted (they are listed in `.fast-dlp-sync.json`); `--sync-keep` deletes
nothing. A `<playlist>.m3u8` is written next to them. A repeat run with no changes takes
under a second — handy for a scheduler.

## A video section without ffmpeg

```
fast-dlp URL --section 3:40-5:10
fast-dlp URL --section 1:00:00-                          # from one hour to the end
fast-dlp "https://youtu.be/VIDEO_ID?t=220" --section t   # from the t= mark in the link
```

From the format's index (`sidx` in MP4, `Cues` in WebM) only the bytes of the section are
downloaded, and the muxer shifts the time to zero. A 30 s section of a 4-minute 1080p video
is 13 MB instead of 80. Cuts are at keyframes, like `ffmpeg -c copy`, with no re-encoding.

## Lyrics and loudness

- **Lyrics** come from LRCLIB, synced (LRC) when available. On by default for music, with
  `--lyrics` for videos. Embedded in MP3 (`USLT`), M4A (`©lyr`) and WebM/MKV.
  `--write-lyrics` also saves a `.lrc`, `--no-lyrics` turns them off.
- **ReplayGain** without analysing the file: YouTube reports the track's loudness, and the
  ReplayGain 2.0 gain is computed from it (reference −18 LUFS). `--no-replaygain` turns it
  off.

## Subtitles, chapters, live streams (YouTube)

```
fast-dlp URL --embed-subs --sub-langs en,ru
fast-dlp URL --write-subs --write-auto-subs --sub-langs all --sub-format vtt
fast-dlp URL --embed-chapters --sponsorblock-mark all
fast-dlp URL --write-info-json --write-description --write-thumbnail --write-comments
fast-dlp LIVE_URL --live-duration 600              # record a live stream for 10 minutes
fast-dlp LIVE_URL --live-from-start                # from the start of the DVR window
fast-dlp "https://www.youtube.com/@CHANNEL"        # a channel
fast-dlp "ytsearch10:query" -x                     # YouTube search
fast-dlp URL --download-archive done.txt           # skip what was downloaded before
fast-dlp URL --proxy http://user:pass@host:8080
```

- **Subtitles** — manual and automatic, embedded without ffmpeg (MP4 `tx3g`, MKV SRT).
- **Chapters** — from the player's markers or timestamps in the description;
  **SponsorBlock** marks sponsors, intros and so on.
- **Live streams** are recorded through HLS until the stream ends or Ctrl+C.
- **Fallback HLS** up to 4K kicks in by itself if direct URLs fail (`--hls` forces it).
- **Side files:** `.info.json`, `.description`, `.jpg`, `.comments.json`.
- **Name template:** `{title} {id} {uploader} {channel} {upload_date} {year} {duration}
  {height} {resolution} {playlist} {playlist_index} {album} {artist} {track_number} {site}
  {ext}` and more. Python formatting works: `{playlist_index:03d}`. An unknown field becomes
  `NA`, `/` makes folders.

## Tags and cover art

Tags are written without ffmpeg:

- **MP4/M4A** — iTunes atoms (`©nam ©ART ©alb aART ©day trkn ©cmt covr`);
- **WebM** — Matroska `Tags`, cover art as an attachment;
- **MP3** — ID3v2.3, cover art in an APIC frame.

By default cover art goes only into audio; `--embed-thumbnail` adds it to video too,
`--no-cover` / `--no-metadata` turn it off.

## MP3

```
fast-dlp URL --audio-format mp3                 # 320k CBR
fast-dlp URL --audio-format mp3 --audio-quality V0
```

YouTube does not serve MP3, so ffmpeg (libmp3lame) is needed: a three-minute track encodes
in ~2 s. `--audio-format opus` / `m4a` take the stream without re-encoding.

## Joining without ffmpeg

Usually there is no separate joining step: the file is put together while it downloads (see
"Why it is fast"). The built-in muxer works when embedded subtitles or chapters are needed,
with `--keep-parts` and for the HLS fallback. It only copies data and never re-encodes:

- **WebM + WebM** (vp9/av01 + opus): clusters are interleaved by time, Cues and SeekHead are
  rewritten.
- **MP4 + M4A** (fragmented): one `moov` with two tracks, `moof+mdat` interleaved by time.

| What | Size | Time |
|---|---|---|
| 4K60 vp9 + opus → .webm | 1.3 GB | 0.90 s |
| 4K vp9 + opus → .webm | 345 MiB | 0.14 s |
| 1080p60 avc1 + aac → .mp4 | 255 MiB | 0.11 s |

The streams are bit-identical to the originals. ffmpeg is needed only for mixed containers
(`-f 398+251`) or with `--merger ffmpeg`.

## Tests

```
py -m unittest discover -s tests -t .                              # offline tests, ~4 s
set FAST_DLP_NETWORK=1 && py -m unittest discover -s tests -t .    # + live checks
```

## Layout

| File | What it does |
|---|---|
| `fast_dlp/innertube.py` | VISIONOS / TVHTML5 clients, the `/player` request |
| `fast_dlp/oauth.py` | device-code sign-in, refresh |
| `fast_dlp/cipher.py` | base.js preparation, sig/n solver |
| `fast_dlp/extractor.py` | VISIONOS → (age-restricted) → TV + OAuth |
| `fast_dlp/download.py` | multi-connection ranged download |
| `fast_dlp/formats.py` | format selection, the `-F` table |
| `fast_dlp/mux.py` | WebM and fMP4 joining, tags and cover art |
| `fast_dlp/mp3.py` | MP3 through ffmpeg, own ID3v2.3 |
| `fast_dlp/playlist.py` | playlists, channels, YouTube search |
| `fast_dlp/spotify.py` | Spotify: own keys / CDisc backend / embed page |
| `fast_dlp/ytmusic.py` | YouTube Music search and track matching |
| `fast_dlp/sources/` | Yandex, VK, SoundCloud, TikTok, Pinterest, Pornhub, Steam, any file |
| `fast_dlp/hls.py` | HLS, AES-128, live recording |
| `fast_dlp/ytmeta.py` | date, chapters, comments, subtitles, SponsorBlock |
| `fast_dlp/search.py` | search across every source |
| `fast_dlp/section.py` | time sections |
| `fast_dlp/sync.py` | playlist sync |
| `fast_dlp/lyrics.py` | LRCLIB lyrics, ReplayGain |
| `fast_dlp/postprocess.py` | name template, side files, archive |
| `fast_dlp/aes.py` | pure-Python AES-128-CBC |
| `fast_dlp/ts.py` | MPEG-TS parsing |
| `fast_dlp/tui.py` | the picker and the download board |
| `fast_dlp/i18n.py` | terminal interface language |
| `fast_dlp/pathfix.py` | puts the `fast-dlp` command on PATH |
| `fast_dlp/stream.py` | streamed files: plan from indexes, parallel fragments, on-the-fly patches |
| `fast_dlp/web/` | the website: server, one-time links, delivery confirmation, page in RU/EN; `targets.py` — every CLI source and streaming of direct files and HLS |
| `fast_dlp/cli.py` | the command line |

Cache: `%LOCALAPPDATA%\fast-dlp`. Tokens and settings: `%APPDATA%\fast-dlp`.

## License

MIT, see [LICENSE](https://github.com/valkdzx/fast-dlp/blob/main/LICENSE).
