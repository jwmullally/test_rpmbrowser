import argparse
import os
import shutil

import test_rpmbrowser
from test_rpmbrowser import application

if os.path.exists(test_rpmbrowser.PKG_CACHE_DIR):
    shutil.rmtree(test_rpmbrowser.PKG_CACHE_DIR)
os.mkdir(test_rpmbrowser.PKG_CACHE_DIR)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', default=False)
    args = parser.parse_args()

    if args.debug:
        test_rpmbrowser.UPSTREAM_RPM_URL = 'http://localhost:8000/{filename}'
        application.run('127.0.0.1', debug=True)
    else:
        application.run('0.0.0.0')
