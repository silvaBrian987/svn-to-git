from os import mkdir, rmdir
import os
import re
import shutil
import subprocess
import svn.remote
import platform
import sys
import psutil
from git import Repo, RemoteReference
import argparse
from csv import DictReader
import traceback

if platform.system() == "Windows":
    AUTHORS_DEFAULT_FILEPATH = "authors.txt"
else:
    AUTHORS_DEFAULT_FILEPATH = "./authors.txt"

USER_HOME = os.path.expanduser("~")

class RepoToMigrate:
    def __init__(self, svn_url, git_path, svn_revisions=None):
        self.svn_url = svn_url
        self.git_path = git_path
        self.svn_revisions = svn_revisions


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
        kwargs.update(creationflags=DETACHED_PROCESS |
                      CREATE_NEW_PROCESS_GROUP)
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
            # print(aux)
            # print(f'type: {type(aux)}')
            output.append(aux)
            print(aux)
        process.stdout.close()
        process.wait()
        return process.returncode, "\n".join(output)
    except KeyboardInterrupt:
        # process.send_signal(signal.SIGINT)
        kill_proc_tree(process.pid, including_parent=False)
        # exit(-1)
    except Exception as e:
        print(f"Error: {e}")
        kill_proc_tree(process.pid, including_parent=False)
        return process.returncode, "\n".join(output)


COMMITTER_EMAIL_BLOCKED_RE = re.compile(
    r"You cannot push commits for '([^']+)'"
)


def rewrite_committer_identity(repodir, replacement_name, replacement_email):
    """Rewrite author and committer name+email on all commits using git filter-branch."""
    print(
        f"Rewriting all commits identity to "
        f"'{replacement_name} <{replacement_email}>' in {repodir}"
    )
    env_filter = (
        f"GIT_AUTHOR_NAME='{replacement_name}'\n"
        f"GIT_AUTHOR_EMAIL='{replacement_email}'\n"
        f"GIT_COMMITTER_NAME='{replacement_name}'\n"
        f"GIT_COMMITTER_EMAIL='{replacement_email}'\n"
    )
    rc, output = execute([
        "git", "-C", repodir,
        "filter-branch", "-f",
        "--env-filter", env_filter,
        "--tag-name-filter", "cat",
        "--", "--branches",
    ])
    if rc != 0:
        raise Exception(f"git filter-branch failed:\n{output}")


# exception handler
def handler(func, path, exc_info):
    import stat

    # Is the error an access error?
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)
    else:
        raise


def push_branch_with_identity_rewrite(repodir, branch_name, replacement_name, replacement_email):
    """Push branch; if GitLab rejects due to committer identity policy,
    rewrite all commits with the provided name+email and retry once."""
    rc, output = execute(["git", "-C", repodir, "push", "origin", branch_name])
    if rc == 0:
        return rc, output
    if COMMITTER_EMAIL_BLOCKED_RE.search(output) and replacement_email:
        blocked_email = COMMITTER_EMAIL_BLOCKED_RE.search(output).group(1)
        print(
            f"Push rejected for committer '{blocked_email}'. "
            "Rewriting identity and retrying..."
        )
        rewrite_committer_identity(repodir, replacement_name, replacement_email)
        # After rewriting, re-merge remote commits that Backstage may have
        # added when creating the repo (unrelated histories).
        execute(["git", "-C", repodir, "fetch", "origin", "--tags"])
        rc_pull, out_pull = execute([
            "git", "-C", repodir, "pull", "origin", branch_name,
            "--allow-unrelated-histories", "--no-rebase",
        ])
        if rc_pull != 0:
            print(f"WARN: pull after rewrite failed (will attempt push anyway):\n{out_pull}")
        rc, output = execute(["git", "-C", repodir, "push", "origin", branch_name])
    return rc, output


