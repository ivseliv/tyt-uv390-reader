#!/bin/bash
set -euo pipefail

tag="v$VERSION"
test "$GITHUB_REF" = "refs/tags/$tag"
commit=$(git rev-parse 'HEAD^{commit}')
tag_commit() {
    gh api "repos/$GH_REPO/commits/refs/tags/$tag" --jq .sha
}
test "$(tag_commit)" = "$commit"

cd "$1"
files=("TYT-Reader-$VERSION-macOS-arm64.zip" "TYT-Reader-$VERSION-Windows-x64.zip"
       "tyt-reader_${VERSION}-1_amd64.deb" "tyt-reader_${VERSION}-1_arm64.deb" SHA256SUMS)
sha256sum --check SHA256SUMS
marker="<!-- TYT Reader automated release: $commit -->"

if release=$(gh release view "$tag" --json databaseId,isDraft,body,assets 2>lookup-error); then
    if [[ $(jq -r .isDraft <<<"$release") == false ]]; then
        diff -u <(printf '%s\n' "${files[@]}" | sort) <(jq -r '.assets[].name' <<<"$release" | sort)
        echo "The complete release for $tag is already published."
        exit 0
    fi
    # Resume only a draft created by this workflow for this exact source commit.
    jq -e --arg marker "$marker" '.body | contains($marker)' <<<"$release" >/dev/null
else
    if [[ $(<lookup-error) != "release not found" ]]; then
        cat lookup-error >&2
        exit 1
    fi
    cat >notes.md <<EOF
$marker

Сборки TYT Reader $VERSION для macOS на Apple Silicon, Windows x64 и Ubuntu Desktop 24.04 LTS (amd64 и arm64).

Для macOS и Windows распакуйте ZIP целиком. На Ubuntu установите подходящий пакет командой \`sudo apt install ./имя-пакета.deb\`. Инструкции по подключению и работе с программой приведены в [README](https://github.com/$GH_REPO/blob/$tag/README.md).

Контрольные суммы всех сборок находятся в SHA256SUMS.
EOF
    gh release create "$tag" --verify-tag --draft --title "TYT Reader $VERSION" --notes-file notes.md
    release=$(gh release view "$tag" --json databaseId)
fi

gh release upload "$tag" --clobber "${files[@]}"
release_id=$(jq -r .databaseId <<<"$release")
diff -u \
    <(sha256sum -- "${files[@]}" | awk '{print $2 " sha256:" $1}' | sort) \
    <(gh api "repos/$GH_REPO/releases/$release_id/assets" --jq '.[] | select(.state == "uploaded") | [.name,.digest] | join(" ")' | sort)

test "$(tag_commit)" = "$commit"
gh release edit "$tag" --draft=false
