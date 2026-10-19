"""
End-to-end check of services/vigenair against the ViGenAiR engine — no AdLens login or credits.

  python -m scripts.vigenair_smoke gs://<GCS_BUCKET>/vv-test/sample.mp4            # full run (~8 min)
  python -m scripts.vigenair_smoke --video-id <folder>                             # reuse an analysed video

Needs VIGENAIR_BUCKET (dev bucket!) and credentials that can sign URLs (guide §3.1).
A full run costs one analysis and one 3-format render on the engine.
"""
import argparse
import json
import sys
import time

import services.vigenair as vg


def wait(fn, done, label, timeout_s=1200, every=15):
    start = time.time()
    while True:
        res = fn()
        if done(res):
            print(f"  {label}: done in {time.time() - start:.0f} s")
            return res
        if time.time() - start > timeout_s:
            sys.exit(f"  {label}: timed out ({res})")
        time.sleep(every)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("source_uri", nargs="?")
    p.add_argument("--video-id")
    p.add_argument("--target", type=float, default=10)
    p.add_argument("--no-render", action="store_true")
    a = p.parse_args()

    if a.video_id:
        video_id = a.video_id
    else:
        if not a.source_uri:
            p.error("give a gs:// source URI or --video-id")
        info = vg.probe_gcs(a.source_uri)
        print("probe:", json.dumps(info))
        vg.check_length(info)
        print(f"credits for analysis: {vg.credits_for(info['duration_s'])}")
        video_id = vg.import_video(a.source_uri, a.source_uri.rsplit("/", 1)[-1], "smoke")["video_id"]
        print("video_id:", video_id)
        st = wait(lambda: vg.get_status(video_id), lambda s: s["status"] in ("ready", "failed"), "analysis")
        if st["status"] == "failed":
            sys.exit(f"analysis failed: {st['error']}")

    segments = vg.get_segments(video_id)
    print(f"{len(segments)} scenes:", ", ".join(f"{s['id']} ({s['duration_s']} s)" for s in segments))

    cuts = vg.generate_variants(video_id, a.target, "", "awareness")
    for c in cuts:
        print(f"  suggestion: {c['title']!r} scenes={c['scenes']} {c['duration_s']} s  AI {c['score']}/{c['max_score']}")

    quote = vg.quote_render(video_id, cuts[:1], ["16:9", "9:16", "1:1"])
    print(f"quote: {len(quote['files'])} files, {quote['credits']} credits")
    if a.no_render:
        return

    render_id = vg.start_render(video_id, cuts[:1], ["16:9", "9:16", "1:1"])
    print("render_id:", render_id)
    res = wait(lambda: vg.get_render(video_id, render_id), lambda r: r["status"] == "done", "render")
    for f in res["files"]:
        print(f"  {f['format']}: {vg.gcs.signed_url(vg.gcs.engine_bucket(), f['blob'])[:100]}…")


if __name__ == "__main__":
    main()
