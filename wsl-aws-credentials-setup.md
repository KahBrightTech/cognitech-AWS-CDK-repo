# AWS Credentials Setup on WSL

How AWS CLI authentication is configured on this machine: Ubuntu running under WSL2 on
Windows, signing in through AWS IAM Identity Center (SSO).

Written as a runbook — every command is copy-pasteable, and each section explains *why*
so you can adapt it rather than just replaying it.

---

## Table of contents

1. [Environment](#1-environment)
2. [The two filesystems](#2-the-two-filesystems)
3. [Install the AWS CLI](#3-install-the-aws-cli)
4. [Sharing `.aws` between Windows and WSL](#4-sharing-aws-between-windows-and-wsl)
5. [Opening the browser from WSL](#5-opening-the-browser-from-wsl)
6. [Configuring SSO](#6-configuring-sso)
7. [The profile switcher](#7-the-profile-switcher)
8. [Daily usage](#8-daily-usage)
9. [Why sessions expire after one hour](#9-why-sessions-expire-after-one-hour)
10. [Where credentials live on disk](#10-where-credentials-live-on-disk)
11. [Troubleshooting](#11-troubleshooting)
12. [Security notes](#12-security-notes)
13. [Optional cleanup](#13-optional-cleanup)

---

## 1. Environment

| Component | Version / value |
| --- | --- |
| Host OS | Windows 11 |
| WSL distro | Ubuntu 24.04.3 LTS |
| Kernel | 6.6.87.2-microsoft-standard-WSL2 |
| AWS CLI | `aws-cli/2.37.3`, installed at `/usr/local/bin/aws` |
| Windows user | `Owner` → `C:\Users\Owner` |
| Linux user | `wner` → `/home/wner` |
| Identity provider | AWS IAM Identity Center |
| SSO start URL | `https://d-9067e9e37c.awsapps.com/start/#` |
| SSO region | `us-east-1` |
| Profiles | 26 |

> The two usernames differ by one character — `Owner` vs `wner`. Mixing them up is the
> single most common source of "the file isn't there" confusion. There is no
> `C:\Users\wner`.

---

## 2. The two filesystems

WSL gives you two storage areas with very different performance characteristics.

| Path in Linux | Backed by | Speed | Notes |
| --- | --- | --- | --- |
| `/` , `/home/wner` | `ext4` on a virtual disk | Fast | Native Linux filesystem |
| `/mnt/c` | Windows `C:` via the `9p` protocol | Slow | Every call is translated |

`/home/wner` is **not** a normal Windows folder. It lives inside a virtual hard disk:

```
C:\Users\Owner\AppData\Local\wsl\{ecdfaf1c-8746-4d1d-ac12-38acec3b71e2}\ext4.vhdx
```

Never open, move, or copy that `.vhdx` directly — you would corrupt the filesystem.
To browse it from Windows, use the UNC path instead:

```
\\wsl.localhost\Ubuntu\home\wner
```

**The working rule:** keep files on the same side as the tools that touch them most.
Code you build with Linux tools belongs in `~`; code you build with Windows tools belongs
on `C:`. Crossing the boundary on every file read is what makes things feel sluggish.

---

## 3. Install the AWS CLI

Ubuntu 24.04 **removed** the `awscli` apt package, so this fails:

```bash
sudo apt install awscli
# E: Package 'awscli' has no installation candidate
```

Use the official v2 installer instead:

```bash
sudo apt update
sudo apt install -y unzip curl

curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install

aws --version    # aws-cli/2.37.3 Python/3.14.6 Linux/... 
```

Clean up the installer afterwards:

```bash
rm -rf awscliv2.zip aws/
```

> Check your architecture first with `uname -m`. If it prints `aarch64` rather than
> `x86_64`, swap the URL for `awscli-exe-linux-aarch64.zip`.

To upgrade later, run the same commands with `--update`:

```bash
sudo ./aws/install --update
```

---

## 4. Sharing `.aws` between Windows and WSL

Rather than maintaining two separate credential stores, the Linux `~/.aws` is a **symlink**
to the Windows one:

```bash
ls -ld ~/.aws
# lrwxrwxrwx 1 wner wner 23 ... /home/wner/.aws -> /mnt/c/Users/Owner/.aws
```

To create it yourself (back up any existing Linux-side folder first):

```bash
[ -e ~/.aws ] && [ ! -L ~/.aws ] && mv ~/.aws ~/.aws.linux-backup
ln -s /mnt/c/Users/Owner/.aws ~/.aws
readlink -f ~/.aws    # confirm: /mnt/c/Users/Owner/.aws
```

### What this buys you

`aws sso login` in WSL logs you in on Windows too, and vice versa. One login, both
environments. PowerShell, VS Code's AWS Toolkit, and the WSL CLI all read the same files.

### What it costs you

- **Slower.** Every token read crosses the `9p` boundary.
- **No Unix permissions.** Files on `/mnt/c` report `rwxrwxrwx` no matter what, so
  `chmod 600 ~/.aws/credentials` silently does nothing. Windows NTFS ACLs still restrict
  the folder to your account, so it isn't world-readable — but the usual Linux safety net
  is absent.

### Finding it from Windows

Because `.aws` is a symlink (a *reparse point*), Windows Explorer does **not** show it as
a folder. Browsing to `\\wsl.localhost\Ubuntu\home\wner`, it appears down in the *file*
list with a generic icon, and clicking it fails — the target `/mnt/c/Users/Owner/.aws` is
a Linux path Explorer can't resolve.

Go to the real folder instead:

```
C:\Users\Owner\.aws
```

Or, from WSL, let the shell resolve the link for you:

```bash
explorer.exe "$(wslpath -w "$(readlink -f ~/.aws)")"
```

`readlink -f` follows the symlink; `wslpath -w` converts the result to a Windows path.

---

## 5. Opening the browser from WSL

SSO login needs a browser, and WSL has no GUI. The usual answer is `wslu`, which provides
`wslview` to hand URLs to Windows:

```bash
sudo apt install -y wslu
export BROWSER=wslview
```

### The bug you will hit

On Ubuntu 24.04 (`wslu` 3.2.3) with `systemd=true` in `/etc/wsl.conf`, every SSO login
prints:

```
[error] WSL Interoperability is disabled. Please enable it before using WSL.
```

Interoperability is **not** actually disabled. When systemd is enabled, WSL registers its
Windows-executable handler under a different name:

| File | With systemd |
| --- | --- |
| `/proc/sys/fs/binfmt_misc/WSLInterop` | does not exist |
| `/proc/sys/fs/binfmt_misc/WSLInterop-late` | `enabled` |

`wslview` hardcodes the old name, finds nothing, and errors out. Confirm interop really
works:

```bash
/mnt/c/Windows/System32/cmd.exe /c echo interop-works
```

### The fix used here

A three-line launcher that calls Windows' URL handler directly, skipping `wslu`:

```bash
mkdir -p ~/bin
cat > ~/bin/winbrowser <<'EOF'
#!/usr/bin/env bash
# Open a URL in the default Windows browser from WSL.
[ -z "$1" ] && { echo "usage: winbrowser <url>" >&2; exit 1; }
exec /mnt/c/Windows/System32/rundll32.exe url.dll,FileProtocolHandler "$1"
EOF
chmod +x ~/bin/winbrowser
```

Point `BROWSER` at it in `~/.bashrc`:

```bash
export BROWSER=$HOME/bin/winbrowser
```

Test:

```bash
winbrowser https://example.com
```

### Alternative: patch `wslview`

If you want the rest of the `wslu` tools working too:

```bash
sudo sed -i 's#binfmt_misc/WSLInterop #binfmt_misc/WSLInterop* #g' \
  /usr/bin/wslview /usr/bin/wslsys /usr/share/wslu/* 2>/dev/null
```

The `*` makes the glob match `WSLInterop-late` as well as the legacy name.

> **Don't** "fix" this by removing `systemd=true` from `/etc/wsl.conf`. Docker and other
> services depend on systemd.

### A note about `~/bin` and PATH

Ubuntu adds `~/bin` to `PATH` from `~/.profile`, which runs **only for login shells**. So
`source ~/.bashrc` will *not* pick up a newly created `~/bin`, and you'll get
`winbrowser: command not found`. Put the logic in `.bashrc` directly:

```bash
case ":$PATH:" in *":$HOME/bin:"*) ;; *) export PATH="$HOME/bin:$PATH" ;; esac
```

The `case` guard prevents a duplicate entry every time you re-source the file.

---

## 6. Configuring SSO

```bash
aws configure sso
```

You'll be prompted for:

| Prompt | Value |
| --- | --- |
| SSO session name | a label you choose, e.g. `prod` |
| SSO start URL | `https://d-9067e9e37c.awsapps.com/start/#` |
| SSO region | `us-east-1` |
| SSO registration scopes | `sso:account:access` (accept the default) |

The browser opens, you approve the device, then you pick an account and role. The CLI
writes two blocks to `~/.aws/config`:

```ini
[sso-session admin-vap]
sso_start_url = https://d-9067e9e37c.awsapps.com/start/#
sso_region = us-east-1
sso_registration_scopes = sso:account:access

[profile admin-vap]
sso_session = admin-vap
sso_account_id = 882680178335
sso_role_name = AdministratorAccess
region = us-east-1
output = json
```

**The `sso-session` block is the login.** The `profile` block is one account-and-role pair
that uses that login. Many profiles can — and normally *should* — share a single
`sso-session`.

If the browser doesn't open, fall back to the code-based flow:

```bash
aws sso login --profile admin-vap --use-device-code
```

It prints a URL and a short code. Open the URL on any device, enter the code, approve.

---

## 7. The profile switcher

Appended to `~/.bashrc`. Requires `fzf`:

```bash
sudo apt install -y fzf
```

```bash
# ---------- AWS profile management ----------
export BROWSER=$HOME/bin/winbrowser   # hand URLs to the Windows browser
case ":$PATH:" in *":$HOME/bin:"*) ;; *) export PATH="$HOME/bin:$PATH" ;; esac

awsuse() {
  export AWS_PROFILE="$1"
  if ! aws sts get-caller-identity >/dev/null 2>&1; then
    echo "Session expired - logging in..."
    aws sso login --profile "$1" >/dev/null 2>&1
  fi
  iam
}

_awspick() {
  local p
  p=$(aws configure list-profiles | grep -E "$1" | sort | fzf --height 50% --reverse --prompt="$2> ")
  [ -n "$p" ] && awsuse "$p"
}

profiles() { _awspick "." "AWS"; }
awsadmin() { _awspick "^admin-" "admin"; }
awsro()    { _awspick "^readonly-" "readonly"; }

iam() {
  local acct arn rest prof key cache exp secs sso=0
  echo "Profile : ${AWS_PROFILE:-<none set - using [default]>}"
  echo "Region  : ${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || echo '<none>')}}"

  if ! read -r acct arn < <(aws sts get-caller-identity --query '[Account,Arn]' --output text 2>/dev/null); then
    echo "Status  : NOT logged in  ->  run: profiles"
    return 1
  fi
  echo "Account : $acct"

  case "$arn" in
    *:assumed-role/*)
      sso=1
      rest=${arn#*assumed-role/}
      echo "Role    : ${rest%%/*}"
      echo "Session : ${rest#*/}"
      ;;
    *:user/*)
      echo "Type    : IAM user (long-lived access keys)"
      echo "User    : ${arn##*/}"
      ;;
    *)
      echo "Identity: ${arn##*/}"
      ;;
  esac
  echo "Arn     : $arn"

  [ "$sso" -eq 1 ] || return 0

  # The CLI names the token cache sha1(sso_session), or sha1(sso_start_url) on
  # legacy configs. grep -o (not sed) so the pattern cannot slide into
  # "registrationExpiresAt", which is a separate 90-day field.
  prof=${AWS_PROFILE:-default}
  key=$(aws configure get sso_session --profile "$prof" 2>/dev/null) ||
    key=$(aws configure get sso_start_url --profile "$prof" 2>/dev/null)
  [ -n "$key" ] || return 0
  cache=~/.aws/sso/cache/$(printf %s "$key" | sha1sum | cut -d' ' -f1).json
  [ -f "$cache" ] || return 0

  exp=$(grep -oE '"expiresAt"[[:space:]]*:[[:space:]]*"[^"]*"' "$cache" | head -1 | cut -d'"' -f4)
  [ -n "$exp" ] || return 0

  secs=$(( $(date -d "${exp/UTC/Z}" +%s 2>/dev/null || echo 0) - $(date +%s) ))
  if [ "$secs" -gt 0 ]; then
    printf 'Expires : %s (in %dh %dm)\n' "$(date -d "${exp/UTC/Z}" '+%H:%M %Z')" $((secs/3600)) $((secs%3600/60))
  else
    echo "Expires : EXPIRED -> run: profiles"
  fi
}

awsclear() { unset AWS_PROFILE; echo "Profile cleared"; }

alias awsp='profiles'
alias awswho='iam'
alias me='iam'
alias prod='awsuse prod-admin'
alias mgmt='awsuse admin-mgmt'

export PS1='${AWS_PROFILE:+\[\e[33m\]($AWS_PROFILE)\[\e[0m\] }'"$PS1"
# ---------- end AWS profile management ----------
```

Back up before editing, and validate afterwards:

```bash
cp ~/.bashrc ~/.bashrc.bak.$(date +%Y%m%d%H%M%S)
# ...edit...
bash -n ~/.bashrc && echo "syntax OK"
```

`bash -n` parses without executing. A syntax error in `.bashrc` can leave you with a shell
that won't start properly, so always check.

Open a **new** terminal to pick up `PATH` changes — `source ~/.bashrc` is not enough.

---

## 8. Daily usage

| Command | What it does |
| --- | --- |
| `profiles` | Fuzzy-pick from all profiles, switch, log in if needed |
| `awsp` | Alias for `profiles` |
| `awsadmin` | Pick from `admin-*` profiles only |
| `awsro` | Pick from `readonly-*` profiles only |
| `iam` | Show current identity and session expiry |
| `me` / `awswho` | Aliases for `iam` |
| `awsclear` | Unset `AWS_PROFILE` |
| `prod` | Jump straight to `prod-admin` |
| `mgmt` | Jump straight to `admin-mgmt` |

`iam` was chosen because `id` and `whoami` are real Linux commands — shadowing them would
break scripts.

The active profile shows in the prompt in yellow:

```
(admin-vap) wner@DESKTOP-KGBORH3:~$
```

Sample `iam` output:

```
Profile : admin-vap
Region  : us-east-1
Account : 882680178335
Role    : AWSReservedSSO_AdministratorAccess_2359829de747c377
Session : Brigthain
Arn     : arn:aws:sts::882680178335:assumed-role/AWSReservedSSO_.../Brigthain
Expires : 11:00 EDT (in 0h 52m)
```

### `AWS_PROFILE` is per-terminal

It's an environment variable, so each tab has its own. That's a feature — run a read-only
profile in one tab and an admin profile in another without risk of crossing them.

---

## 9. Why sessions expire after one hour

Look at the token cache for a session:

```bash
f=~/.aws/sso/cache/$(printf %s "admin-vap" | sha1sum | cut -d' ' -f1).json
date -r "$f" -u +"issued:  %Y-%m-%dT%H:%M:%SZ"
grep -oE '"expiresAt"[[:space:]]*:[[:space:]]*"[^"]*"' "$f"
```

```
issued:  2026-09-25T14:00:07Z
"expiresAt": "2026-09-25T15:00:07Z"
```

Exactly 60 minutes. That number is **not** an AWS CLI default and **not** something you
can change locally.

### Three different clocks

People conflate these constantly. They are separate settings:

| # | Clock | Controlled by | Typical | Stored in |
| --- | --- | --- | --- | --- |
| 1 | **SSO access token** | Identity Center *session duration* | 1 h here | `~/.aws/sso/cache/<sha1>.json` → `expiresAt` |
| 2 | **Role credentials** | Permission set *session duration* (1–12 h) | 1 h default | Held in memory / `~/.aws/cli/cache/` |
| 3 | **Client registration** | Fixed by AWS | 90 days | same file → `registrationExpiresAt` |

The one-hour figure you see is **#1**. Your Identity Center administrator set the session
duration to the minimum. In the console it lives under
*IAM Identity Center → Settings → Authentication → Session settings*, and can be anywhere
from 15 minutes to 90 days. Unless you administer that instance, you cannot change it.

Number 3 is why an earlier version of the `iam` function reported an absurd
"in 2159h" — it matched `registrationExpiresAt` (90 days out) instead of `expiresAt`.
Both fields live in the same JSON file, which makes this an easy mistake:

```json
{
  "expiresAt": "2026-09-25T15:00:07Z",
  "registrationExpiresAt": "2026-12-24T13:59:58Z"
}
```

### You are not actually re-authenticating every hour

Check which keys the cache holds:

```bash
grep -oE '"[a-zA-Z]+"[[:space:]]*:' "$f" | tr -d '": '
```

```
startUrl  region  accessToken  expiresAt
clientId  clientSecret  registrationExpiresAt  refreshToken
```

That last one matters. Because the config uses `sso-session` blocks with
`sso_registration_scopes = sso:account:access`, AWS issues a **refresh token**. When the
access token expires, the CLI trades the refresh token for a new one silently — no
browser, no prompt.

So in practice:

- Expiry passes → next `aws` command refreshes transparently.
- You only see a real login prompt when the *overall* SSO session maximum is reached, or
  after 90 days when the client registration lapses.

The `Expires` line in `iam` is therefore a **heads-up, not a countdown to a lockout**. If
you had *no* refresh token — the old `sso_start_url`-on-the-profile style of config — you
would genuinely re-authenticate every hour. That's a good reason to keep using
`sso-session` blocks.

### Forcing a fresh login

```bash
aws sso logout                        # clear cached tokens
aws sso login --profile admin-vap     # start over
```

---

## 10. Where credentials live on disk

```
C:\Users\Owner\.aws\          ( = /home/wner/.aws via symlink)
├── config                    profiles + sso-session blocks
├── credentials               long-lived IAM access keys
├── sso/cache/*.json          SSO access + refresh tokens
├── cli/cache/*.json          temporary assumed-role credentials
└── amazonq/                  Amazon Q settings
```

| File | Sensitivity | Notes |
| --- | --- | --- |
| `config` | Low | No secrets — safe to review or copy |
| `credentials` | **High** | Plaintext long-lived keys |
| `sso/cache/*.json` | **High** | Bearer tokens; short-lived but fully usable |
| `cli/cache/*.json` | **High** | Temporary role credentials |

Inspect config safely:

```bash
aws configure list-profiles          # names only
aws configure get region --profile admin-vap
cat ~/.aws/config                    # no secrets in this file
```

Never `cat ~/.aws/credentials` or the cache JSON into a terminal you're screen-sharing,
and never paste them into a chat, an issue, or a commit.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Package 'awscli' has no installation candidate` | Removed in Ubuntu 24.04 | Use the official v2 zip ([§3](#3-install-the-aws-cli)) |
| `[error] WSL Interoperability is disabled` | `wslu` checks `WSLInterop`; systemd creates `WSLInterop-late` | Use `winbrowser`, or patch `wslview` ([§5](#5-opening-the-browser-from-wsl)) |
| `xdg-open: no method available` | No GUI browser in WSL | Set `BROWSER` ([§5](#5-opening-the-browser-from-wsl)) |
| `winbrowser: command not found` | `~/bin` not on `PATH` | Add the `case` guard to `.bashrc`, open a new terminal |
| Browser never opens during login | Bridge not working | `aws sso login --use-device-code` |
| `Unable to locate credentials` | No profile selected | `profiles`, or `export AWS_PROFILE=...` |
| `ExpiredToken` / `InvalidGrantException` | Refresh token also expired | `aws sso logout && aws sso login --profile X` |
| Expiry shows a wild number like `2159h` | Read `registrationExpiresAt` by mistake | Match `expiresAt` with `grep -o` ([§9](#9-why-sessions-expire-after-one-hour)) |
| `.aws` invisible in Explorer | It's a symlink, not a directory | Open `C:\Users\Owner\.aws` |
| Wrong account in `iam` | Stale `AWS_PROFILE` in that tab | `awsclear`, then `profiles` |

Useful checks:

```bash
aws sts get-caller-identity              # who am I right now
aws configure list                       # where each setting came from
aws configure list-profiles              # all profile names
echo "$AWS_PROFILE"                      # active profile in this shell
bash -n ~/.bashrc                        # validate shell config
```

`aws configure list` is the one to reach for when a value isn't what you expect — it shows
the *source* of each setting (env var, config file, or command line), which immediately
reveals precedence problems.

---

## 12. Security notes

### Prefer SSO over long-lived keys

`~/.aws/credentials` on this machine holds access keys for an IAM user. Those keys do not
expire and are the most common source of leaked AWS credentials. SSO already works here,
so the keys are redundant. Check when they were last used:

```bash
aws iam list-access-keys --user-name <name>
aws iam get-access-key-last-used --access-key-id AKIA...
```

If unused, delete them in the IAM console. If needed, at minimum rotate them on a
schedule.

### Permissions don't apply on `/mnt/c`

`chmod 600 ~/.aws/credentials` appears to succeed but changes nothing — DrvFs synthesises
`rwxrwxrwx`. Protection comes from Windows NTFS ACLs on `C:\Users\Owner` instead. Worth
knowing if you assume the Linux permission model is protecting you here.

### Don't commit credentials

The repo `.gitignore` covers build artefacts, not dotfiles in your home directory. Before
committing anything AWS-related:

```bash
git diff --cached | grep -iE 'aws_secret|AKIA|aws_session_token'
```

### Prefer read-only when you can

`awsro` exists for a reason. Defaulting to `readonly-*` for investigation and switching to
`admin-*` only when you intend to change something is a cheap habit that prevents
expensive mistakes.

---

## 13. Optional cleanup

Current state: **26 profiles, 19 `sso-session` blocks, 36 cached token files.**

Every profile defines its own `sso-session`, even though all of them point at the same
start URL:

```ini
[sso-session admin-vap]
sso_start_url = https://d-9067e9e37c.awsapps.com/start/#
sso_region = us-east-1

[sso-session admin-mgmt]
sso_start_url = https://d-9067e9e37c.awsapps.com/start/#     # identical
sso_region = us-east-1
```

Because the cache filename is `sha1(sso_session_name)`, each one gets a **separate token**
— which is why there are 36 cache files, and why switching profiles often triggers a fresh
login.

Consolidating to a single shared session means **one login covers every account**:

```ini
[sso-session cognitech]
sso_start_url = https://d-9067e9e37c.awsapps.com/start/#
sso_region = us-east-1
sso_registration_scopes = sso:account:access

[profile admin-vap]
sso_session = cognitech
sso_account_id = 882680178335
sso_role_name = AdministratorAccess
region = us-east-1
output = json

[profile readonly-vap]
sso_session = cognitech
sso_account_id = 882680178335
sso_role_name = ReadOnlyAccess
region = us-east-1
output = json
```

Back up first — this rewrites your whole config:

```bash
cp ~/.aws/config ~/.aws/config.bak.$(date +%Y%m%d)
```

Then clear the stale token cache (it regenerates on next login):

```bash
aws sso logout
rm -f ~/.aws/sso/cache/*.json
aws sso login --profile admin-vap
```

There are also duplicate profile names differing only in case — `QA`/`qa`,
`Devops`/`devops` — worth resolving at the same time.

---

## Quick reference

```bash
# Switch profile (interactive)
profiles

# Who am I / when does this expire
iam

# Targeted pickers
awsadmin
awsro

# Manual control
export AWS_PROFILE=admin-vap
aws sso login --profile admin-vap
aws sso login --profile admin-vap --use-device-code
aws sso logout
awsclear

# Inspect
aws configure list
aws configure list-profiles
aws sts get-caller-identity

# Open the real .aws folder in Explorer
explorer.exe "$(wslpath -w "$(readlink -f ~/.aws)")"
```
