# YouTube Video for Lyrion Music Server

Browse your own YouTube and listen to the audio of videos on your Squeezebox
players: talks, podcasts, lectures, concerts, video essays.

- **Home**: your personal home feed
- **Subscriptions**: the newest videos from the channels you follow
- **Watch Later**, **Liked videos**, your **Playlists** and **History**

Tap a video to play it, or use Add / Play next to build a queue. Playlists
(including Watch Later) can be played or added as a whole. Seeking works,
including in videos several hours long. Shorts and live streams are left out.

**Volume levelling** (on by default) evens out loudness between videos and
within a video, such as a quiet guest or a loud intro, so you don't have to
keep reaching for the volume. It uses ffmpeg's `dynaudnorm` filter, tuned
for speech: pauses and room noise are left alone, but music loses some of
its dynamics. You can switch it off in the settings. In a test, a loud tech
video and a talk show 6 dB apart came out 0.3 dB apart.

## Disclaimer

An independent, unofficial project, not affiliated with or endorsed by
Google or YouTube. It uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) to
read the same pages the YouTube website shows you, which YouTube may change
at any time. Use it in accordance with YouTube's Terms of Service.

## Requirements

- Lyrion Music Server 9.0 or newer
- Python 3.10 or newer
- ffmpeg (with MP3 support; without it the plugin streams AAC and can't seek)
- Node.js 22 or newer. It is downloaded automatically if none is found; the
  YouTube Music plugin's copy is used when present.

yt-dlp itself is downloaded by the plugin into the LMS cache folder and kept
up to date automatically (checked daily). YouTube changes often break old
versions, so this matters.

On Debian / Raspberry Pi OS: `sudo apt install python3 ffmpeg`

## Install

In Lyrion: **Settings → Plugins → Additional Repositories**, add

```
https://raw.githubusercontent.com/NikolajChristensen/lms-ytvideo/main/repo.xml
```

then enable **YouTube Video** in the plugin list and restart Lyrion.

## Sign-in

Your feeds need your YouTube sign-in. If you use the
[YouTube Music plugin](https://github.com/schmij97/lms-ytmusic) and have
signed in there, YouTube Video uses that sign-in automatically. Nothing to set up.

Otherwise, or to use a different Google account: open youtube.com in your
browser while signed in, press F12 and open the Network tab, type `browse`
in the filter and click Home. Click a POST `browse` request, copy its whole
**Request headers** section and paste it into **Settings → Advanced →
YouTube Video**. Only the cookie and X-Goog-AuthUser are used; they're kept
on your server in a file only the LMS user can read.

If the lists say YouTube no longer accepts the sign-in, the cookie has
expired or been rotated: copy fresh headers the same way (into the YouTube
Music plugin's settings if you share its sign-in).

Tip: YouTube regularly rotates the sign-in cookies of a browser you keep
using, which makes a copied cookie stop working. Copy the headers from a
**private/incognito window** instead, then close that window without
signing out; that sign-in stays valid much longer.

## How it works

The plugin starts a small Python helper (`ytvproxy.py`) that listens on
127.0.0.1 only (port 9877). The helper:

- lists the feeds with yt-dlp's YouTube extractors, and keeps each list for
  5–10 minutes. Lyrion menus find the item you tapped by its position, and
  YouTube reorders the home feed on every request.
- streams a video's audio to Lyrion as 128 kbps constant-bitrate MP3,
  encoded by ffmpeg while Lyrion reads it. There are no temporary files, so
  long videos use no disk space. Because the bitrate is constant, a seek
  position maps straight to a byte offset.
- reads the audio from YouTube's servers in bounded chunks (as yt-dlp does),
  because open-ended requests get throttled to about playback speed. An
  expired or refused (403) link is renewed mid-stream, and a broken
  connection continues from where it stopped.
- runs at most 3 streams and 2 yt-dlp lookups at once, to suit small
  servers (it was developed against a 1-CPU, 1 GB container).
- looks up the next video's audio link in advance, so the next video starts
  quickly.

Logs: `YouTubeVideo/ytvproxy.log` in the LMS cache folder.

## Development

```
tools/test-compile.sh                 # Perl syntax (Slim::* stubbed), Python, XML
tools/release.sh 0.1.1                # bump, build zip, rewrite repo.xml; nothing published
tools/release.sh 0.1.1 --publish      # ...then commit, tag, push and create the GitHub release
```

## Credits

Built on what was learned in schmij97's
[YouTube Music plugin](https://github.com/schmij97/lms-ytmusic): the helper
architecture, the pasted-headers sign-in and many playback fixes.

## License

MIT, see [LICENSE](LICENSE).
