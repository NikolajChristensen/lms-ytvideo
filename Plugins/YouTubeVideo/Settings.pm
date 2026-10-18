package Plugins::YouTubeVideo::Settings;

use strict;
use warnings;
use base qw(Slim::Web::Settings);

use Slim::Utils::Prefs;

use Plugins::YouTubeVideo::API;

my $prefs = preferences('plugin.youtubevideo');

sub name { 'PLUGIN_YOUTUBEVIDEO' }
sub page { 'plugins/YouTubeVideo/settings/basic.html' }

sub handler {
    my ($class, $client, $params) = @_;

    if ($params->{saveSettings}) {
        my $headers = $params->{pref_headers} // '';
        $headers =~ s/^\s+|\s+$//g;
        $prefs->set('headers', $headers);

        my $shorts = $params->{pref_show_shorts} ? 1 : 0;
        if ($shorts != ($prefs->get('show_shorts') ? 1 : 0)) {
            $prefs->set('show_shorts', $shorts);
            Plugins::YouTubeVideo::API->setOptions({ show_shorts => $shorts });
        }

        my $port = int($params->{pref_proxy_port} || 9877);
        $port = 9877 unless $port >= 1024 && $port <= 65535;
        if ($port != ($prefs->get('proxy_port') || 9877)) {
            $prefs->set('proxy_port', $port);
            Plugins::YouTubeVideo::Plugin->restartHelper;
        }
        else {
            Plugins::YouTubeVideo::Plugin::syncHeaders();
        }
    }

    $params->{prefs}{headers}     = $prefs->get('headers');
    $params->{prefs}{show_shorts} = $prefs->get('show_shorts');
    $params->{prefs}{proxy_port}  = $prefs->get('proxy_port') || 9877;
    $params->{headers_source}     = Plugins::YouTubeVideo::Plugin::headersSource();

    return $class->SUPER::handler($client, $params);
}

1;
