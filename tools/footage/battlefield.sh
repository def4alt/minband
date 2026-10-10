#!/usr/bin/env bash
# Fetch and normalise the unlabeled battlefield test clips listed in battlefield-clips.json.
# usage: battlefield.sh <out-dir>     (needs curl, ffmpeg, python3)
# The clips are for local evaluation only: never commit or publish their frames.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
out=${1:?usage: battlefield.sh <out-dir>}
mkdir -p "$out"
python3 - "$here/battlefield-clips.json" <<'PY' | while IFS=$'\t' read -r id url range sha src_ext; do
import json, sys
for c in json.load(open(sys.argv[1]))['clips']:
    r = '-'.join(map(str, c['byte_range'])) if 'byte_range' in c else '-'
    ext = c['url'].rsplit('.', 1)[-1].lower()
    ext = 'mp4' if ext == 'tar' else ext
    print('\t'.join([c['id'], c['url'], r, c['source_sha256'], ext]))
PY
  d="$out/$id"; mkdir -p "$d"; src="$d/source.$src_ext"
  if [ ! -s "$src" ]; then
    if [ "$range" = "-" ]; then curl -fsSL --retry 3 -o "$src" "$url"; else curl -fsSL --retry 3 -r "$range" -o "$src" "$url"; fi
  fi
  got=$(sha256sum "$src" | cut -d' ' -f1)
  [ "$got" = "$sha" ] || { echo "$id: sha256 mismatch ($got), source changed upstream" >&2; continue; }
  case "$id" in
    arma3-surveillance)   # GIF -> H.264
      ffmpeg -nostdin -v error -y -i "$src" -an -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p \
        -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -movflags +faststart "$d/$id.mp4" ;;
    meva-uav-0307-1720)   # 4K -> 1920 wide
      ffmpeg -nostdin -v error -y -i "$src" -map 0:v:0 -an -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
        -vf "scale=1920:-2" -movflags +faststart "$d/$id.mp4" ;;
    *)                    # already H.264: drop audio, remux
      ffmpeg -nostdin -v error -y -i "$src" -map 0:v:0 -c:v copy -an -movflags +faststart "$d/$id.mp4" ;;
  esac
  echo "$id ok -> $d/$id.mp4"
done
