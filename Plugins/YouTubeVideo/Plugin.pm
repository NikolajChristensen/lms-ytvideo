package Plugins::YouTubeVideo::Plugin;

# YouTube Video: browse your YouTube home feed, subscriptions, Watch Later,
# liked videos, playlists and history, and play the audio of videos.
#
# A local Python helper (ytvproxy.py) does the YouTube work through yt-dlp;
# this module starts it, builds the menus from its lists and hands it the
# sign-in. The sign-in is the YouTube Music plugin's, unless one is pasted
# into this plugin's settings.

use strict;
use warnings;
use base qw(Slim::Plugin::OPMLBased);

use File::Spec;
use POSIX qw(WNOHANG);
use Time::HiRes;

use Slim::Utils::Log;
use Slim::Utils::Prefs;
use Slim::Utils::Strings qw(cstring);
use Slim::Utils::Timers;

use Plugins::YouTubeVideo::API;
use Plugins::YouTubeVideo::PlaylistProtocolHandler;
use Plugins::YouTubeVideo::ProtocolHandler;

my $prefs      = preferences('plugin.youtubevideo');
my $musicPrefs = preferences('plugin.youtubemusic');
my $log        = Slim::Utils::Log->addLogCategory({
    category     => 'plugin.youtubevideo',
    defaultLevel => 'WARN',
    description  => 'PLUGIN_YOUTUBEVIDEO',
});

my $helperPid;
my @restarts;          # times of watchdog restarts, to give up on a crash loop
my $sentHeaders;       # sign-in the helper currently has

sub initPlugin {
    my $class = shift;

    $prefs->init({
        proxy_port  => 9877,
        headers     => '',
        show_shorts => 0,
        levelling   => 1,
    });

    Slim::Player::ProtocolHandlers->registerHandler(ytv => 'Plugins::YouTubeVideo::ProtocolHandler');
    Slim::Player::ProtocolHandlers->registerHandler(ytvplaylist => 'Plugins::YouTubeVideo::PlaylistProtocolHandler');

    $class->startHelper;
    Slim::Utils::Timers::setTimer(undef, Time::HiRes::time() + 60, \&_watchdog);
    _whenReady(sub { Plugins::YouTubeVideo::ProtocolHandler::initFormat() });

    $class->SUPER::initPlugin(
        feed   => \&_topLevel,
        tag    => 'youtubevideo',
        menu   => 'radios',
        is_app => 1,
        weight => 11,
    );

    if (main::WEBUI) {
        require Plugins::YouTubeVideo::Settings;
        Plugins::YouTubeVideo::Settings->new;
        _registerSettingsRoutes();
    }
}

sub shutdownPlugin {
    stopHelper();
}

sub getDisplayName { 'PLUGIN_YOUTUBEVIDEO' }
sub playerMenu     { undef }

# ── Helper process ───────────────────────────────────────────────────────────

# The sign-in to use: pasted here, else the YouTube Music plugin's.
sub headers {
    return $prefs->get('headers') || $musicPrefs->get('ytm_cookie') || '';
}

sub headersSource {
    return $prefs->get('headers') ? 'own' : $musicPrefs->get('ytm_cookie') ? 'music' : '';
}

sub startHelper {
    my $class  = shift;
    my $script = File::Spec->catfile($class->_pluginDataFor('basedir'), 'ytvproxy.py');
    my $python = _findPython();
    unless ($python) {
        $log->error('Python 3.10 or newer not found; YouTube Video cannot start its helper');
        return;
    }

    my @args = ($python, $script,
        '--port',     $prefs->get('proxy_port') || 9877,
        '--data-dir', preferences('server')->get('cachedir'),
        ($prefs->get('show_shorts') ? '--show-shorts' : ()),
        ($prefs->get('levelling') ? () : '--no-levelling'),
    );
    $sentHeaders = headers();
    $log->info("Starting helper: @args");

    # The sign-in goes in the environment, which (unlike the command line)
    # other users can't read.
    if (main::ISWINDOWS) {
        local $ENV{YTV_HEADERS} = $sentHeaders;
        $helperPid = system(1, @args) or $log->error("Could not start helper: $!");
        return;
    }

    my $pid = fork();
    if (!defined $pid) {
        $log->error("fork failed: $!");
        return;
    }
    if ($pid == 0) {
        $ENV{YTV_HEADERS} = $sentHeaders;
        exec(@args) or POSIX::_exit(1);   # _exit: never run LMS's cleanup in the child
    }
    $helperPid = $pid;
}

