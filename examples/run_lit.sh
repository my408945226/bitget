#!/bin/bash
# LITUSDT dry-run 示例
python -m bitget_short_pyramid.strategy dry-run \
  --symbol LITUSDT --size 30 --grid-pct 0.025 --multiplier 1.15 \
  --layers 10 --tp-pct 0.012 --leverage 2 --interval 5
