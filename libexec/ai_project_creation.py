"""Create a private Gitea repository only after an explicit human UI action."""

import re
import urllib.error


def create(name, branch, gitea, checkout, git):
    name = re.sub(r"\s+", "-", name.strip())
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name)
        or ".." in name
        or name.endswith(".git")
    ):
        raise ValueError("Use a project name with letters, digits, dots, underscores or hyphens")
    if branch:
        if not branch.startswith("hal/") or ".." in branch:
            raise ValueError("Use a hal/ development branch")
        git(None, "check-ref-format", "--branch", branch)
    try:
        repository = gitea(
            "/user/repos",
            "POST",
            {
                "name": name,
                "private": True,
                "auto_init": True,
                "default_branch": "main",
                "readme": "Default",
            },
        )
    except urllib.error.HTTPError as error:
        if error.code == 409:
            raise ValueError(
                "Project already exists. Choose Existing Gitea project instead."
            ) from error
        raise ValueError("Gitea project creation failed (HTTP " + str(error.code) + ")") from error
    try:
        workspace = checkout(repository["full_name"], repository["default_branch"], branch)
    except Exception as error:
        raise ValueError(
            "Created "
            + repository["full_name"]
            + ", but checkout failed. Retry using Existing Gitea project. "
            + str(error)
        ) from error
    return dict(workspace, created_project=True, private=True)
