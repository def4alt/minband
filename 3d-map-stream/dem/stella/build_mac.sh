#!/usr/bin/env bash
# Build stella_vslam (the maintained OpenVSLAM fork) headless on macOS arm64 into ~/.local/stella, reusing Eigen 3.4 and
# OpenCV 4.11 from ~/.local/orbslam3 (see ../orbslam3). Clones into $SRC (default ~/src/github.com) on first run.
# Pins: stella_vslam e445b54, stella_vslam_examples defc69e, g2o 20230223_git (stella's Dockerfile), yaml-cpp 0.8.0,
# SuiteSparse v7.8.3 (CXSparse only: g2o's csparse solver, which stella's BA and pose graph use).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${SRC:-$HOME/src/github.com}; P=$HOME/.local/stella; DEP=$HOME/.local/orbslam3
CM=(-DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=$P "-DCMAKE_PREFIX_PATH=$P;$DEP" -DEigen3_DIR=$DEP/share/eigen3/cmake
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 "-DCMAKE_SHARED_LINKER_FLAGS=-L$P/lib" "-DCMAKE_EXE_LINKER_FLAGS=-L$P/lib")
get() {  # get ORG/REPO REF
  [ -d "$SRC/$1" ] || git clone "https://github.com/$1.git" "$SRC/$1"
  git -C "$SRC/$1" checkout -q "$2"
}
get DrTimothyAldenDavis/SuiteSparse v7.8.3
cmake -S "$SRC/DrTimothyAldenDavis/SuiteSparse" -B "$SRC/DrTimothyAldenDavis/SuiteSparse/build-cx" "${CM[@]}" \
  -DSUITESPARSE_ENABLE_PROJECTS="suitesparse_config;cxsparse" -DSUITESPARSE_USE_FORTRAN=OFF -DSUITESPARSE_USE_OPENMP=OFF \
  -DBUILD_TESTING=OFF -DSUITESPARSE_DEMOS=OFF
cmake --build "$SRC/DrTimothyAldenDavis/SuiteSparse/build-cx" -j10 --target install
get jbeder/yaml-cpp 0.8.0
cmake -S "$SRC/jbeder/yaml-cpp" -B "$SRC/jbeder/yaml-cpp/build" "${CM[@]}" -DYAML_CPP_BUILD_TESTS=OFF -DYAML_CPP_BUILD_TOOLS=OFF \
  -DYAML_BUILD_SHARED_LIBS=ON
cmake --build "$SRC/jbeder/yaml-cpp/build" -j10 --target install
get RainerKuemmerle/g2o 20230223_git
cmake -S "$SRC/RainerKuemmerle/g2o" -B "$SRC/RainerKuemmerle/g2o/build" "${CM[@]}" -DBUILD_SHARED_LIBS=ON -DBUILD_UNITTESTS=OFF \
  -DG2O_USE_CHOLMOD=OFF -DG2O_USE_CSPARSE=ON -DG2O_USE_OPENGL=OFF -DG2O_USE_OPENMP=OFF -DG2O_BUILD_APPS=OFF \
  -DG2O_BUILD_EXAMPLES=OFF -DG2O_BUILD_LINKED_APPS=OFF
cmake --build "$SRC/RainerKuemmerle/g2o/build" -j10 --target install
get stella-cv/stella_vslam e445b54
git -C "$SRC/stella-cv/stella_vslam" submodule update --init --recursive
get stella-cv/stella_vslam_examples defc69e
git -C "$SRC/stella-cv/stella_vslam_examples" submodule update --init --recursive
(cd "$SRC/stella-cv" && { git apply -p1 --check "$HERE/macos.patch" 2>/dev/null && git apply -p1 "$HERE/macos.patch" || echo "macos.patch: already applied"; })
# USE_AVX=OFF: FBoW only recognises "aarch64", and CMake reports Apple Silicon as "arm64"
cmake -S "$SRC/stella-cv/stella_vslam" -B "$SRC/stella-cv/stella_vslam/build" "${CM[@]}" -DUSE_OPENMP=OFF -DUSE_AVX=OFF -DBUILD_TESTS=OFF
cmake --build "$SRC/stella-cv/stella_vslam/build" -j10 --target install
cmake -S "$SRC/stella-cv/stella_vslam_examples" -B "$SRC/stella-cv/stella_vslam_examples/build" "${CM[@]}"
cmake --build "$SRC/stella-cv/stella_vslam_examples/build" -j10 --target run_image_slam
get stella-cv/FBoW_orb_vocab main  # orb_vocab.fbow (45 MB)
echo "built $SRC/stella-cv/stella_vslam_examples/build/run_image_slam"
