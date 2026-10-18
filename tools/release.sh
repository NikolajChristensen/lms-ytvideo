#!/usr/bin/env bash
# Cut a release: bump the version, rebuild the zip, and rewrite repo.xml so
# its version, <url> and <sha> all agree with the artefact (LMS downloads the
# zip named in <url> and refuses it unless its SHA-1 matches <sha>).
#
#   tools/release.sh 0.1.0            # test + bump + build + rewrite, nothing published
#   tools/release.sh 0.1.0 --publish  # ...then commit, tag and create the GitHub release
#
# Edit <changes> in repo.xml by hand before releasing.
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="${1:-}"
PUBLISH="${2:-}"
REPO_SLUG="NikolajChristensen/lms-ytvideo"
PLUGIN_DIR="Plugins/YouTubeVideo"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
	echo "usage: tools/release.sh <version> [--publish]   e.g. 0.1.0" >&2
	exit 2
fi

ZIP="dist/YouTubeVideo-${VERSION}.zip"
URL="https://github.com/${REPO_SLUG}/releases/download/v${VERSION}/YouTubeVideo-${VERSION}.zip"

echo "==> tests"
./tools/test-compile.sh

echo "==> version -> $VERSION"
perl -i -pe "s{<version>[^<]*</version>}{<version>${VERSION}</version>}" "$PLUGIN_DIR/install.xml"

echo "==> build $ZIP"
mkdir -p dist
rm -f dist/YouTubeVideo-*.zip
rm -rf "$PLUGIN_DIR/__pycache__"
# Layout: Plugins/YouTubeVideo/... at the root.
#
# Reproducible: the same tree always gives the same zip and SHA-1, so a dry
# run and --publish agree. Every entry's mtime is pinned to the last commit's
# time (in UTC, since zip stores local time), entries go in in sorted order,
# and -X drops uid/gid and platform extra fields.
# The time comes from the last commit touching the plugin's code, not counting
# install.xml, so committing the version bump doesn't change the zip.
EPOCH=$(git log -1 --format=%ct -- "$PLUGIN_DIR" ":(exclude)$PLUGIN_DIR/install.xml")
FILES=$(find "$PLUGIN_DIR" \( -name __pycache__ -o -name '*.pyc' -o -name '*.orig' -o -name '*.swp' \
	-o -name '*~' -o -name .DS_Store \) -prune -o -print | LC_ALL=C sort)
# shellcheck disable=SC2086
touch -h -d "@$EPOCH" $FILES
# shellcheck disable=SC2086
printf '%s\n' $FILES | TZ=UTC zip -qX -@ "$ZIP"
unzip -l "$ZIP"

SHA=$(sha1sum "$ZIP" | cut -d' ' -f1)
echo "==> sha1 $SHA"

echo "==> repo.xml"
perl -i -pe "
	s{<plugin name=\"YouTubeVideo\" version=\"[^\"]*\"}{<plugin name=\"YouTubeVideo\" version=\"${VERSION}\"};
	s{<url>[^<]*</url>}{<url>${URL}</url>};
	s{<sha>[^<]*</sha>}{<sha>${SHA}</sha>};
" repo.xml

# Fail loudly rather than ship a repo.xml that disagrees with the zip.
grep -q "<sha>${SHA}</sha>" repo.xml                         || { echo "repo.xml sha not updated" >&2; exit 1; }
[[ ${#SHA} -eq 40 ]]                                         || { echo "sha is not a SHA-1" >&2; exit 1; }
grep -q "version=\"${VERSION}\"" repo.xml                    || { echo "repo.xml version not updated" >&2; exit 1; }
grep -q "<url>${URL}</url>" repo.xml                         || { echo "repo.xml url not updated" >&2; exit 1; }
grep -q "<version>${VERSION}</version>" "$PLUGIN_DIR/install.xml" || { echo "install.xml not updated" >&2; exit 1; }
./tools/test-compile.sh >/dev/null                           || { echo "post-bump checks failed" >&2; exit 1; }

echo
echo "ready: $ZIP  ($SHA)"

if [[ "$PUBLISH" != "--publish" ]]; then
	echo "re-run with --publish to commit, tag v${VERSION} and create the GitHub release."
	exit 0
fi

echo "==> publishing v${VERSION}"
git add -A "$PLUGIN_DIR/install.xml" repo.xml
if git diff --cached --quiet; then
	echo "    (already committed, nothing to add)"
else
	git commit -m "Release ${VERSION}"
fi
git tag "v${VERSION}"
git push origin HEAD "v${VERSION}"
gh release create "v${VERSION}" "$ZIP" --repo "$REPO_SLUG" --title "YouTube Video ${VERSION}" --generate-notes
echo "done: https://github.com/${REPO_SLUG}/releases/tag/v${VERSION}"
