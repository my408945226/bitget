"""包入口: python -m bitget_short_pyramid"""
import sys

if __name__ == "__main__":
    # 检查是否指定了 monitor 命令
    if len(sys.argv) > 1 and sys.argv[1] == "monitor":
        # 移除 "monitor" 参数，留下其他参数给 config parser
        sys.argv.pop(1)
        from .monitor import main
    else:
        from .strategy import main

    main()
