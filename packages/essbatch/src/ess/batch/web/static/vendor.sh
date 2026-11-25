#!/bin/sh
# Fill vendor/ with the JavaScript libraries the page loads, from the npm
# registry, at pinned versions. Run once after a checkout: sh vendor.sh
set -eu
cd "$(dirname "$0")"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
(cd "$tmp" && npm pack --silent tabulator-tables@6.3.1 plotly.js-basic-dist-min@2.35.2 >/dev/null)
mkdir -p vendor
tar -xzf "$tmp"/tabulator-tables-6.3.1.tgz -C "$tmp" package/dist/js/tabulator_esm.min.js package/dist/css/tabulator.min.css
mv "$tmp"/package/dist/js/tabulator_esm.min.js "$tmp"/package/dist/css/tabulator.min.css vendor/
rm -rf "$tmp"/package
tar -xzf "$tmp"/plotly.js-basic-dist-min-2.35.2.tgz -C "$tmp" package/plotly-basic.min.js
mv "$tmp"/package/plotly-basic.min.js vendor/
