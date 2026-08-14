"""Command validation for the terminal tool.

Classification:
- BLOCKED           : unconditionally dangerous / malicious patterns.
- CONFIRM_REQUIRED  : mutating, system-level, network-mutating or risky commands.
- SAFE_ACTION       : read-only / harmless commands.

The permission layer is authoritative: the model can never bypass it.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass

from .levels import PermissionLevel

# Commands that are always blocked, no matter the arguments.
ALWAYS_BLOCKED = {
    "mkfs", "mkfs.ext4", "mkfs.xfs", "fdisk", "parted", "dd", "shutdown", "reboot", "halt",
    "poweroff", "init", "telinit", "rmmod", "insmod", "modprobe", "kexec", "swapoff",
}
# Borderline system commands → confirmation.
MUTATING_COMMANDS = {
    "rm", "mv", "cp", "install", "apt", "apt-get", "aptitude", "dnf", "yum", "pacman", "zypper",
    "snap", "flatpak", "pip", "pip3", "pipx", "npm", "npx", "yarn", "pnpm", "cargo", "go", "gem",
    "brew", "docker", "podman", "kubectl", "helm", "systemctl", "service", "journalctl",
    "chmod", "chown", "chgrp", "usermod", "useradd", "userdel", "groupadd", "passwd", "su",
    "sudo", "visudo", "iptables", "nft", "firewall-cmd", "ufw", "mount", "umount", "swapon",
    "git", "svn", "hg", "make", "cmake", "meson", "gcc", "g++", "clang", "rustc", "go build",
    "kill", "pkill", "killall", "pkill -9", "sleep", "touch", "truncate", "tee", "unlink",
    "ln", "mktemp", "watch", "xargs", "eval", "bash", "sh", "zsh", "python", "python3",
    "perl", "ruby", "node", "wget", "curl", "ssh", "scp", "rsync", "ftp", "sftp", "telnet",
    "nc", "ncat", "netcat", "socat", "openssl", "gpg", "cryptsetup", "grub-install", "update-grub",
    "timedatectl", "localectl", "hostnamectl", "sysctl", "crond", "crontab", "at", "nice",
    "renice", "nohup", "disown", "unshare", "nsenter", "setcap", "chroot",
}

# Commands that are safe to run read-only.
SAFE_COMMANDS = {
    "ls", "ll", "dir", "pwd", "whoami", "id", "date", "cal", "echo", "printf", "cat", "less",
    "head", "tail", "grep", "rg", "find", "locate", "which", "whereis", "file", "stat", "du",
    "df", "free", "ps", "top", "htop", "uname", "hostname", "uptime", "env", "printenv",
    "true", "false", "history", "tree", "wc", "sort", "uniq", "cut", "tr", "od", "xxd",
    "hexdump", "diff", "cmp", "git status", "git log", "git diff", "git branch", "git remote",
    "git stash list", "sqlite3", "ip", "ifconfig", "ss", "netstat", "ping", "dig", "nslookup",
    "getent", "host", "finger", "w", "last", "lsusb", "lspci", "lsblk", "blkid", "dmesg",
    "sysctl -a", "cat /proc", "python -c", "python3 -c",
}

DENYLIST_REGEXES = [
    re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|/\*|\.\s)"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;"),
    re.compile(r">\s*/dev/sd"),
    re.compile(r"\b(echo|printf)\s+[^|]*\|[^|]*(bash|sh|zsh)\b"),
    re.compile(r"\bcurl[^|]*\|[^|]*(bash|sh)\b"),
    re.compile(r"\bwget[^|]*\|\s*(bash|sh)\b"),
    re.compile(r"\bchmod\s+(-R\s+)?777\s+/"),
    re.compile(r"\brm\s+-rf\s+(/|\.|~)\b"),
]

REDIRECTION_BLOCKED_DIRS = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/boot", "/var/lib")


@dataclass
class CommandVerdict:
    allowed: bool
    level: PermissionLevel
    reason: str


def validate_command(command: str, cwd: str | None = None) -> CommandVerdict:
    """Classify a shell command into a permission verdict."""
    if not command or not command.strip():
        return CommandVerdict(False, PermissionLevel.BLOCKED, "empty command")
    stripped = command.strip()
    for rx in DENYLIST_REGEXES:
        if rx.search(stripped):
            return CommandVerdict(False, PermissionLevel.BLOCKED, f"blocked pattern: {rx.pattern[:60]}")
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return CommandVerdict(False, PermissionLevel.BLOCKED, "unparseable command (unbalanced quotes)")
    if not tokens:
        return CommandVerdict(False, PermissionLevel.BLOCKED, "empty command")

    # Prevent secret leakage into the command line.
    lowered = stripped.lower()
    for secret_marker in ("api_key", "apikey", "bearer ", "nvapi-", "sk-"):
        if secret_marker in lowered and any(tok in lowered for tok in ("echo", "print", "curl -H", "-H ")):
            return CommandVerdict(False, PermissionLevel.BLOCKED, "command may leak secrets")

    first = tokens[0]
    joined_first = " ".join(tokens[:2])
    if first in ALWAYS_BLOCKED:
        return CommandVerdict(False, PermissionLevel.BLOCKED, f"command '{first}' is always blocked")

    # Redirection targets (writes) are checked BEFORE the safe-command shortcut
    # so `echo x > /etc/hosts` can never slip through as read-only.
    redir_verdict = _check_redirections(stripped, cwd)
    if redir_verdict is not None:
        return redir_verdict

    if joined_first in SAFE_COMMANDS or first in SAFE_COMMANDS:
        return CommandVerdict(True, PermissionLevel.SAFE_ACTION, "read-only command")

    if first in MUTATING_COMMANDS or joined_first in MUTATING_COMMANDS:
        return CommandVerdict(True, PermissionLevel.CONFIRM_REQUIRED,
                              f"mutating/system command: {first}")
    return CommandVerdict(True, PermissionLevel.SAFE_ACTION, "unclassified command (treated as safe)")


def _check_redirections(command: str, cwd: str | None) -> CommandVerdict | None:
    """Writes via >, >>, tee to protected/system paths → confirmation."""
    targets: list[str] = []
    for m in re.finditer(r"(?:>>|>|2>)\s*([^\s|&;]+)", command):
        targets.append(m.group(1))
    if not targets:
        return None
    for t in targets:
        expanded = os.path.abspath(os.path.expanduser(t) if not os.path.isabs(t) else t)
        base = os.path.basename(expanded)
        if expanded.startswith(REDIRECTION_BLOCKED_DIRS):
            return CommandVerdict(False, PermissionLevel.BLOCKED,
                                  f"refusing to write to protected path: {t}")
        if not (expanded.startswith("/tmp") or expanded.startswith("/var/tmp")
                or (cwd and expanded.startswith(os.path.abspath(cwd)))
                or expanded.startswith(os.path.expanduser("~"))):
            return CommandVerdict(True, PermissionLevel.CONFIRM_REQUIRED,
                                  f"writing to {t} outside workspace/home")
    return None
