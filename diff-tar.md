Create differential tarballs
============================

This tools allows you to create differential tarballs of a (usually
mirrored) conda repository.  The resulting tarball can be used to update
a copy of the mirror on a remote (air-gapped) system, without having to
copy the entire conda repository.

Usage:
------
Running `conda-diff-tar --help` will show the following output:

```
usage: conda-diff-tar [-h] [--create] [--reference] [-o OUTFILE] [-i INFILE]
                      [--show] [--verify] [-v] [--version]
                      [REPOSITORY]

create "differential" tarballs of a conda repository

positional arguments:
  REPOSITORY            path to repository directory

optional arguments:
  -h, --help            show this help message and exit
  --create              create differential tarball
  --reference           create a reference point file
  -o OUTFILE, --outfile OUTFILE
                        Path to references json file when using --reference,
                        or update tarfile when using --create
  -i INFILE, --infile INFILE
                        Path to specify references json file when using
                        --create or --show
  --show                show the files in respect to the latest reference
                        point file (which would be included in the
                        differential tarball)
  --verify              verify the mirror repository and exit
  -v, --verbose
  --version             print version and exit
```

Example workflow:
-----------------

  1. we assume that the remote and local repository are in sync
  2. create a `reference.json` file of the local repository with the `--reference` flag
  3. update the local repository using `conda-mirror` or some other tools
  4. create the "differential" tarball with the `--create` flag
  5. move the differential tarball to the remote machine, and unpack it
  6. now that the remote repository is up-to-date, we should create a new
     `reference.json` on the local machine.  That is, repeat step 2


Notes:
------

The file `reference.json` (or whatever you named it) is a collection of all `repodata.json`
files (`linux-64`, `win-32`, `noarch`, etc.) in the local repository.
It is created in order to compare a future state of the repository to the
state of the repository when `reference.json` was created.

The differential tarball contains files which either have been updated (such
as `repodata.json`) or new files (new conda packages).  It is meant to be
unpacked on top of the existing mirror on the remote machine by:

    cd <repository>
    tar xf update.tar
    # or y using tar's -C option from any directory
    tar xf update.tar -C <repository>

Example:
--------

In this example we assume that a conda mirror is located in `./repo`.
Create `reference.json`:

    conda-diff-tar --reference ./repo

Show the files in respect to the latest reference point file (which would be
included in the differential tarball).  Since we just created the reference
file, we don't expect any output:

    conda-diff-tar --show ./repo

Now, we can update the mirror:

    conda-mirror --upstream-channel conda-forge --target-directory ./repo ...

Create the actual differential tarball:

    $ conda-diff-tar --create ./repo
    Wrote: update.tar
    $ tar tf update.tar
    noarch/repodata.json
    noarch/repodata.json.bz2
    noarch/ablog-0.9.2-py_0.tar.bz2
    noarch/aws-amicleaner-0.2.2-py_0.tar.bz2
    ...
