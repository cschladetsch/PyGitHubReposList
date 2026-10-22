# Github Repos

A small Python script that lists all of a GitHub user's repositories, sorted by stars, views and other metrics.

![Demo](Resources/Demo.bmp)

## Features

- Lists every repo owned by a user (including private repos when authenticated as that user)
- Shows stars, forks, open issues, views, unique views, clones, language, last push date, and fork/private flags
- Sorts by stars, views, unique views, clones, forks, issues, last updated, or name
- Fetches traffic data in parallel
- Optional CSV export

## Requirements

- Python 3.8+
- [`requests`](https://pypi.org/project/requests/)

```
pip install requests
```

## Authentication

Stars, forks and issues are public and need no token. Views and clones come from GitHub's traffic API, which requires:

- A token in the `GITHUB_TOKEN` environment variable (classic PAT with `repo` scope, or fine-grained with read access to repository administration)
- Push access to the repos (in practice, you must be the owner)

Traffic data only covers the last 14 days. Without a token the script still runs and skips views and clones.

```
export GITHUB_TOKEN=ghp_xxxxxxxx          # bash / zsh
$env:GITHUB_TOKEN = "ghp_xxxxxxxx"        # PowerShell
```

## Usage

```
python github_repos.py                        # default user, sorted by stars then views
python github_repos.py --sort views           # sort by 14-day views
python github_repos.py --no-traffic           # skip traffic calls, no token needed
python github_repos.py --no-forks --top 50    # top 50 non-fork repos
python github_repos.py --csv repos.csv        # also write a CSV
python github_repos.py --user someoneelse     # a different account
```

## Options

| Option         | Description                                                                          |
| -------------- | ------------------------------------------------------------------------------------ |
| `--user`       | GitHub username (default: `cschladetsch`)                                            |
| `--sort`       | `stars`, `views`, `uniques`, `clones`, `forks`, `issues`, `updated`, `name`          |
| `--no-traffic` | Skip views and clones                                                                |
| `--no-forks`   | Exclude forked repos                                                                 |
| `--top N`      | Show only the first N rows                                                           |
| `--csv FILE`   | Write the results to a CSV file                                                      |

Ties are broken by stars, then views, so the ordering is stable.

## Output

A plain-text table followed by a totals line:

```
name        stars  forks  issues  views  uniques  clones  language  updated     fork  private
----------  -----  -----  ------  -----  -------  ------  --------  ----------  ----  -------
...

282 repos, 1234 stars, 5678 views (last 14 days)
```

## License

MIT
