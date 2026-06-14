#!/bin/bash
# WLDUSDT dry-run 示例
python -m bitget_short_pyramid.strategy dry-run \
  --symbol WLDUSDT --size 100 --grid 0.02 --multiplier 1.2 \
  --layers 8 --tp-pct 0.01 --leverage 3 --interval 5
