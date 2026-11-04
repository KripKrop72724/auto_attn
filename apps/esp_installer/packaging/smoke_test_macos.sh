#!/bin/bash
# Opens a built disk image the way a person does, checks the app inside it and
# a copy of it (the drag to Applications), then closes the image. The macOS
# twin of smoke_test.ps1; run it on an Apple Silicon Mac.
#
#   packaging/smoke_test_macos.sh dist/StateLife-Device-Installer-1.1.0-macOS-arm64-TEST.dmg smoke
#
# Checks: the image holds the app and the Applications link; the app's ad hoc
# signature, Info.plist and Apple Silicon code; the bundled esptool and
# espefuse; --self-test and --screenshots without a screen
# (QT_QPA_PLATFORM=offscreen, unless already set); the copied app still verifies
# and runs. The app keeps its data in a scratch folder (SL_INSTALLER_DATA_DIR),
# never in the Mac's own Library.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <disk image> <output folder>" >&2
    exit 2
fi
[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || {
    echo "Run the macOS smoke test on an Apple Silicon Mac." >&2
    exit 2
}

app_name="State Life Device Installer"
bundle_id="com.statelife.deviceinstaller"
dmg=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
mkdir -p "$2"
out=$(cd "$2" && pwd)
work=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/sl-installer-smoke.XXXXXX")
mount_point="$work/volume"
# A folder with spaces, like the real /Applications copy, so quoting mistakes show up here.
copy_dir="$work/smoke install"
export SL_INSTALLER_DATA_DIR="$work/data"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
mounted=0

fail() {
    echo "SMOKE TEST FAILED: $*" >&2
    exit 1
}

cleanup() {
    # Spotlight can hold the volume for a moment.
    if [ "$mounted" = 1 ] && ! hdiutil detach "$mount_point" -quiet; then
        sleep 3
        hdiutil detach "$mount_point" -force -quiet || true
    fi
    rm -rf "$work"
}
trap cleanup EXIT

# run NAME SECONDS COMMAND...: output goes to NAME.out.txt and NAME.err.txt;
# prints the exit code. perl's alarm stands in for timeout(1), which macOS lacks.
run() {
    local name=$1 seconds=$2 code=0
    shift 2
    perl -e 'alarm shift; exec @ARGV or die "cannot start $ARGV[0]: $!\n"' "$seconds" "$@" \
        >"$out/$name.out.txt" 2>"$out/$name.err.txt" || code=$?
    echo "$name exited with code $code" >&2
    echo "$code"
}

check_app() {
    local app=$1 exe="$1/Contents/MacOS/$app_name"
    [ -x "$exe" ] || fail "$exe is missing"
    codesign --verify --deep --strict "$app" || fail "the signature of $app does not verify"
    [ "$(lipo -archs "$exe")" = arm64 ] || fail "the app is not for Apple Silicon only"
}

plist_value() {
    /usr/libexec/PlistBuddy -c "Print :$1" "$app/Contents/Info.plist"
}

# 1. Open the image read-only, as Finder does, without showing it.
mkdir -p "$mount_point"
hdiutil attach "$dmg" -readonly -nobrowse -noautoopen -mountpoint "$mount_point" -quiet
mounted=1
app="$mount_point/$app_name.app"
items=$(cd "$mount_point" && ls -1 | sort | tr '\n' '|')
[ "$items" = "Applications|$app_name.app|" ] || fail "the image holds: $items"
[ "$(readlink "$mount_point/Applications")" = /Applications ] || fail "Applications is not a link to /Applications"

# 2. The app inside it: signature, Apple Silicon, Info.plist.
check_app "$app"
version=$(plist_value CFBundleShortVersionString)
[ "$(plist_value CFBundleIdentifier)" = "$bundle_id" ] || fail "the bundle ID is not $bundle_id"
[ "$(plist_value CFBundleVersion)" = "$version" ] || fail "CFBundleVersion is not $version"
[ -n "$(plist_value LSMinimumSystemVersion)" ] || fail "LSMinimumSystemVersion is missing"
case "$(basename "$dmg")" in
    "StateLife-Device-Installer-$version-macOS-arm64.dmg" | \
    "StateLife-Device-Installer-$version-macOS-arm64-TEST.dmg") ;;
    *) fail "the image's name does not carry the app's version $version" ;;
esac
for document in EULA.txt THIRD-PARTY-NOTICES.txt "licenses/esptool-5.1.0.tar.gz"; do
    [ -f "$app/Contents/Resources/$document" ] || fail "the app has no $document"
done
exe="$app/Contents/MacOS/$app_name"

# 3. The bundled tools run from the app (no board needed).
[ "$(run tool-esptool-version 120 "$exe" --tool esptool version)" = 0 ] &&
    grep -q '5\.1\.0' "$out/tool-esptool-version.out.txt" ||
    fail "the bundled esptool did not report version 5.1.0"
[ "$(run tool-espefuse-virtual 120 "$exe" --tool espefuse --chip esp32s3 --virt summary --format json)" = 0 ] ||
    fail "the bundled espefuse could not read a virtual ESP32-S3"

# 4. Self-test: engine checks plus opening and closing the window.
if [ "$(run self-test 300 "$exe" --self-test)" != 0 ] ||
    ! grep -q 'Self-test passed' "$out/self-test.out.txt"; then
    cat "$out/self-test.out.txt" "$out/self-test.err.txt" >&2
    fail "the app's self-test failed"
fi

# 5. A picture of every screen.
[ "$(run screenshots 300 "$exe" --screenshots "$out/screenshots")" = 0 ] ||
    fail "the app did not save its screenshots"
count=$(find "$out/screenshots" -name '*.png' | wc -l | tr -d ' ')
[ "$count" -gt 0 ] || fail "the app saved no screenshots"
echo "Saved $count screenshots." >&2

# 6. The drag to Applications: a copy keeps its links and signature, and runs.
mkdir -p "$copy_dir"
ditto "$app" "$copy_dir/$app_name.app"
check_app "$copy_dir/$app_name.app"
[ "$(run copy-esptool-version 120 "$copy_dir/$app_name.app/Contents/MacOS/$app_name" --tool esptool version)" = 0 ] ||
    fail "the copied app could not run its esptool"

echo "Smoke test passed: image, signature, tools, self-test, screenshots and copy all work ($version)." >&2
