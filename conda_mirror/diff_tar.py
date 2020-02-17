import os
import sys
import json
import hashlib
import tarfile
from os.path import abspath, isdir, join, relpath


mirror_dir = None
reference_path = './reference.json'


def md5_file(path):
    """
    Return the MD5 hashsum of the file given by `path` in hexadecimal
    representation.
    """
    h = hashlib.new('md5')
    with open(path, 'rb') as fi:
        while 1:
            chunk = fi.read(262144)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def find_repos():
    """
    Asssuming the global `mirror_dir` is set, iterate all sub-directories
    which contain a repodata.json and repodata.json.bz2 file.
    """
    for root, unused_dirs, files in os.walk(mirror_dir):
        if 'repodata.json' in files and 'repodata.json.bz2' in files:
            yield root


def all_repodata():
    """
    Return a dictionary mapping all repository sub-directories to the conda
    package list as respresented by the 'packages' field in repodata.json.
    """
    d = {}
    for repo_path in find_repos():
        with open(join(repo_path, 'repodata.json')) as fi:
            index = json.load(fi)['packages']
        d[repo_path] = index
    return d


def verify_all_repos():
    """
    Verify all the MD5 sum of all conda packages listed in all repodata.json
    files in the repository.
    """
    d = all_repodata()
    for repo_path, index in d.items():
        for fn, info in index.items():
            path = join(repo_path, fn)
            if info['md5'] == md5_file(path):
                continue
            print('MD5 mismatch: %s' % path)


def write_reference():
    """
    Write the "reference file", which is a collection of the content of all
    repodata.json files.
    """
    data = json.dumps(all_repodata(), indent=2, sort_keys=True)
    # make sure we have newline at the end
    if not data.endswith('\n'):
        data += '\n'
    with open(reference_path, 'w') as fo:
        fo.write(data)


def read_reference():
    """
    Read the "reference file" from disk and return its content as a dictionary.
    """
    try:
        with open(reference_path) as fi:
            return json.load(fi)
    except FileNotFoundError:
        sys.exit('No such file: %s' % reference_path)


def get_updates():
    """
    Compare the "reference file" to the actual the repository (all the
    repodata.json files) and iterate the new and updates files in the
    repository.  That is, the files which need to go into the differential
    tarball.
    """
    d1 = read_reference()
    d2 = all_repodata()
    for repo_path, index2 in d2.items():
        index1 = d1.get(repo_path, {})
        if index1 != index2:
            for fn in 'repodata.json', 'repodata.json.bz2':
                yield relpath(join(repo_path, fn), mirror_dir)
        for fn, info2 in index2.items():
            info1 = index1.get(fn, {})
            if info1.get('md5') != info2['md5']:
                yield relpath(join(repo_path, fn), mirror_dir)


def tar_repo(outfile='update.tar', verbose=False):
    """
    Write the so-called differential tarball, see get_updates().
    """
    t = tarfile.open(outfile, 'w')
    for f in get_updates():
        if verbose:
            print('adding: %s' % f)
        t.add(join(mirror_dir, f), f)
    t.close()
    if verbose:
        print("Wrote: %s" % outfile)


def main():
    from optparse import OptionParser

    p = OptionParser(usage="usage: %prog [options] MIRROR_DIRECTORY",
                     description='create "differential" tarballs of a conda '
                                 'mirror repository')

    p.add_option('--create',
                 action="store_true",
                 help="create a differential tarball")

    p.add_option('--reference',
                 action="store_true",
                 help="create a reference point file and exit")

    p.add_option('--show',
                 action="store_true",
                 help="show the files in respect to the latest reference "
                      "point file (which would be included in the "
                      "differential tarball) and exit")

    p.add_option('--verify',
                 action="store_true",
                 help="verify the mirror repository and exit")

    p.add_option('-v', '--verbose',
                 action="store_true")

    p.add_option('--version',
                 action="store_true",
                 help="print version and exit")

    opts, args = p.parse_args()

    if opts.version:
        from conda_mirror import __version__
        print('conda-mirror: %s' % __version__)
        return

    if len(args) != 1:
        p.error('exactly one argument is required, try -h')

    global mirror_dir
    mirror_dir = abspath(args[0])
    if not isdir(mirror_dir):
        sys.exit("No such directory: %r" % mirror_dir)

    if opts.create:
        tar_repo(verbose=opts.verbose)
        return

    if opts.verify:
        verify_all_repos()
        return

    if opts.show:
        for path in get_updates():
            print(path)
        return

    if opts.reference:
        write_reference()
        return

    print("Nothing done.")


if __name__ == '__main__':
    main()
