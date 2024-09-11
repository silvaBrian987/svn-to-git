from os import mkdir, rmdir
import os
import subprocess
import svn.remote
import shutil
import platform
import sys
import psutil
from git import Repo, RemoteReference
import argparse
import signal

if platform.system() == "Windows":
    AUTHORS_DEFAULT_FILEPATH = "authors.txt"
else:
    AUTHORS_DEFAULT_FILEPATH = "./authors.txt"

USER_HOME = os.path.expanduser("~")


def kill_proc_tree(pid, including_parent=True):
    parent = psutil.Process(pid)
    children = parent.children(recursive=True)
    for child in children:
        child.kill()
    gone, still_alive = psutil.wait_procs(children, timeout=5)
    if including_parent:
        parent.kill()
        parent.wait(5)


def execute(cmd):
    # set system/version dependent "start_new_session" analogs
    kwargs = {}
    if platform.system() == "Windows":
        # from msdn [1]
        CREATE_NEW_PROCESS_GROUP = 0x00000200  # note: could get it from subprocess
        DETACHED_PROCESS = 0x00000008  # 0x8 | 0x200 == 0x208
        kwargs.update(creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    elif sys.version_info < (3, 2):  # assume posix
        kwargs.update(preexec_fn=os.setsid)
    else:  # Python 3.2+ and Unix
        kwargs.update(start_new_session=True)
    print(f"Executing: {cmd}")
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True, **kwargs
    )
    output = []
    try:
        print("Waiting for process to finish...")
        for line in iter(process.stdout.readline, b""):
            aux = str(line.decode("utf-8").strip())
            print(aux)
            # print(f'type: {type(aux)}')
            output.append(aux)
        process.stdout.close()
        process.wait()
        return process.returncode, "\n".join(output)
    except KeyboardInterrupt:
        # process.send_signal(signal.SIGINT)
        kill_proc_tree(process.pid, including_parent=False)
        # exit(-1)
    except Exception as e:
        print(e)
        kill_proc_tree(process.pid, including_parent=False)
        return process.returncode, "\n".join(output)


# exception handler
def handler(func, path, exc_info):
    import stat

    # Is the error an access error?
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)
    else:
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser("SVN to Git Migration Tool")
    parser.add_argument("--svn-username", required=True)
    parser.add_argument("--svn-password", required=True)
    parser.add_argument("--svn-repos-file", default="to_migrate.csv")
    parser.add_argument("--svn-authors-file", default=AUTHORS_DEFAULT_FILEPATH)
    parser.add_argument("--svn-revisions", default="BASE:HEAD")
    parser.add_argument("--git-repos-path", default="git_repos/")
    parser.add_argument("--git-base-url", default="https://github.com/username/")
    parser.add_argument("--ignore-history", action="store_true")
    parser.add_argument("--no-stdlayout", action="store_true")
    parser.add_argument("--migrate-from-copy", action="store_true")

    args = parser.parse_args()

    svn_username = args.svn_username
    svn_password = args.svn_password
    remote_url_base = args.git_base_url
    svn_revisions = args.svn_revisions
    no_stdlayout = args.no_stdlayout
    ignore_history = args.ignore_history
    migrate_from_copy = args.migrate_from_copy

    lines = []
    with open(args.svn_repos_file, "r") as f:
        lines = f.readlines()

    if not os.path.exists(args.git_repos_path):
        mkdir(args.git_repos_path)

    for line in lines:
        line = line.strip()
        if line.startswith("#"):
            continue
        line = line.split(",")
        if len(line) != 2:
            continue
        print(line)
        r = svn.remote.RemoteClient(
            line[0], username=svn_username, password=svn_password
        )
        
        svn_info = r.info()
        latest_commit = svn_info["commit_revision"]
        print(f'Last commit: {latest_commit}')
        if migrate_from_copy:
            log = list(r.log_default(stop_on_copy=True))[-1]
            svn_revisions = f"{log.revision}:{latest_commit}"
        if ignore_history:
            svn_revisions = str(int(latest_commit) - 1) + ":" + str(int(latest_commit))
        reponame = line[1]
        repodir = os.path.join(os.path.curdir, "git_repos/", reponame)

        # shutil.rmtree(repodir, onerror = handler)
        cmd = [
            "git",
            "svn",
            "clone",
            f"--username={svn_username}",
            "--stdlayout" if not no_stdlayout else "",
            f"--revision={svn_revisions}" if svn_revisions else "",
            f"--authors-file={args.svn_authors_file}",
            line[0],
            repodir,
        ]
        rc, output = execute(cmd)
        if rc != 0:
            print(f"Error: {rc}\n{output}")
            continue

        repo = Repo(repodir)
        for ref in repo.refs:
            if "remotes/origin" in ref.path:
                remote: RemoteReference = ref
                print(f"Remote branch: {remote.path}")
                remote_path = remote.path
                path_split = remote.path.split("/")
                branch_name = path_split[-1]
                if path_split[-2] == "tags":
                    print(f"Tag: {branch_name}")
                    branch_name = "tags/" + branch_name
                    if branch_name in repo.tags:
                        print(f"Tag already exists: {branch_name}")
                        continue
                    repo.create_tag(
                        branch_name, ref=ref, message="Tag created by svn_to_git.py"
                    )
                    continue
                if branch_name in repo.refs:
                    print(f"Ignoring branch {branch_name} because already exists")
                    continue
                if branch_name == "trunk":
                    print(f"Ignoring trunk because is already the branch master")
                    continue
                # remote.rename(branch_name)
                repo.create_head(branch_name, ref)

        if not "main" in repo.branches and "master" in repo.branches:
            print(f"Creating branch main")
            repo.branches.master.rename("main")

        if not "origin" in repo.remotes:
            print(f"Creating remote url")
            repo.create_remote("origin", remote_url_base + reponame + ".git")
        elif not remote_url_base in repo.remotes.origin.url:
            print(f"Updating remote url")
            repo.remotes.origin.set_url(remote_url_base + reponame + ".git")

        rc, output = execute(["git", "-C", repodir, "push", "origin", "--all"])
        if rc != 0:
            print(f"Error: {rc}\n{output}")
            continue
        rc, output = execute(["git", "-C", repodir, "push", "origin", "--tags"])
        if rc != 0:
            print(f"Error: {rc}\n{output}")
            continue