sub stopHelper {
    return unless $helperPid;
    if (main::ISWINDOWS) {
        system("taskkill /F /T /PID $helperPid >nul 2>&1");
    }
    else {
        kill 'TERM', $helperPid;
        for (1 .. 30) {
            last if waitpid($helperPid, WNOHANG) != 0;
            Time::HiRes::sleep(0.1);
        }
        kill 'KILL', $helperPid if kill 0, $helperPid;
        waitpid($helperPid, WNOHANG);
    }
    $helperPid = undef;
}

sub restartHelper {
    my $class = shift;
    stopHelper();
    $class->startHelper;
    _whenReady(sub { Plugins::YouTubeVideo::ProtocolHandler::initFormat() });
}

# Restart the helper if it died - at most 5 times an hour.
sub _watchdog {
    Slim::Utils::Timers::setTimer(undef, Time::HiRes::time() + 60, \&_watchdog);
    return if main::ISWINDOWS || !$helperPid;
    waitpid($helperPid, WNOHANG);
    return if kill 0, $helperPid;

    my $now = time;
    @restarts = grep { $_ > $now - 3600 } @restarts;
    if (@restarts >= 5) {
        $log->error('Helper keeps exiting; not restarting it again for now');
        return;
    }
    push @restarts, $now;
    $log->warn('Helper exited; restarting it');
    __PACKAGE__->startHelper;
}

# Run $cb once the helper answers (it needs a few seconds to start).
sub _whenReady {
    my ($cb, $tries) = @_;
    $tries //= 30;
    Slim::Networking::SimpleAsyncHTTP->new(
        sub { $cb->() },
        sub {
            return unless $tries > 0;
            Slim::Utils::Timers::setTimer(undef, Time::HiRes::time() + 2, sub { _whenReady($cb, $tries - 1) });
        },
        { timeout => 5 },
    )->get(Plugins::YouTubeVideo::API->baseUrl . '/ping');
}

# Give the helper the current sign-in if it changed (e.g. renewed in the
# YouTube Music plugin's settings).
sub syncHeaders {
    my $h = headers();
    return if defined $sentHeaders && $h eq $sentHeaders;
    $sentHeaders = $h;
    Plugins::YouTubeVideo::API->setAuth($h);
}

