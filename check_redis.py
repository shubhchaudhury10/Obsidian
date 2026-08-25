"""Quick Redis health check — run this after a Windows reboot to confirm Redis is up.

    python check_redis.py

Reads REDIS_URL from .env (default redis://localhost:6379/0), pings Redis, and prints a
clear OK/FAIL. Exit code 0 = reachable, 1 = not. Redis runs in WSL (Ubuntu-24.04) and is
set to auto-start at login; if this FAILs, start it with:
    wsl -d Ubuntu-24.04 -u root -- systemctl start redis-server
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')


def main():
    try:
        import redis
    except ImportError:
        print('FAIL: the `redis` package is not installed (pip install -r requirements.txt).')
        return 1

    print(f'Pinging {REDIS_URL} ...')
    try:
        client = redis.from_url(REDIS_URL, socket_connect_timeout=3)
        if client.ping():
            info = client.info('server')
            ver = info.get('redis_version', '?')
            print(f'OK: Redis is up (version {ver}).')
            return 0
        print('FAIL: Redis did not reply to PING.')
        return 1
    except Exception as exc:
        print(f'FAIL: could not reach Redis at {REDIS_URL}\n       {type(exc).__name__}: {exc}')
        print('\nStart it with:  wsl -d Ubuntu-24.04 -u root -- systemctl start redis-server')
        return 1


if __name__ == '__main__':
    sys.exit(main())
