# svn-to-git
A Python tool for migrate SVN repositories to Git

## How to use


```shell
# Install dependencies
pipenv install
# Run program
pipenv run python './svn_to_git.py' '--svn-username' 'svnuser' '--svn-password' 'svnpass' '--git-base-url' 'https://gitlab.com/git-user' '--migrate-from-copy'
```