package Plugins::YouTubeVideo::PlaylistProtocolHandler;

# ytvplaylist://PLAYLIST_ID - never streamed; LMS expands it into the
# playlist's ytv:// videos when a whole playlist (Watch Later, Liked videos,
# your own) is played or added.

use strict;
use warnings;

use Slim::Utils::Log;

use Plugins::YouTubeVideo::API;
use Plugins::YouTubeVideo::ProtocolHandler;

my $log = Slim::Utils::Log->addLogCategory({
    category     => 'plugin.youtubevideo',
    defaultLevel => 'WARN',
    description  => 'PLUGIN_YOUTUBEVIDEO',
});

sub isRemote        { 1 }
sub isPlaylist      { 1 }
sub canDirectStream { 0 }
sub contentType     { 'ytvplaylist' }
sub getFormatForURL { 'ytvplaylist' }

sub explodePlaylist {
    my ($class, $client, $url, $cb) = @_;

    my ($id) = ($url // '') =~ m{^ytvplaylist://([A-Za-z0-9_-]+)};
    return $cb->([]) unless $id;

    Plugins::YouTubeVideo::API->playlist($id, 0, sub {
        my $data = shift;
        if (!$data || $data->{error}) {
            $log->error("Playlist $id: " . ($data ? $data->{error} : 'no reply'));
            return $cb->([]);
        }
        my @urls;
        for my $item (@{ $data->{items} || [] }) {
            next unless ($item->{type} // '') eq 'video';
            Plugins::YouTubeVideo::ProtocolHandler->prime($item);
            push @urls, "ytv://$item->{id}";
        }
        $cb->(\@urls);
    });
}

1;
