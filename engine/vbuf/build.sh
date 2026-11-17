#!/usr/bin/env bash
# Builds libnvdaVbuf.so from upstream's storage.cpp and utils.cpp, unchanged, plus vbufLinux.cpp.
# compat/ comes first on the include path, so its log.h stands in for upstream's; xml.h is upstream's.
# Run inside the dev shell: nix develop ROOT -c bash ROOT/engine/vbuf/build.sh ROOT/nvdaHelper OUT
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
helper=$1
out=$2
mkdir -p "$(dirname "$out")"
g++ -std=c++20 -O2 -DNDEBUG -fPIC -shared -Wall -Wno-sign-compare -Wno-unused-variable -Wno-unused-but-set-variable \
	-I"$here/compat" -I"$helper" -I"$helper/vbufBase" \
	"$helper/vbufBase/storage.cpp" "$helper/vbufBase/utils.cpp" "$here/vbufLinux.cpp" \
	-o "$out"
