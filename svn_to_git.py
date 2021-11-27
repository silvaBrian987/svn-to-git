from os import mkdir, rmdir
import os
import subprocess
import svn.remote
import shutil
import platform
import sys
import psutil
from git import Repo, RemoteReference

if platform.system() == 'Windows':
    AUTHORS_FILEPATH = 'authors.txt'
else:
    AUTHORS_FILEPATH = '/tmp/authors.txt'

USER_HOME = os.path.expanduser('~')
SVN_USERNAME = 'svnuser'
SVN_PASSWORD = '123456'
REMOTE_URL_BASE = 'https://github.com/username/'

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
    if platform.system() == 'Windows':
        # from msdn [1]
        CREATE_NEW_PROCESS_GROUP = 0x00000200  # note: could get it from subprocess
        DETACHED_PROCESS = 0x00000008          # 0x8 | 0x200 == 0x208
        kwargs.update(creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)  
    elif sys.version_info < (3, 2):  # assume posix
        kwargs.update(preexec_fn=os.setsid)
    else:  # Python 3.2+ and Unix
        kwargs.update(start_new_session=True)
    print(f'Executing: {cmd}')
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True, **kwargs)
    output = []
    try:
        print('Waiting for process to finish...')
        for line in iter(process.stdout.readline, b''):
            aux = str(line.decode("utf-8").strip())
            print(aux)
            # print(f'type: {type(aux)}')
            output.append(aux)
        process.stdout.close()
        process.wait()
        return process.returncode, '\n'.join(output)
    except Exception as e:
        print(e)
        kill_proc_tree(process.pid, including_parent=False)
        return process.returncode, '\n'.join(output)

# exception handler
def handler(func, path, exc_info):
    import stat
    # Is the error an access error?
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)
    else:
        raise

if __name__ == '__main__':
    lines = []
    with open('to_migrate.csv', 'r') as f:
        lines = f.readlines()

    if not os.path.exists(os.path.join(os.path.curdir, 'git_repos/')):
        mkdir('git_repos/')
    
    for line in lines:
        line = line.strip()
        if line.startswith('#'):
            continue
        line = line.split(',')
        if len(line) != 2:
            continue
        print(line)
        r = svn.remote.RemoteClient(line[0], username=SVN_USERNAME, password=SVN_PASSWORD)
        try:
            svn_info = r.info()
            print(f'Last commit: {svn_info["commit_revision"]}')
        except svn.remote.SvnException as e:
            print(e)
            continue
        reponame = line[1]
        repodir = os.path.join(os.path.curdir, 'git_repos/', reponame)

        # shutil.rmtree(repodir, onerror = handler)
        cmd = ['git', 'svn', 'clone', '--stdlayout', f'--authors-file={AUTHORS_FILEPATH}', line[0], repodir]
        rc, output = execute(cmd)
        if rc != 0:
            print(f'Error: {rc}\n{output}')
            continue

        repo = Repo(repodir)
        for ref in repo.refs:
            if 'remotes/origin' in ref.path:
                remote: RemoteReference = ref
                print(f'Remote branch: {remote.path}')
                remote_path = remote.path
                path_split = remote.path.split('/')
                branch_name = path_split[-1]
                if path_split[-2] == 'tags':
                    print(f'Tag: {branch_name}')
                    branch_name = 'tags/' + branch_name
                    if branch_name in repo.tags:
                        print(f'Tag already exists: {branch_name}')
                        continue
                    repo.create_tag(branch_name, ref=ref, message='Tag created by svn_to_git.py')
                    continue
                if branch_name in repo.refs:
                    print(f'Ignoring branch {branch_name} because already exists')
                    continue
                if branch_name == 'trunk':
                    print(f'Ignoring trunk because is already the branch master')
                    continue
                # remote.rename(branch_name)
                repo.create_head(branch_name, ref)
        
        if not 'main' in repo.branches and 'master' in repo.branches:
            print(f'Creating branch main')
            repo.branches.master.rename('main')

        if not 'origin' in repo.remotes:
            print(f'Creating remote url')
            repo.create_remote('origin', REMOTE_URL_BASE + reponame + '.git')
        elif not REMOTE_URL_BASE in repo.remotes.origin.url:
            print(f'Updating remote url')
            repo.remotes.origin.set_url(REMOTE_URL_BASE + reponame + '.git')
        
        rc, output = execute(['git', '-C', repodir, 'push', 'origin', '--all'])
        if rc != 0:
            print(f'Error: {rc}\n{output}')
            continue
        rc, output = execute(['git', '-C', repodir, 'push', 'origin', '--tags'])
        if rc != 0:
            print(f'Error: {rc}\n{output}')
            continue
                

