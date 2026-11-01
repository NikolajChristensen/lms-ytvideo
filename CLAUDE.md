# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Lyrion Music Server (LMS, formerly Logitech Media Server) plugin, in Perl plus a
Python helper, that lets you browse your own YouTube (home feed, subscriptions,
Watch Later, liked videos, playlists, history) and play the **audio** of videos on
Squeezebox players via `yt-dlp` + `ffmpeg`. There is no build step for the Perl side;
the plugin runs as-is inside LMS's Perl process and spawns the Python helper as a
subprocess.

## Commands

```
tools/test-compile.sh   # Perl syntax (Slim::* stubbed via tools/StubInc.pm), Python
                         # byte-compile, install.xml/repo.xml well-formedness
```

There's no behavioral/unit test suite beyond the compile check — it catches syntax
and `use`/import errors, not runtime behavior against the real LMS API or YouTube.
To exercise it for real, load the plugin into a real LMS instance and watch
`YouTubeVideo/ytvproxy.log` in the LMS cache folder.

### Releasing

```
tools/release.sh 0.2.1            # run tests, bump version, rebuild dist/*.zip, rewrite repo.xml
tools/release.sh 0.2.1 --publish  # ...then git commit, tag v0.2.1, push, and create the GitHub release
```

Same constraint as the other Lyrion plugins in this family: `install.xml`'s
`<version>` and `repo.xml`'s `<version>`/`<url>`/`<sha>` must all move together — LMS
downloads the zip named in `repo.xml`'s `<url>` and refuses it if the SHA-1 doesn't
match `<sha>`. `release.sh` rewrites all of them from one version argument and fails
loudly (via `grep` checks at the end) if any didn't take; don't hand-edit these
fields separately. Edit `<changes>` in `repo.xml` by hand before releasing — it isn't
generated. The zip build is reproducible (entry mtimes pinned to the last commit
touching plugin code, sorted order, no uid/gid), so a dry run and `--publish` always
produce the same SHA.

## Architecture

```
Plugin.pm (initPlugin)
  - starts ytvproxy.py as a child process (watchdog restarts it on crash, capped)
  - hands it the sign-in (this plugin's own pasted headers, or falls back to the
    YouTube Music plugin's prefs — `plugin.youtubemusic` — if none are set here)
  - builds the OPML menus (Home, Subscriptions, Watch Later, Liked, Playlists,
    History) from the helper's JSON lists

API.pm
  - thin async HTTP client (Slim::Networking::SimpleAsyncHTTP) to the helper's
    localhost port (default 9877, `baseUrl()`); every Perl->helper call goes
    through here

ytv://VIDEO_ID                          ytvplaylist://PLAYLIST_ID
  ProtocolHandler.pm                      PlaylistProtocolHandler.pm
  canDirectStream=0: LMS streams the      never streamed directly; LMS calls
  audio itself from the helper's          explodePlaylist to expand it into
  /stream/<id> (ffmpeg transcodes to      the playlist's ytv:// entries
  CBR MP3 on the fly, no temp files,
  so hour-long videos cost no disk)

Settings.pm
  - settings page: pasted sign-in headers, show-shorts toggle, volume-levelling
    toggle; pushes changes to the helper via API.pm->setOptions

ytvproxy.py (127.0.0.1:9877 only — never reachable from players, since
canDirectStream is 0)
  - lists feeds via yt-dlp's YouTube extractors, cached 5-10 min (YouTube
    reorders the home feed on every request; Lyrion menus find the tapped item
    by position, so the list must stay stable within a browse session)
  - streams audio as 128 kbps CBR MP3 (or AAC where ffmpeg has no MP3 encoder,
    e.g. piCorePlayer) — constant bitrate means a seek position maps straight
    to a byte offset
  - reads from YouTube's CDN in bounded chunks, like yt-dlp does, since
    open-ended requests get throttled to roughly playback speed; renews an
    expired/403 link mid-stream and resumes a broken connection in place
  - applies volume levelling (ffmpeg's `dynaudnorm`, tuned for speech) unless
    switched off in settings
  - caps concurrency (3 streams, 2 yt-dlp lookups) for small servers; prefetches
    the next queued video's link ahead of time
```

- Sign-in is pasted browser request headers (cookie + `X-Goog-AuthUser`), not
  OAuth — see the README's "Sign-in" section for how to capture them and why a
  private/incognito window's cookies last longer. This plugin can share the
  **YouTube Music** plugin's sign-in automatically (reads `plugin.youtubemusic`
  prefs) so a user who already signed in there needs no extra setup.
- Shorts and live streams are deliberately excluded from all feeds — not a bug,
  a scope decision.
- `yt-dlp` itself is downloaded into the LMS cache folder and auto-updated daily;
  YouTube's page format changes frequently enough that this matters for
  correctness, not just features.
- This plugin's sibling Android app, `android-apps/TubeCast/tube-audio`, treats
  `ytvproxy.py` as the reference implementation for YouTube sign-in parsing and
  CDN streaming behavior — a fix made here to sign-in handling or CDN chunking is
  worth checking against that app too.
- The helper architecture, pasted-headers sign-in, and several playback fixes
  were built on what was learned from schmij97's YouTube Music plugin (credited
  in the README).

See the README's "How it works", "Sign-in" and "Credits" sections for the full
user-facing rationale before touching sign-in handling or streaming behavior.
