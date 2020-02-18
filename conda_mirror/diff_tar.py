"""
Implementation of the conda-diff-tar command, a tools which allows creating
differential tarballs of a (usually mirrored) conda repository.  The resulting
tarball can be used to update a copy of the mirror on a remote (air-gapped)
system, without having to copy the entire conda repository.
"""
import os
import sys
import json
import hashlib
import tarfile
from os.path import abspath, isdir, join, relpath


MIRROR_DIR = None
REFERENCE_PATH = './reference.json'


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
    Asssuming the global `MIRROR_DIR` is set, iterate all sub-directories
    which contain a repodata.json and repodata.json.bz2 file.
    """
    for root, unused_dirs, files in os.walk(MIRROR_DIR):
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
    with open(REFERENCE_PATH, 'w') as fo:
        fo.write(data)


def read_reference():
    """
    Read the "reference file" from disk and return its content as a dictionary.
    """
    with open(REFERENCE_PATH) as fi:
        return json.load(fi)


def get_updates():
    """
    Compare the "reference file" to the actual the repository (all the
    repodata.json files) and iterate the new and updates files in the
    repository.  That is, the files which need to go into the differential
    tarball.
    """
    try:
        d1 = read_reference()
    except FileNotFoundError:
        no_reference()
    d2 = all_repodata()
    for repo_path, index2 in d2.items():
        index1 = d1.get(repo_path, {})
        if index1 != index2:
            for fn in 'repodata.json', 'repodata.json.bz2':
                yield relpath(join(repo_path, fn), MIRROR_DIR)
        for fn, info2 in index2.items():
            info1 = index1.get(fn, {})
            if info1.get('md5') != info2['md5']:
                yield relpath(join(repo_path, fn), MIRROR_DIR)


def tar_repo(outfile='update.tar', verbose=False):
    """
    Write the so-called differential tarball, see get_updates().
    """
    t = tarfile.open(outfile, 'w')
    for f in get_updates():
        if verbose:
            print('adding: %s' % f)
        t.add(join(MIRROR_DIR, f), f)
    t.close()
    if verbose:
        print("Wrote: %s" % outfile)


def no_reference():
    sys.exit("""
Error: no such file: %s
Please use the --reference option before creating a differential tarball.
    """ % REFERENCE_PATH)


def main():
    import argparse

    p = argparse.ArgumentParser(
        description='create "differential" tarballs of a conda repository')

    p.add_argument('repo_dir',
                   nargs='?',
                   action="store",
                   metavar='REPOSITORY',
                   help="path to repository directory")

    p.add_argument('--create',
                   action="store_true",
                   help="create differential tarball")

    p.add_argument('--reference',
                   action="store_true",
                   help="create a reference point file")

    p.add_argument('--show',
                   action="store_true",
                   help="show the files in respect to the latest reference "
                        "point file (which would be included in the "
                        "differential tarball)")

    p.add_argument('--verify',
                   action="store_true",
                   help="verify the mirror repository and exit")

    p.add_argument('-v', '--verbose',
                   action="store_true")

    p.add_argument('--version',
                   action="store_true",
                   help="print version and exit")

    args = p.parse_args()

    if args.version:
        from conda_mirror import __version__
        print('conda-mirror: %s' % __version__)
        return

    if not args.repo_dir:
        p.error('exactly one REPOSITORY is required, try -h')

    global MIRROR_DIR
    MIRROR_DIR = abspath(args.repo_dir)
    if not isdir(MIRROR_DIR):
        sys.exit("No such directory: %r" % MIRROR_DIR)

    if args.create:
        tar_repo(verbose=args.verbose)
        return

    if args.verify:
        verify_all_repos()
        return

    if args.show:
        for path in get_updates():
            print(path)
        return

    if args.reference:
        write_reference()
        return

    print("Nothing done.")


if __name__ == '__main__':
    main()
