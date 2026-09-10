#!/usr/bin/env bash
# Compiles the real .mq5 source as C++ against a small MQL5 mock and runs it
# on two data sets: the video's setup, and random-walk data as a sanity check.
set -e
cd "$(dirname "$0")"
python3 convert.py ../SMC_VideoStrategy.mq5 indicator.inc
g++ -std=c++17 -O2 -Wall -Wextra -Wno-unused-parameter -o harness harness.cpp
./harness
