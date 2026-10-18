package StubInc;
# Used by tools/test-compile.sh; see there.
# Auto-stub any module that isn't installed (Slim::*, JSON::XS, URI::Escape...)
# so `perl -c` can check syntax of LMS plugin code outside a server.
my @names = qw(preferences logger uri_escape uri_escape_utf8 decode_json encode_json string cstring);
my $tmpl = <<'PM';
package __PKG__;
sub import {
    my $c = caller;
    no strict 'refs';
    for my $n (qw(__NAMES__)) {
        *{"${c}::$n"} = sub { bless {}, 'StubObj' } unless defined &{"${c}::$n"};
    }
}
our $AUTOLOAD;
sub AUTOLOAD { return if $AUTOLOAD =~ /DESTROY$/; bless {}, 'StubObj' }
1;
PM
unshift @INC, sub {
    my (undef, $file) = @_;
    return if grep { -e "$_/$file" } grep { !ref } @INC;
    (my $pkg = $file) =~ s{/}{::}g; $pkg =~ s/\.pm$//;
    (my $src = $tmpl) =~ s/__PKG__/$pkg/;
    $src =~ s/__NAMES__/@names/;
    open my $fh, '<', \$src; return $fh;
};
package StubObj; our $AUTOLOAD; sub AUTOLOAD { return if $AUTOLOAD =~ /DESTROY$/; $_[0] }
package StubInc;
BEGIN { *main::INFOLOG = sub(){1}; *main::DEBUGLOG = sub(){1}; *main::ISWINDOWS = sub(){0}; *main::SCANNER = sub(){0}; *main::WEBUI = sub(){1}; }
1;
