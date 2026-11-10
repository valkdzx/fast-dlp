"""`fast-dlp sync <playlist/album link> [-P folder]`: keep a folder equal to a playlist.

New entries are downloaded, entries that left the playlist are deleted (only files this
command created - they are listed in .fast-dlp-sync.json), and `<name>.m3u8` lists the
files in playlist order. File names do not include the position, so reordering the
playlist never re-downloads anything. Run it as often as you like."""
import argparse
import json
import os
import sys
import threading

from . import utils
from .postprocess import Archive

STATE = ".fast-dlp-sync.json"


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def folder_for(args, title):
    if args.sync_dir:
        return args.sync_dir
    return os.path.join(args.paths, utils.sanitize_path(utils.sanitize_filename(title or "playlist")))


def run(jobs, args, run_jobs, label_of, duration_of, music):
    title = jobs[0].get("playlist") or "playlist"
    folder = folder_for(args, title)
    os.makedirs(folder, exist_ok=True)
    state_path = os.path.join(folder, STATE)
    state = _load(state_path)
    old = state.get("items") or {}

    job_args = argparse.Namespace(**vars(args))
    job_args.paths = folder
    job_args.select = False
    if not args.output:
        job_args.output = "{author} - {title}.{ext}" if music else "{title} [{id}].{ext}"
    files, lock = {}, threading.Lock()

    def on_file(job, path):
        with lock:
            files[Archive.key(job)] = os.path.relpath(path, folder)

    job_args.on_file = on_file
    current = [Archive.key(j) for j in jobs]
    print(f"[sync] «{title}» -> {folder}: {len(jobs)} in the playlist, {len(old)} known locally", file=sys.stderr)
    run_jobs(jobs, job_args)

    # Entries that left the playlist: their files go (only files this command made).
    removed = []
    wanted = set(current)
    for key, name in old.items():
        if key not in wanted:
            path = os.path.join(folder, name)
            if args.sync_keep:
                continue
            if os.path.isfile(path):
                os.remove(path)
                removed.append(name)
    items = {k: files.get(k) or old.get(k) for k in current if files.get(k) or old.get(k)}
    if args.sync_keep:
        items.update({k: v for k, v in old.items() if k not in wanted})

    m3u = os.path.join(folder, utils.sanitize_filename(title) + ".m3u8")
    with open(m3u, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"#EXTM3U\n#PLAYLIST:{title}\n")
        for job, key in zip(jobs, current):
            name = items.get(key)
            if name and os.path.isfile(os.path.join(folder, name)):
                fh.write(f"#EXTINF:{int(duration_of(job) or -1)},{label_of(job)}\n{name.replace(os.sep, '/')}\n")
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump({"source": title, "items": items}, fh, ensure_ascii=False, indent=1)
    added = len([k for k in current if k in files and k not in old])
    print(f"[sync] {added} new, {len(removed)} removed, {len(items)} in {os.path.basename(m3u)}", file=sys.stderr)
    for name in removed[:20]:
        print(f"  removed: {name}", file=sys.stderr)
