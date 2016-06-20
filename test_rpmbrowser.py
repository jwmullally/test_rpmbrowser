#!/usr/bin/env python2.7

"""
This is a quick proof of concept for a lightweight online RPM content
browser web service for packages from the Fedora build system.

See https://github.com/jwmullally/test_rpmbrowser/README.md for background.
"""

import argparse
import logging
import os
import re
import shutil
import subprocess
import traceback

import requests
import flask
import flask_autoindex
from pygments import highlight, lexers
from pygments.util import ClassNotFound
from pygments.formatters import HtmlFormatter

PKG_CACHE_DIR = os.path.join(os.getcwd(), 'rpmbrowser_pkg_cache') # ideally /var/cache/...
MAX_CACHE_SIZE = 1000000000 # bytes
RPM_SIZE_LIMIT = 100000000 # bytes
UPSTREAM_RPM_URL = 'https://kojipkgs.fedoraproject.org/packages/{name}/{version}/{release}/{architecture}/{filename}' # TODO: pick better mirror to reduce load on kojipkgs


application = flask.Flask(__name__)
autoindex = flask_autoindex.AutoIndex(application, add_url_rules=False)


@application.route('/')
def index():

    example_urls = [
        'https://retrace.fedoraproject.org/faf/problems/1354441/',
        'https://bugzilla.redhat.com/show_bug.cgi?id=1272642',
        'https://git.gnome.org/browse/nautilus/commit/?h=gnome-3-18&id=e5163bab0c95e4490a0d90618093f483d34a299c',
        flask.url_for('browse', rpm_filename='nautilus-debuginfo-3.18.1-1.fc23.x86_64.rpm', path='usr/src/debug/nautilus-3.18.1/src/', _external=True),
        flask.url_for('browse', rpm_filename='nautilus-debuginfo-3.18.1-1.fc23.x86_64.rpm', path='usr/src/debug/nautilus-3.18.1/libnautilus-private/nautilus-progress-info.c', hl_lines='96', _anchor='LINE-96', _external=True),
        flask.url_for('browse', rpm_filename='nautilus-3.18.1-1.fc23.src.rpm', path='BUILD', _external=True),
        flask.url_for('browse', rpm_filename='strace-4.12-1.fc24.src.rpm', path='BUILD/strace-4.12', _external=True),
        flask.url_for('browse', rpm_filename='strace-4.12-1.fc24.armv7hl.rpm', _external=True),
        flask.url_for('browse', rpm_filename='strace-debuginfo-4.12-1.fc24.armv7hl.rpm', path='usr/src/debug', _external=True),
        flask.url_for('browse', rpm_filename='gnome-terminal-3.18.3-2.fc23.src.rpm', path='BUILD/gnome-terminal-3.18.3/src/terminal-screen.c', _external=True) + '?hl_lines=808-810,827#LINE-800',
        flask.url_for('browse', rpm_filename='xorg-x11-drv-intel-2.99.917-19.20151206.fc23.src.rpm', path='BUILD', _external=True),
        flask.url_for('browse', rpm_filename='fake-rpm-1.23-1.fc24.src.rpm', _external=True),
            ]
    return '<pre>'+__doc__+'</pre>' + '<br><br>'.join('<a href="{}">{}</a>'.format(url,url) for url in example_urls)


@application.route('/pygments.css')
def pygments_css():
    return HtmlFormatter().get_style_defs()


@application.route('/robots.txt')
def robots_txt():
    return flask.Response('User-agent: *\nDisallow: /', mimetype='text/plain')


@application.route('/rpm/<rpm_filename>/browse', defaults={'path': '.'})
@application.route('/rpm/<rpm_filename>/browse/', defaults={'path': '.'})
@application.route('/rpm/<rpm_filename>/browse/<path:path>')
def browse(rpm_filename, path):
    parse_rpm_filename(rpm_filename)
    basedir = os.path.join(PKG_CACHE_DIR, rpm_filename)
    abspath = os.path.abspath(os.path.join(basedir, path))
    if '/' in rpm_filename or not abspath.startswith(PKG_CACHE_DIR):
        raise Exception('insecure path: {}'.format(abspath))

    load_rpm_into_cache(rpm_filename)
    if os.path.exists(abspath) and os.path.isfile(abspath):
        try:
            rendered_file = render_with_pygments(abspath, hl_lines=flask.request.args.get('hl_lines', None))
            rendered_dir = autoindex.render_autoindex(os.path.dirname(path), browse_root=basedir, endpoint='.browse')
            return hack_fileview_into_dirview(rendered_file, rendered_dir)
        except (ClassNotFound, UnicodeError):
            pass # let autoindex serve the file as a download
    return autoindex.render_autoindex(path, browse_root=basedir, endpoint='.browse')


def browse_autoindex_urlfor_handler(error, endpoint, values):
    # Hack to workaround autoindex template only passing 'path' 
    # to url_for() generation
    # https://github.com/sublee/flask-autoindex/blob/0192f74141e197dc6da9735e9cf307aff55a7b20/flask_autoindex/templates/__autoindex__/macros.html#L10
    if endpoint != 'browse':
        raise
    return flask.url_for('browse', rpm_filename=flask.request.view_args['rpm_filename'], path=values['path'])
