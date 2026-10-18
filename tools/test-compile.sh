#!/usr/bin/env bash
# Compile check for the YouTubeVideo plugin without a running Lyrion server.
#
# The Perl modules `use` Slim::* classes (plus JSON::XS / URI::Escape) that only
# exist inside LMS. tools/StubInc.pm stubs whatever isn't installed, so
# `perl -c` can check syntax and `use` lines. It does NOT check behaviour
# against the real LMS API. The Python proxy is byte-compiled.
#
# Usage: tools/test-compile.sh
set -euo pipefail

cd "$(dirname "$0")/.."

rc=0
for f in Plugins/YouTubeVideo/*.pm; do
	printf '%-48s ' "$f"
	if out=$(perl -Itools -I. -MStubInc -c "$f" 2>&1); then
		echo ok
	else
		echo FAIL; echo "$out" | grep -v -i 'prototype mismatch' | tail -5; rc=1
	fi
done

printf '%-48s ' Plugins/YouTubeVideo/ytvproxy.py
if python3 -W ignore -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' \
	Plugins/YouTubeVideo/ytvproxy.py; then
	echo ok
else
	echo FAIL; rc=1
fi

printf '%-48s ' "install.xml / repo.xml"
if python3 -c 'import sys,xml.dom.minidom as m; [m.parse(f) for f in sys.argv[1:]]' \
	Plugins/YouTubeVideo/install.xml $(ls repo.xml 2>/dev/null) && head -1 Plugins/YouTubeVideo/install.xml | grep -q 'version="1.0"'; then
	echo ok
else
	echo FAIL; rc=1
fi

exit $rc
