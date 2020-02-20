import os
import sys
import json
import shutil
import tempfile
from os.path import isfile, join

import pytest

import conda_mirror.diff_tar as dt


EMPTY_MD5 = 'd41d8cd98f00b204e9800998ecf8427e'


@pytest.fixture
def tmpdir():
    tmpdir = tempfile.mkdtemp()
    dt.MIRROR_DIR = join(tmpdir, 'repo')
    dt.REFERENCE_PATH = join(tmpdir, 'reference.json')
    yield tmpdir
    shutil.rmtree(tmpdir)


def test_md5_file(tmpdir):
    tmpfile = join(tmpdir, 'testfile')
    with open(tmpfile, 'wb') as fo:
        fo.write(b'A\n')
    assert dt.md5_file(tmpfile) == 'bf072e9119077b4e76437a93986787ef'


def create_test_repo(subdirname='linux-64'):
    subdir = join(dt.MIRROR_DIR, subdirname)
    os.makedirs(subdir)
    with open(join(subdir, 'repodata.json'), 'w') as fo:
        fo.write(json.dumps({'packages':
                             {'a-1.0-0.tar.bz2': {'md5': EMPTY_MD5}}}))
    for fn in 'repodata.json.bz2', 'a-1.0-0.tar.bz2':
        with open(join(subdir, fn), 'wb') as fo:
            pass


def test_find_repos(tmpdir):
    create_test_repo()
    assert list(dt.find_repos()) == [join(dt.MIRROR_DIR, 'linux-64')]


def test_all_repodata_repos(tmpdir):
    create_test_repo()
    d = dt.all_repodata()
    assert d[join(dt.MIRROR_DIR, 'linux-64')]['a-1.0-0.tar.bz2']['md5'] == \
        EMPTY_MD5


def test_verify_all_repos(tmpdir):
    create_test_repo()
    dt.verify_all_repos()


def test_write_and_read_reference(tmpdir):
    create_test_repo()
    dt.write_reference()
    ref = dt.read_reference()
    assert ref[join(dt.MIRROR_DIR, 'linux-64')]['a-1.0-0.tar.bz2']['md5'] == \
        EMPTY_MD5


def test_get_updates(tmpdir):
    create_test_repo()
    dt.write_reference()
    assert list(dt.get_updates()) == []

    create_test_repo('win-32')
    lst = sorted(dt.get_updates())
    assert lst == ['win-32/a-1.0-0.tar.bz2',
                   'win-32/repodata.json',
                   'win-32/repodata.json.bz2']


def test_tar_repo(tmpdir):
    create_test_repo()
    tarball = join(tmpdir, 'up.tar')
    dt.write_reference()
    create_test_repo('win-32')
    dt.tar_repo(tarball)
    assert isfile(tarball)


def run_with_args(args):
    old_args = list(sys.argv)
    sys.argv = ['conda-diff-tar'] + args
    dt.main()
    sys.argv = old_args


def test_version():
    run_with_args(['--version'])


def test_misc(tmpdir):
    create_test_repo()
    run_with_args(['--reference', dt.MIRROR_DIR])
    assert isfile(dt.REFERENCE_PATH)
    create_test_repo('win-32')
    run_with_args(['--show', dt.MIRROR_DIR])
    run_with_args(['--create', '--verbose', dt.MIRROR_DIR])
    run_with_args(['--verify', dt.MIRROR_DIR])
    run_with_args([dt.MIRROR_DIR])  # do nothing
