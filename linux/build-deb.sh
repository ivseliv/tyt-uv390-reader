#!/usr/bin/env bash
set -euo pipefail
umask 022

if [[ $(uname -s) != Linux || $# -gt 1 ]]; then
    echo "Build on Ubuntu with: bash linux/build-deb.sh [output-directory]" >&2
    exit 2
fi
project=$(cd -- "$(dirname -- "$0")/.." && pwd)
destination=${1:-"$project/dist/ubuntu"}
mkdir -p -- "$destination"
destination=$(cd -- "$destination" && pwd)
work=$(mktemp -d -- "$destination/.tyt-build.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
cd -- "$work"
cp -r -- "$project/linux/debian" debian

package=tyt-reader
root="$work/debian/$package"
app="$root/usr/lib/$package"
install -d -m 0755 "$root/DEBIAN" "$app/vendor/yaml" "$app/macos/i18n" "$app/windows"

# Keep the runtime manifest explicit. A working directory can also contain local codeplugs.
modules=(
    cps_basic cps_channel cps_choices cps_csv cps_csv_record cps_dtmf cps_section_editor cps_sections
    export_qdmr import_qdmr import_rdt import_settings normalize_snapshot rdt_editor rdt_template
    read_settings win_cps_actions win_document win_language win_members win_program win_records
    win_services windows_app write_backup write_settings
)
for module in "${modules[@]}"; do
    install -m 0644 "$project/$module.py" "$app/"
done
yaml_modules=(__init__ composer constructor cyaml dumper emitter error events loader nodes parser reader representer resolver scanner serializer tokens)
for module in "${yaml_modules[@]}"; do
    install -m 0644 "$project/vendor/yaml/$module.py" "$app/vendor/yaml/"
done
install -m 0644 "$project/vendor/__init__.py" "$app/vendor/"
install -Dm 0644 "$project/vendor/yaml/LICENSE" "$root/usr/share/doc/$package/PyYAML-LICENSE"
install -m 0644 "$project"/macos/i18n/*.tsv "$app/macos/i18n/"
install -m 0644 "$project"/windows/*.tsv "$app/windows/"
install -Dm 0644 "$project/macos/AppIcon.png" "$root/usr/share/pixmaps/$package.png"
ln -s "../../../share/pixmaps/$package.png" "$app/macos/AppIcon.png"
install -Dm 0755 "$project/linux/tyt-reader" "$root/usr/bin/$package"
install -Dm 0644 "$project/linux/tyt-reader.desktop" "$root/usr/share/applications/$package.desktop"
install -Dm 0644 "$project/linux/70-tyt-reader.rules" "$root/usr/lib/udev/rules.d/70-tyt-reader.rules"

read -r -a usb_cflags <<< "$(pkg-config --cflags libusb-1.0)"
read -r -a usb_libs <<< "$(pkg-config --libs libusb-1.0)"
cc -std=c17 -D_POSIX_C_SOURCE=200809L -O2 -Wall -Wextra -Werror \
    "${usb_cflags[@]}" "$project/read_usb.c" -o "$app/read_usb" "${usb_libs[@]}"
strip --strip-unneeded "$app/read_usb"
dpkg-shlibdeps -T"debian/$package.substvars" "$app/read_usb"
dpkg-gencontrol -p"$package" -P"$root" -T"debian/$package.substvars"
dpkg-deb --root-owner-group --build "$root" "$work/"
version=$(dpkg-parsechangelog -S Version)
arch=$(dpkg --print-architecture)
filename="${package}_${version}_${arch}.deb"
mv -- "$work/$filename" "$destination/$filename"
echo "$destination/$filename"
