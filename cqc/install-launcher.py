#!/usr/bin/env python3
"""Install the user-owned radio launcher; no service or radio changes at install."""
import argparse
from datetime import datetime
import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

FILES = ('launch-radio.py', 'radio_launcher.py', 'radio_backend.py', 'stations.json')


def dependency_packages():
    packages = set()
    if importlib.util.find_spec('tkinter') is None or importlib.util.find_spec('_tkinter') is None:
        packages.add('python3-tk')
    for command, package in [('rtl_fm', 'rtl-sdr'), ('aplay', 'alsa-utils'),
                             ('fuser', 'psmisc'), ('lsusb', 'usbutils'),
                             ('pgrep', 'procps'), ('xdg-open', 'xdg-utils')]:
        if shutil.which(command) is None:
            packages.add(package)
    return sorted(packages)


def desktop_quote(value):
    # Desktop Entry escaping occurs before Exec argument parsing.
    if '\n' in value or '\r' in value:
        raise ValueError('A desktop launcher path cannot contain newlines.')
    value = value.replace('\\', '\\\\\\\\').replace('"', '\\\\"')
    value = value.replace('`', '\\\\`').replace('$', '\\\\$').replace('%', '%%')
    return '"' + value + '"'


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-deps', action='store_true', help='Install missing Debian packages with apt.')
    parser.add_argument('--classic', action='store_true', help='Install the bundled classic v1.3 launcher instead of arrow navigation.')
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise RuntimeError('Run this installer as user, without sudo. It requests sudo only for missing packages.')
    source = Path(__file__).resolve().parent
    payload = source / ('launcher-classic' if args.classic else 'launcher')
    mode = 'classic v1.3' if args.classic else 'arrow navigation'
    files = FILES if args.classic else (*FILES, 'sdrpp_presets.py')
    for name in files:
        if not (payload / name).is_file():
            raise RuntimeError('Missing ' + payload.name + '/' + name + '. Extract the entire ZIP first.')
    for command in ('sudo', 'systemctl', 'aiov2_ctl'):
        if shutil.which(command) is None:
            raise RuntimeError(f'{command} is missing. This installer is for the configured uConsole Pi.')
    packages = dependency_packages()
    if packages:
        if not args.install_deps:
            flags = '--classic --install-deps' if args.classic else '--install-deps'
            raise RuntimeError('Missing packages: ' + ', '.join(packages) + '. Run again with ' + flags + ' while online.')
        print('Installing missing packages: ' + ', '.join(packages), flush=True)
        subprocess.run(['sudo', 'apt-get', 'update'], check=True)
        subprocess.run(['sudo', 'apt-get', 'install', '-y', *packages], check=True)
        importlib.invalidate_caches()
        if dependency_packages():
            raise RuntimeError('Some dependencies are still missing. Review the package output above.')

    home = Path.home()
    cache = Path(os.environ.get('XDG_CACHE_HOME', str(home / '.cache'))) / 'uconsole-radio'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (cache / 'launcher.lock').open('a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Close uConsole Radio before updating it.')
    target = home / '.local' / 'share' / 'uconsole-radio'
    if target.exists():
        backup = home / '.local' / 'share' / 'uconsole-radio-backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        shutil.copytree(target, backup)
        print('Previous launcher backed up to ' + str(backup))
    target.mkdir(parents=True, exist_ok=True)
    for name in files:
        atomic_write(target / name, (payload / name).read_bytes(), 0o644)
    command = '/usr/bin/python3 ' + desktop_quote(str(target / 'launch-radio.py'))
    command += ' --guide-dir ' + desktop_quote(str(source))
    comment = ('Weather, FM, tower and aircraft radio controls' if args.classic else
               'Weather, FM, tower, aircraft, SDR++ and Meshtastic radio controls')
    entry = ('[Desktop Entry]\nVersion=1.0\nType=Application\nName=uConsole Radio\n'
             'Comment=' + comment + '\n'
             'Exec=' + command + '\nIcon=audio-radio\nTerminal=true\n'
             'Categories=AudioVideo;Audio;\nStartupNotify=false\n').encode('utf-8')
    desktop = home / 'Desktop'
    if shutil.which('xdg-user-dir'):
        found = subprocess.run(['xdg-user-dir', 'DESKTOP'], capture_output=True, text=True)
        if found.returncode == 0 and found.stdout.strip() and Path(found.stdout.strip()).is_absolute():
            desktop = Path(found.stdout.strip())
    desktop_entry = desktop / 'uConsole-Radio.desktop'
    atomic_write(desktop_entry, entry, 0o755)
    atomic_write(home / '.local' / 'share' / 'applications' / 'uconsole-radio.desktop', entry)
    print('\nInstalled uConsole Radio (' + mode + ').\n'
          'On the Pi desktop, double-click uConsole Radio.\n'
          'If asked by the file manager, choose Execute / Launch.\n'
          'Enter your Pi password in its terminal if requested.\n'
          'No radio program was started during installation.\n'
          'Guide: ' + str(source / 'radio-launcher.html'))
    lock.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print('Installation stopped: ' + str(exc), file=sys.stderr)
        sys.exit(1)
