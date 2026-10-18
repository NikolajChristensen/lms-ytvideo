package Plugins::YouTubeVideo::ProtocolHandler;

# ytv://VIDEO_ID - the audio of a YouTube video.
#
# LMS fetches the audio from the local helper's /stream/VIDEO_ID, which
# transcodes it to constant-bitrate MP3 as LMS reads it (no temp files, so
# hour-long videos are fine). canDirectStream is 0, so players never talk to
# the helper, which only listens on 127.0.0.1.

use strict;
use warnings;
use base qw(Slim::Player::Protocols::HTTP);

use Slim::Player::Playlist;
use Slim::Utils::Log;
use Slim::Utils::Prefs;

use Plugins::YouTubeVideo::API;

my $prefs = preferences('plugin.youtubevideo');
my $log   = Slim::Utils::Log->addLogCategory({
    category     => 'plugin.youtubevideo',
    defaultLevel => 'WARN',
    description  => 'PLUGIN_YOUTUBEVIDEO',
});

sub isRemote        { 1 }
sub isAudio         { 1 }
sub isAudioURL      { 1 }
sub canDirectStream { 0 }
sub songBytes       {}

# Metadata by video ID, filled from the lists the user browses (prime), so
# the queue and Now Playing show title, channel, length and thumbnail
# straight away; otherwise fetched from the helper.
my %_meta;

# 'mp3', or 'aac' when the helper's ffmpeg has no MP3 encoder (piCorePlayer)
my $format = 'mp3';

sub initFormat {
    Plugins::YouTubeVideo::API->codec(sub {
        my $d = shift;
        if ($d && $d->{format}) {
            $format = $d->{format} eq 'adts' ? 'aac' : $d->{format};
            $log->info("Audio format: $format");
        }
    });
}

sub formatOverride  { $format }
sub getFormatForURL { $format }
sub contentType     { $format }

# Seeking: the helper sends constant 128 kbps MP3, so a time is a byte
# offset. LMS asks for it with "Range: bytes=N-" and the helper starts ffmpeg
# at that time. Deliberately not conditional on the duration: Slim::Player::
# Song caches this answer per track, and the duration may arrive a moment
# after playback starts. AAC has no fixed bytes per second, so no seeking.
use constant SEEK_BYTES_PER_SEC => 128_000 / 8;

sub canSeek { $format eq 'mp3' ? 1 : 0 }

sub getSeekData {
    my ($class, $client, $song, $newtime) = @_;
    return {
        sourceStreamOffset => int(SEEK_BYTES_PER_SEC * $newtime),
        timeOffset         => $newtime,
    };
}

sub videoId {
    my ($class, $url) = @_;
    my ($id) = ($url // '') =~ m{^ytv://([A-Za-z0-9_-]{6,})};
    return $id;
}

sub new {
    my ($class, $args) = @_;
    my $song      = $args->{song};
    my $streamUrl = $song ? $song->streamUrl : undef;
    unless ($streamUrl) {
        $log->error('No stream URL for ' . ($args->{url} // '?'));
        return undef;
    }
    return $class->SUPER::new({
        url    => $streamUrl,
        song   => $song,
        client => $args->{client},
    });
}

sub getNextTrack {
    my ($class, $song, $successCb, $errorCb) = @_;

    my $id = $class->videoId($song->currentTrack->url);
    return $errorCb->('Invalid YouTube video URL') unless $id;

    $song->streamUrl(Plugins::YouTubeVideo::API->baseUrl . "/stream/$id");

    # Resolve this video's audio URL now, while LMS sets up the stream, and
    # the next one's for a quick track change.
    Plugins::YouTubeVideo::API->prefetch($id);
    if (my $next = _nextVideoId($song)) {
        Plugins::YouTubeVideo::API->prefetch($next);
    }

    # The length drives the progress bar and seeking; set it now when the
    # lists already told us, else once the helper has looked the video up.
    if (my $secs = $_meta{$id} && $_meta{$id}{duration}) {
        _setDuration($song, $secs);
        # Some screens show the track's own title (current_title), not
        # getMetadataFor's - without this they'd show the ytv:// URL.
        $song->currentTrack->title($_meta{$id}{title}) if $_meta{$id}{title};
    }
    else {
        _fetchMetadata($id, $song);
    }
    $successCb->();
}

sub _nextVideoId {
    my $song   = shift;
    my $client = eval { $song->master } or return;
    my $index  = eval { $song->index };
    return unless defined $index;
    my $track = eval { Slim::Player::Playlist::track($client, $index + 1) } or return;
    return __PACKAGE__->videoId(eval { $track->url });
}

# ── Metadata ─────────────────────────────────────────────────────────────────


sub prime {
    my ($class, $item) = @_;
    return unless $item && $item->{id};
    my $have = $_meta{$item->{id}} || {};
    $_meta{$item->{id}} = {
        title    => $item->{title}     || $have->{title},
        channel  => $item->{channel}   || $have->{channel},
        duration => $item->{duration}  || $have->{duration},
        cover    => $item->{thumbnail} || $have->{cover},
    };
}

sub _fetchMetadata {
    my ($id, $song) = @_;
    Plugins::YouTubeVideo::API->video($id, sub {
        my $info = shift;
        return unless $info && !$info->{error};
        __PACKAGE__->prime({ %$info, id => $id });
        my $m = $_meta{$id};
        if ($song && (my $track = eval { $song->currentTrack })) {
            $track->title($m->{title}) if $m->{title};
        }
        _setDuration($song, $m->{duration}) if $m->{duration};
        Slim::Control::Request::notifyFromArray(undef, ['newmetadata']);
    });
}

sub _setDuration {
    my ($song, $secs) = @_;
    $song->duration($secs);
    if (my $track = eval { $song->currentTrack }) {
        $track->secs($secs);
    }
}

my %_fetching;

sub getMetadataFor {
    my ($class, $client, $url) = @_;
    my $id = $class->videoId($url) or return {};
    my $m  = $_meta{$id};

    if (!$m && !$_fetching{$id}++) {
        Plugins::YouTubeVideo::API->video($id, sub {
            my $info = shift;
            delete $_fetching{$id};
            return unless $info && !$info->{error};
            $class->prime({ %$info, id => $id });
            Slim::Control::Request::notifyFromArray(undef, ['newmetadata']);
        });
    }
    $m ||= {};

    return {
        title    => $m->{title} || "YouTube $id",
        artist   => $m->{channel} || '',
        album    => 'YouTube',
        duration => $m->{duration} || undef,
        cover    => $m->{cover} || "https://i.ytimg.com/vi/$id/hqdefault.jpg",
        icon     => $class->getIcon,
        type     => 'YouTube',
        bitrate  => $format eq 'mp3' ? '128k CBR' : '128k',
    };
}

sub getIcon {
    return Plugins::YouTubeVideo::Plugin->_pluginDataFor('icon');
}

1;
