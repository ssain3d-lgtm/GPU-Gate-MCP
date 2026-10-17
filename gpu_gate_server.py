"""Root launcher for the GPU-Gate-MCP stdio server."""
import os
import sys


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gpu_gate.__main__ import main as _main

    return _main()


if __name__ == "__main__":
    sys.exit(main())
