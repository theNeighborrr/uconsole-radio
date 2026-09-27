#!/usr/bin/env python3
"""Obtain normal terminal sudo authorization, then run the GUI as this user."""
import os
import subprocess
import sys
import threading


def main():
    if os.geteuid() == 0:
        print('Open uConsole Radio as your normal user, not with sudo.')
        return 1
    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        print('Open the uConsole Radio icon from the Pi desktop, not an SSH terminal.')
        return 1
    print('uConsole Radio — enter your Pi password below if requested.\n'
          'The radio window will open after authorization.\n'
          'Keep this terminal open while using the launcher.', flush=True)
    result = subprocess.run(['sudo', '-v'])
    if result.returncode:
        print('Authorization cancelled. The receiver has not been changed.')
        return result.returncode

    done = threading.Event()

    def refresh_authorization():
        while not done.wait(60):
            try:
                result = subprocess.run(['sudo', '-n', '-v'], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=10)
                if result.returncode:
                    break
            except (OSError, subprocess.TimeoutExpired):
                break

    refresh = threading.Thread(target=refresh_authorization, daemon=True)
    refresh.start()
    try:
        import radio_launcher
        return radio_launcher.main() or 0
    finally:
        done.set()
        refresh.join(timeout=2)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ImportError, OSError) as exc:
        print(f'Cannot start uConsole Radio: {exc}', file=sys.stderr)
        print('Run install-launcher.py --install-deps from the field-kit folder.', file=sys.stderr)
        if sys.stdin.isatty():
            input('Press Enter to close. ')
        sys.exit(1)
