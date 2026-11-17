#!/usr/bin/env bash
# Builds libnvdaVbuf.so from upstream's storage.cpp and utils.cpp, unchanged, plus vbufLinux.cpp.
# compat/ comes first on the include path, so its log.h stands in for upstream's; xml.h is upstream's.
# Run inside the dev shell: nix develop ROOT -c bash ROOT/engine/vbuf/build.sh ROOT/nvdaHelper OUT [asan]
# asan builds with AddressSanitizer, UndefinedBehaviorSanitizer and storage.cpp's assertions; a process
# loading it needs libasan preloaded, from g++ -print-file-name=libasan.so.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
helper=$1
out=$2
mode=${3:-release}
case $mode in
	release) flags=(-O2 -DNDEBUG) ;;
	asan) flags=(-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined -fno-sanitize-recover=undefined) ;;
	*)
		echo "build.sh: unknown mode $mode, expected release or asan" >&2
		exit 2
		;;
esac
mkdir -p "$(dirname "$out")"
g++ -std=c++20 "${flags[@]}" -fPIC -shared -Wall -Wno-sign-compare -Wno-unused-variable -Wno-unused-but-set-variable -Wno-delete-non-virtual-dtor \
	-I"$here/compat" -I"$helper" -I"$helper/vbufBase" \
	"$helper/vbufBase/storage.cpp" "$helper/vbufBase/utils.cpp" "$here/vbufLinux.cpp" \
	-o "$out"