sub _findPython {
    my @candidates = main::ISWINDOWS
        ? qw(python3 python py)
        : qw(python3.13 python3.12 python3.11 python3.10 python3 /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3);
    for my $py (@candidates) {
        my $path = $py =~ m{[/\\]} ? $py : Slim::Utils::Misc::findbin($py);
        next unless $path && -x $path;
        my $ver = `"$path" -c "import sys; print(sys.version_info[0] * 100 + sys.version_info[1])" 2>&1`;
        return $path if ($ver // '') =~ /^(\d+)/ && $1 >= 310;
    }
    return;
}

# The settings page talks to the helper only through these LMS routes, so
# they sit behind LMS's own web access control.
my %routes = (status => 15, update_ytdlp => 600);

sub _registerSettingsRoutes {
    for my $endpoint (keys %routes) {
        Slim::Web::Pages->addRawFunction("plugins/YouTubeVideo/proxy/$endpoint", sub {
            my ($httpClient, $response) = @_;
            my $send = sub {
                my ($code, $body) = @_;
                $response->code($code);
                $response->header('Content-Type' => 'application/json');
                $response->content($body);
                $httpClient->send_response($response);
                Slim::Web::HTTP::closeHTTPSocket($httpClient);
            };
            Slim::Networking::SimpleAsyncHTTP->new(
                sub { $send->(200, $_[0]->content) },
                sub { $send->(502, '{"error":"helper not running"}') },
                { timeout => $routes{$endpoint} },
            )->get(Plugins::YouTubeVideo::API->baseUrl . "/$endpoint");
        });
    }
}

# ── Menus ────────────────────────────────────────────────────────────────────

sub _topLevel {
    my ($client, $cb) = @_;
    syncHeaders();

    my @items;
    push @items, { name => cstring($client, 'PLUGIN_YOUTUBEVIDEO_NOT_SIGNED_IN'), type => 'text' }
        unless headers();

    push @items, (
        _feedEntry($client, 'PLUGIN_YOUTUBEVIDEO_HOME', 'home'),
        _feedEntry($client, 'PLUGIN_YOUTUBEVIDEO_SUBSCRIPTIONS', 'subscriptions'),
        _playlistEntry(cstring($client, 'PLUGIN_YOUTUBEVIDEO_WATCH_LATER'), 'WL'),
        _playlistEntry(cstring($client, 'PLUGIN_YOUTUBEVIDEO_LIKED'), 'LL'),
        _feedEntry($client, 'PLUGIN_YOUTUBEVIDEO_PLAYLISTS', 'playlists'),
        _feedEntry($client, 'PLUGIN_YOUTUBEVIDEO_HISTORY', 'history'),
    );
    $cb->({ items => \@items });
}

sub _feedEntry {
    my ($client, $string, $feed) = @_;
    return {
        name        => cstring($client, $string),
        type        => 'link',
        url         => \&_feedMenu,
        passthrough => [{ feed => $feed }],
    };
}

# A playlist: opens as a list, and plays/adds as a whole.
sub _playlistEntry {
    my ($name, $id, $item) = @_;
    $item ||= {};
    return {
        name        => $name,
        line2       => join(" \x{b7} ", grep { $_ } $item->{channel}, $item->{count} ? "$item->{count} videos" : ''),
        image       => $item->{thumbnail} || undef,
        type        => 'playlist',
        url         => \&_playlistMenu,
        play        => "ytvplaylist://$id",
        passthrough => [{ id => $id }],
    };
}

sub _feedMenu {
    my ($client, $cb, $args, $params) = @_;
    Plugins::YouTubeVideo::API->feed($params->{feed}, 0, sub {
        $cb->(_listMenu($client, shift));
    });
}

sub _playlistMenu {
    my ($client, $cb, $args, $params) = @_;
    Plugins::YouTubeVideo::API->playlist($params->{id}, 0, sub {
        $cb->(_listMenu($client, shift));
    });
}

sub _listMenu {
    my ($client, $data) = @_;
    if (my $err = $data->{error}) {
        my $text = $err eq 'not_authenticated' ? cstring($client, 'PLUGIN_YOUTUBEVIDEO_NOT_SIGNED_IN')
                 : $err eq 'auth_expired'      ? cstring($client, 'PLUGIN_YOUTUBEVIDEO_AUTH_EXPIRED')
                 : $err eq 'helper_down'       ? cstring($client, 'PLUGIN_YOUTUBEVIDEO_HELPER_DOWN')
                 : cstring($client, 'PLUGIN_YOUTUBEVIDEO_ERROR') . " $err";
        return { items => [{ name => $text, type => 'text' }] };
    }

    my @items;
    for my $item (@{ $data->{items} || [] }) {
        my $type = $item->{type} // '';
        if ($type eq 'video') {
            Plugins::YouTubeVideo::ProtocolHandler->prime($item);
            my $url = "ytv://$item->{id}";
            push @items, {
                name      => $item->{title},
                line2     => join(" \x{b7} ", grep { $_ } $item->{channel}, _duration($item->{duration})),
                image     => $item->{thumbnail},
                type      => 'audio',
                url       => $url,
                play      => $url,
                on_select => 'play',
            };
        }
        elsif ($type eq 'playlist') {
            push @items, _playlistEntry($item->{title}, $item->{id}, $item);
        }
    }
    @items = ({ name => cstring($client, 'PLUGIN_YOUTUBEVIDEO_EMPTY'), type => 'text' }) unless @items;
    return { items => \@items };
}

sub _duration {
    my $s = int(shift || 0) or return '';
    my ($h, $m) = (int($s / 3600), int($s % 3600 / 60));
    return $h ? sprintf('%d:%02d:%02d', $h, $m, $s % 60) : sprintf('%d:%02d', $m, $s % 60);
}

1;
