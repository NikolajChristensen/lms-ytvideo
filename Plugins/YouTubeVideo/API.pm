package Plugins::YouTubeVideo::API;

# Asynchronous calls to the local helper (ytvproxy.py).

use strict;
use warnings;

use JSON::XS::VersionOneAndTwo;
use Slim::Networking::SimpleAsyncHTTP;
use Slim::Utils::Log;
use Slim::Utils::Prefs;

my $prefs = preferences('plugin.youtubevideo');
my $log   = Slim::Utils::Log->addLogCategory({
    category     => 'plugin.youtubevideo',
    defaultLevel => 'WARN',
    description  => 'PLUGIN_YOUTUBEVIDEO',
});

sub baseUrl {
    my $port = $prefs->get('proxy_port') || 9877;
    return "http://127.0.0.1:$port";
}

sub _get {
    my ($path, $cb, $timeout) = @_;
    my $url = baseUrl() . $path;
    main::DEBUGLOG && $log->is_debug && $log->debug("GET $url");

    Slim::Networking::SimpleAsyncHTTP->new(
        sub {
            my $http = shift;
            my $data = eval { decode_json($http->content) };
            if ($@ || ref $data ne 'HASH') {
                $log->error("Bad reply from helper for $path: " . ($@ || 'not an object'));
                return $cb->({ error => 'bad reply from helper' });
            }
            $cb->($data);
        },
        sub {
            my ($http, $err) = @_;
            # An error status still carries the helper's JSON explanation
            my $data;
            $data = eval { decode_json($http->content) } if $http && $http->content;
            $log->warn("Helper request $path failed: $err");
            $cb->(ref $data eq 'HASH' && $data->{error} ? $data : { error => 'helper_down' });
        },
        { timeout => $timeout || 90 },
    )->get($url);
}

sub _post {
    my ($path, $body, $cb) = @_;
    Slim::Networking::SimpleAsyncHTTP->new(
        sub { my $d = eval { decode_json($_[0]->content) }; $cb->($d || {}) if $cb },
        sub { $log->warn("Helper POST $path failed: $_[1]"); $cb->({ error => 'helper_down' }) if $cb },
        { timeout => 30 },
    )->post(baseUrl() . $path, 'Content-Type' => 'text/plain; charset=utf-8', $body);
}

# Feeds: home, subscriptions, history, playlists
sub feed {
    my ($class, $name, $refresh, $cb) = @_;
    _get("/feed/$name" . ($refresh ? '?refresh=1' : ''), $cb);
}

# Any playlist, including WL (Watch Later) and LL (Liked videos)
sub playlist {
    my ($class, $id, $refresh, $cb) = @_;
    _get("/playlist/$id" . ($refresh ? '?refresh=1' : ''), $cb);
}

sub video {
    my ($class, $id, $cb) = @_;
    _get("/video/$id", $cb);
}

sub prefetch {
    my ($class, $id) = @_;
    _get("/prefetch/$id", sub {}, 15);
}

sub codec {
    my ($class, $cb) = @_;
    _get('/codec', $cb, 10);
}

sub setAuth {
    my ($class, $headers, $cb) = @_;
    _post('/auth', $headers // '', $cb);
}

sub setOptions {
    my ($class, $opts, $cb) = @_;
    _post('/options', encode_json($opts), $cb);
}

1;
