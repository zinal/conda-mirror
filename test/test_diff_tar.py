import os
import sys
import json
import shutil
import unittest
import tempfile
from os.path import isfile, join

import conda_mirror.diff_tar as dt


EMPTY_MD5 = 'd41d8cd98f00b204e9800998ecf8427e'


class DiffTarTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        dt.mirror_dir = join(self.tmpdir, 'repo')
        dt.reference_path = join(self.tmpdir, 'reference.json')

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_md5_file(self):
        tmpfile = join(self.tmpdir, 'testfile')
        with open(tmpfile, 'wb') as fo:
            fo.write(b'A\n')
        self.assertEqual(dt.md5_file(tmpfile),
                         'bf072e9119077b4e76437a93986787ef')

    def create_test_repo(self, subdirname='linux-64'):
        subdir = join(dt.mirror_dir, subdirname)
        os.makedirs(subdir)
        with open(join(subdir, 'repodata.json'), 'w') as fo:
            fo.write(json.dumps({'packages':
                                 {'a-1.0-0.tar.bz2': {'md5': EMPTY_MD5}}}))
        for fn in 'repodata.json.bz2', 'a-1.0-0.tar.bz2':
            with open(join(subdir, fn), 'wb') as fo:
                pass

    def test_find_repos(self):
        self.create_test_repo()
        self.assertEqual(list(dt.find_repos()),
                         [join(dt.mirror_dir, 'linux-64')])

    def test_all_repodata_repos(self):
        self.create_test_repo()
        d = dt.all_repodata()
        self.assertEqual(
            d[join(dt.mirror_dir, 'linux-64')]['a-1.0-0.tar.bz2']['md5'],
            EMPTY_MD5)

    def test_verify_all_repos(self):
        self.create_test_repo()
        dt.verify_all_repos()

    def test_write_and_read_reference(self):
        self.create_test_repo()
        dt.write_reference()
        ref = dt.read_reference()
        self.assertEqual(
            ref[join(dt.mirror_dir, 'linux-64')]['a-1.0-0.tar.bz2']['md5'],
            EMPTY_MD5)

    def test_get_updates(self):
        self.create_test_repo()
        dt.write_reference()
        self.assertEqual(list(dt.get_updates()), [])

        self.create_test_repo('win-32')
        lst = sorted(dt.get_updates())
        self.assertEqual(lst, ['win-32/a-1.0-0.tar.bz2',
                               'win-32/repodata.json',
                               'win-32/repodata.json.bz2'])

    def test_tar_repo(self):
        self.create_test_repo()
        tarball = join(self.tmpdir, 'up.tar')
        dt.write_reference()
        self.create_test_repo('win-32')
        dt.tar_repo(tarball)
        self.assertTrue(isfile(tarball))

    def run_with_args(self, args):
        old_args = list(sys.argv)
        sys.argv = ['conda-diff-tar'] + args
        dt.main()
        sys.argv = old_args

    def test_version(self):
        self.run_with_args(['--version'])

    def test_misc(self):
        self.create_test_repo()
        self.run_with_args(['--reference', dt.mirror_dir])
        self.assertTrue(isfile(dt.reference_path))
        self.create_test_repo('win-32')
        self.run_with_args(['--show', dt.mirror_dir])
        self.run_with_args(['--create', '--verbose', dt.mirror_dir])
        self.run_with_args(['--verify', dt.mirror_dir])
        self.run_with_args([dt.mirror_dir])  # do nothing


if __name__ == '__main__':
    unittest.main()