def migrateRepo(repoToMigrate: RepoToMigrate, git_base_url, svn_username, svn_password, svn_authors_file, migrate_from_copy=False, ignore_history=False, no_stdlayout=False, skip_svn_clone=False, git_committer_name=None, git_committer_email=None):
    svn_url = repoToMigrate.svn_url
    svn_repo = svn.remote.RemoteClient(
        svn_url, username=svn_username, password=svn_password
    )

    if not skip_svn_clone:
        cmd = ["svn", "--non-interactive", "--username", svn_username,
               "--password", svn_password, "info", svn_url]
        rc, output = execute(cmd)
        if rc != 0:
            raise Exception(f"Error: {rc}\n{output}")

    if not skip_svn_clone:
        svn_revisions = repoToMigrate.svn_revisions
        if not svn_revisions or (migrate_from_copy or ignore_history):
            svn_info = svn_repo.info()
            latest_commit = svn_info["commit_revision"]
            print(f'Last commit: {latest_commit}')
            svn_revisions = f"BASE:{latest_commit}"

            # Ignore-history has precedence over migrate-from-copy because it
            # explicitly requests only the latest commit.
            if ignore_history:
                svn_revisions = str(int(latest_commit) - 1) + \
                    ":" + str(int(latest_commit))
            elif migrate_from_copy:
                try:
                    log = list(svn_repo.log_default(stop_on_copy=True))[-1]
                    svn_revisions = f"{log.revision}:{latest_commit}"
                except Exception as ex:
                    # Python 3.12 breaks some versions of python-svn
                    # (Element.getchildren removed). Fall back to full history.
                    print(
                        "WARN: Could not compute copy revision with python-svn "
                        f"({ex}). Falling back to BASE:{latest_commit}."
                    )
    reponame = repoToMigrate.git_path
    repodir = os.path.join(os.path.curdir, "git_repos/", reponame)

    if not skip_svn_clone:
        # shutil.rmtree(repodir, onerror = handler)
        execute(["svn", "checkout", "--username", svn_username, "--password", svn_password, "--depth", "empty", svn_url, repodir])
        keep_goin = True
        while keep_goin:
            cmd = [
                "git",
                "svn",
                "clone",
                f"--username={svn_username}",
                "--stdlayout" if not no_stdlayout else "",
                f"--revision={svn_revisions}" if svn_revisions else "",
                f"--authors-file={svn_authors_file}",
                svn_url,
                repodir,
            ]
            rc, output = execute(cmd)
            print(f"rc: {rc}")
            if rc == 0 or rc == 128:  # 128 means repository already exists
                keep_goin = False
            else:
                raise Exception(f"Error: {rc}\n{output}")
    repo = Repo(repodir)
    # git_cmd = repo.git
    # git_cmd.svn("clone",
    #         f"--username={svn_username}",
    #         "--stdlayout" if not no_stdlayout else "",
    #         f"--revision={svn_revisions}" if svn_revisions else "",
    #         f"--authors-file={svn_authors_file}",
    #         svn_url,
    #         repodir
    #         )

    for ref in repo.refs:
        print(f"Processing ref: {ref.path}")
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
                print(
                    f"WARN: Ignoring branch {branch_name} because already exists")
                continue
            if branch_name == "HEAD":
                print("WARN: Ignoring HEAD symbolic ref")
                continue
            if branch_name == "trunk":
                print(f"WARN: Ignoring trunk because is already the branch master")
                continue
            # remote.rename(branch_name)
            print(f"Creating branch {branch_name} from {remote_path}")
            repo.create_head(branch_name, ref)
    
    if not "main" in repo.branches and "master" in repo.branches:
        print(f"Creating branch main")
        repo.branches.master.rename("main")

    if "HEAD" in repo.branches:
        print("Removing invalid local branch HEAD")
        repo.delete_head("HEAD", force=True)

    remote_url = git_base_url + reponame + ".git"
    if not "origin" in repo.remotes:
        print(f"Creating remote url {remote_url}")
        repo.create_remote("origin", remote_url)
    elif not git_base_url in repo.remotes.origin.url:
        print(f"Updating remote url {remote_url}")
        repo.remotes.origin.set_url(remote_url)

    rc, output = execute(["git", "-C", repodir, "fetch", "origin", "--tags"])
    if rc != 0:
        raise Exception(f"Error: {rc}\n{output}")

    local_branch = "main" if "main" in repo.branches else "master"
    rc, output = execute(["git", "-C", repodir, "checkout", local_branch])
    if rc != 0:
        raise Exception(f"Error: {rc}\n{output}")

    rc, output = execute(["git", "-C", repodir, "ls-remote", "--heads", "origin", "main"])
    if rc != 0:
        raise Exception(f"Error: {rc}\n{output}")

    if output.strip():
        print("Remote branch main exists. Merging unrelated histories before push.")
        rc, output = execute([
            "git",
            "-C",
            repodir,
            "pull",
            "origin",
            "main",
            "--allow-unrelated-histories",
            "--no-rebase",
        ])
        if rc != 0:
            raise Exception(f"Error: {rc}\n{output}")

    for branch in repo.branches:
        if branch.name == "HEAD":
            continue
        rc, output = push_branch_with_identity_rewrite(
            repodir, branch.name, git_committer_name, git_committer_email
        )
        if rc != 0:
            raise Exception(f"Error: {rc}\n{output}")

    rc, output = execute(["git", "-C", repodir, "push", "origin", "--tags"])
    if rc != 0:
        raise Exception(f"Error: {rc}\n{output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser("SVN to Git Migration Tool")
    parser.add_argument("--svn-username", required=True)
    parser.add_argument("--svn-password", required=True)
    parser.add_argument("--svn-repos-file", default="to_migrate.csv")
    parser.add_argument("--svn-authors-file", default=AUTHORS_DEFAULT_FILEPATH)
    parser.add_argument("--svn-revisions", default="BASE:HEAD")
    parser.add_argument("--git-repos-path", default="git_repos/")
    parser.add_argument(
        "--git-base-url", default="https://github.com/username/")
    parser.add_argument("--ignore-history", action="store_true")
    parser.add_argument("--no-stdlayout", action="store_true")
    parser.add_argument("--migrate-from-copy", action="store_true")
    parser.add_argument("--skip-svn-clone", action="store_true")
    parser.add_argument(
        "--git-committer-name",
        default=None,
        help="Full name to use when rewriting committer identity (e.g. 'Brian Silva').",
    )
    parser.add_argument(
        "--git-committer-email",
        default=None,
        help="Verified GitLab email to use when a push is rejected due to committer identity policy.",
    )

    args = parser.parse_args()

    svn_username = args.svn_username
    svn_password = args.svn_password
    remote_url_base = args.git_base_url
    svn_revisions = args.svn_revisions
    no_stdlayout = args.no_stdlayout
    ignore_history = args.ignore_history
    migrate_from_copy = args.migrate_from_copy
    svn_authors_file = args.svn_authors_file
    skip_svn_clone = args.skip_svn_clone
    git_committer_name = args.git_committer_name
    git_committer_email = args.git_committer_email

    if ignore_history and migrate_from_copy:
        print(
            "WARN: --ignore-history and --migrate-from-copy were both set. "
            "Using --ignore-history (last revision only)."
        )

    if os.path.exists(args.git_repos_path):
        print(f"Removing existing {args.git_repos_path}...")
        shutil.rmtree(args.git_repos_path)
    mkdir(args.git_repos_path)

    with open(args.svn_repos_file, "r") as f:
        reader = DictReader(f)

        for row in reader:
            repoToMigrate = RepoToMigrate(svn_url=row.get("svn_url"),
                                          git_path=row.get("git_path"),
                                          svn_revisions=row.get("svn_revisions", svn_revisions))
            if repoToMigrate.svn_url.startswith("#"):
                continue  # skip comments

            try:
                print(
                    f"Migrating {repoToMigrate.svn_url} to {repoToMigrate.git_path}")
                migrateRepo(repoToMigrate, git_base_url=remote_url_base, svn_username=svn_username, svn_password=svn_password,
                            svn_authors_file=svn_authors_file, migrate_from_copy=migrate_from_copy, ignore_history=ignore_history, no_stdlayout=no_stdlayout, skip_svn_clone=skip_svn_clone, git_committer_name=git_committer_name, git_committer_email=git_committer_email)
                print(f"Migration of {repoToMigrate.svn_url} completed.")
            except Exception as e:
                print(f"Error on migration of {repoToMigrate.svn_url}: {e}")
                traceback.print_exc()
                continue
