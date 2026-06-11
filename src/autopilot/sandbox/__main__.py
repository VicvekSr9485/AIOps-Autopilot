"""Deterministic sandbox lifecycle CLI: python -m autopilot.sandbox {up,down,reset,probe}"""

import argparse
import sys

from autopilot.sandbox.controller import SandboxController


def main() -> int:
    parser = argparse.ArgumentParser(prog="autopilot.sandbox")
    parser.add_argument("command", choices=["up", "down", "reset", "probe"])
    args = parser.parse_args()

    ctrl = SandboxController()
    if args.command == "up":
        ctrl.up()
    elif args.command == "down":
        ctrl.down()
    elif args.command == "reset":
        ctrl.reset()
    elif args.command == "probe":
        print(ctrl.probe().model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
