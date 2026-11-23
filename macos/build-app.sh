#!/bin/bash
set -euo pipefail

project_dir=$(cd "$(dirname "$0")/.." && pwd)
out_dir="$project_dir/dist"
mkdir -p "$out_dir"
staging=$(mktemp -d "$out_dir/.build.XXXXXX")
trap 'rm -rf "$staging"' EXIT

app_name="TYT Reader.app"
app="$staging/$app_name"
contents="$app/Contents"
mkdir -p "$contents/MacOS" "$contents/Resources" "$contents/Frameworks"
cp "$project_dir/macos/Info.plist" "$contents/Info.plist"
if [[ -n ${TYT_RELEASE_VERSION:-} ]]; then
    if [[ ! $TYT_RELEASE_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "Expected a numeric TYT release version (X.Y.Z)." >&2
        exit 2
    fi
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $TYT_RELEASE_VERSION" "$contents/Info.plist"
fi
python3 -IB "$project_dir/macos/build-localizations.py" "$contents/Resources"
icon_source="$project_dir/macos/AppIcon.png"
iconset="$staging/AppIcon.iconset"
mkdir -p "$iconset"
for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$icon_source" --out "$iconset/icon_${size}x${size}.png" >/dev/null
    retina_size=$((2 * size))
    sips -z "$retina_size" "$retina_size" "$icon_source" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$contents/Resources/AppIcon.icns"
cp "$project_dir"/{read_settings,write_settings,write_backup,import_settings,import_rdt,import_qdmr,export_qdmr,cps_basic,cps_dtmf,cps_channel,cps_sections,cps_section_editor,cps_csv,cps_csv_record,rdt_editor,rdt_template,normalize_snapshot}.py "$contents/Resources/"
python3 -ESB "$project_dir/cps_choices.py" > "$contents/Resources/CPSChoices.json"
(cd "$project_dir" && python3 -ESB -c 'import json; from cps_dtmf import sequence_section; print(json.dumps(sequence_section(), ensure_ascii=False))') > "$contents/Resources/DTMFBlank.json"
cp "$project_dir"/CPS-{REFERENCES,COVERAGE}.md "$contents/Resources/"
mkdir -p "$contents/Resources/vendor/yaml"
cp "$project_dir/vendor/__init__.py" "$contents/Resources/vendor/"
cp "$project_dir"/vendor/yaml/*.py "$project_dir/vendor/yaml/LICENSE" "$contents/Resources/vendor/yaml/"

usb_prefix=$(pkg-config --variable=prefix libusb-1.0)
usb_lib="$(pkg-config --variable=libdir libusb-1.0)/libusb-1.0.0.dylib"
usb_install_name=$(otool -D "$usb_lib" | tail -n 1)
usb_min_os=$(vtool -show-build "$usb_lib" | awk '$1 == "minos" { print $2; exit }')
min_os=13.0
if [[ "$usb_min_os" =~ ^[0-9]+([.][0-9]+){1,2}$ ]] && (( ${usb_min_os%%.*} >= 13 )); then
    min_os="$usb_min_os"
fi
/usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion $min_os" "$contents/Info.plist"
bundled_usb="$contents/Frameworks/libusb-1.0.0.dylib"
cp -L "$usb_lib" "$bundled_usb"
chmod u+w "$bundled_usb"
if [[ -f "$usb_prefix/COPYING" ]]; then
    cp "$usb_prefix/COPYING" "$contents/Resources/libusb-COPYING.txt"
fi

read -r -a usb_cflags <<< "$(pkg-config --cflags libusb-1.0)"
clang -std=c17 -O2 -Wall -Wextra -Werror -mmacosx-version-min="$min_os" \
    "${usb_cflags[@]}" "$project_dir/read_usb.c" "$usb_lib" \
    -Wl,-headerpad_max_install_names -o "$contents/MacOS/read_usb"
install_name_tool -id '@rpath/libusb-1.0.0.dylib' "$bundled_usb"
install_name_tool -change "$usb_install_name" '@loader_path/../Frameworks/libusb-1.0.0.dylib' \
    "$contents/MacOS/read_usb"

swiftc -swift-version 6 -O -parse-as-library -target "$(uname -m)-apple-macosx$min_os" \
    -module-cache-path "$out_dir/.swift-module-cache" \
    "$project_dir"/macos/*.swift -o "$contents/MacOS/TYTReader"
codesign --force --sign - "$bundled_usb"
codesign --force --sign - "$contents/MacOS/read_usb"
codesign --force --sign - "$app"
codesign --verify --deep --strict "$app"

destination="$out_dir/$app_name"
if [[ -e "$destination" ]]; then
    mv "$destination" "$staging/previous.app"
fi
if ! mv "$app" "$destination"; then
    if [[ -e "$staging/previous.app" ]]; then
        mv "$staging/previous.app" "$destination"
    fi
    exit 1
fi
printf '%s\n' "$destination"
