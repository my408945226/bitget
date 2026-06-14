#!/bin/bash
# TONUSDT dry-run 示例
python -m bitget_short_pyramid.strategy dry-run \
  --symbol TONUSDT --size 50 --grid-pct 0.02 --multiplier 1.3 \
  --layers 6 --tp-pct 0.015 --leverage 3 --interval 5