application.url_build_error_handlers.append(browse_autoindex_urlfor_handler)


@application.errorhandler(500)
def error_page(e):
    return flask.Response(traceback.format_exc(), mimetype='text/plain'), 500


def render_with_pygments(file_path, hl_lines):
    """
    :raises: pygments.util.ClassNotFound if no lexer can be found
    """
    try:
        lexer = lexers.get_lexer_for_filename(file_path)
    except:
        with open(file_path) as infile:
            contents_head = infile.read(1000)
        try:
            lexer = lexers.guess_lexer(contents_head)
        except ClassNotFound:
            lexer = lexers.TextLexer()
    formatter = HtmlFormatter(linenos=True, lineanchors='LINE', anchorlinenos=True, hl_lines=line_ranges_to_lines(hl_lines), noclasses=True)
    with open(file_path) as infile:
        contents = infile.read()
    return highlight(contents, lexer, formatter)


def hack_fileview_into_dirview(file_html, dir_html):
    no_file_listing = re.sub('<tbody>[^$]*</tbody>', '', dir_html)
    with_fileview = ('</table>'+file_html+'<address>').join(re.split('</table>[^$]*<address>', no_file_listing))
    return with_fileview


def load_rpm_into_cache(filename):
    if os.path.exists(os.path.join(PKG_CACHE_DIR, filename)):
        return
    fetch_extract_rpm(filename)
    evict_lru_cache()


def fetch_extract_rpm(filename):
    rpm = parse_rpm_filename(filename)
    pkgdir = os.path.join(PKG_CACHE_DIR, filename)
    os.mkdir(pkgdir)
    try:
        rpm['filename'] = filename
        file_url = UPSTREAM_RPM_URL.format(**rpm)
        write_url_to_file(file_url, os.path.join(pkgdir, filename))
        if rpm['architecture'] == 'src':
            subprocess.check_call(['rpm', '--define', '_topdir {}'.format(pkgdir), '-i', os.path.join(pkgdir, filename)], shell=False) 
            subprocess.check_call(['rpmbuild', '--define', '_topdir {}'.format(pkgdir), '--nodeps', '-bp', os.path.join(pkgdir, 'SPECS', '{}.spec'.format(rpm['name']))], shell=False) 
        else:
            process_rpm2cpio = subprocess.Popen(['rpm2cpio', os.path.join(pkgdir, filename)], stdout=subprocess.PIPE, shell=False)
            subprocess.check_call(['cpio', '-idmv'], stdin=process_rpm2cpio.stdout, shell=False, cwd=pkgdir)
        os.remove(os.path.join(pkgdir, filename))
    except:
        shutil.rmtree(pkgdir)
        raise


def evict_lru_cache():
    while len(os.listdir(PKG_CACHE_DIR)) > 1 and get_dir_size(PKG_CACHE_DIR) > MAX_CACHE_SIZE:
        lru_pkg = min((os.path.getatime(os.path.join(dirpath, filenm)), filenm) for filenm in os.listdir(dirpath))[1]
        shutil.rmtree(os.path.join(PKG_CACHE_DIR, lru_pkg))


def parse_rpm_filename(filename):
    """
    > parse_rpm_filename('strace-4.12-1.fc24.x86_64.rpm')
    {
        'name': 'strace',
        'version': '4.12'
        'release': '1.fc24',
        'architecture': 'x86_64',
        'debuginfo': False
    }
    """
    rpm_regex = '^(?P<name>[a-zA-Z0-9\-\._+]+)-(?P<version>[^-]+)-(?P<release>[^-]+)\.(?P<architecture>[^\.]+)\.rpm$'
    m = re.match(rpm_regex, filename)
    if not m:
        raise ValueError('"{}" does not match RPM filename regex: "{}"'.format(filename, rpm_regex))
    result = m.groupdict()
    if result['name'].endswith('-debuginfo'):
        # would be nice to handle this in the regex...
        result['name'] = result['name'][:-len('-debuginfo')]
        result['debuginfo'] = True
    else:
        result['debuginfo'] = False
    return result


def line_ranges_to_lines(line_ranges):
    if not line_ranges:
        return []
    if not re.match('(\d+(-\d+)?,?)*', line_ranges):
        raise ValueError('Invalid line ranges: "{}", expecting something like "1,5-9,20"'.format(ranges))
    ranges = (x.split("-") for x in line_ranges.split(","))
    return [i for r in ranges for i in range(int(r[0]), int(r[-1]) + 1)]


def write_url_to_file(file_url, output_path):
    size = int(requests.head(file_url).headers.get('content-length', 0))
    if size > RPM_SIZE_LIMIT:
        raise ValueError('{} size {} exceeds limit {}'.format(file_url, size, RPM_SIZE_LIMIT))
    request = requests.get(file_url, stream=True)
    request.raise_for_status()
    with open(output_path, 'wb') as outfile:
        request.raw.decode_content = True
        shutil.copyfileobj(request.raw, outfile)


def get_dir_size(path):
    size = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for filename in filenames:
            try:
                size += os.path.getsize(os.path.join(dirpath, filename))
            except:
                pass
    return size
