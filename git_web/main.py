import os
import secrets
import shutil

from git_interface.branch import get_branches
from git_interface.exceptions import (NoBranchesException,
                                      UnknownRevisionException)
from git_interface.log import get_logs
from git_interface.ls import ls_tree
from git_interface.utils import (ArchiveTypes, get_archive, get_description,
                                 init_repo, run_maintenance, set_description)
from quart import (Quart, abort, make_response, redirect, render_template,
                   request, url_for)
from quart_auth import (AuthManager, AuthUser, Unauthorized, login_required,
                        login_user, logout_user)

from .helpers import find_repos, get_config, is_allowed_dir

app = Quart(__name__)
auth_manager = AuthManager()


@app.errorhandler(Unauthorized)
async def redirect_to_login(*_):
    return redirect(url_for(".do_login"))


@app.route("/login", methods=["GET", "POST"])
async def do_login():
    if request.method == "POST":
        password = (await request.form).get("password")
        if not password:
            abort(400)
        if not secrets.compare_digest(password, get_config().LOGIN_PASSWORD):
            abort(401)
        login_user(AuthUser("user"), True)
        return redirect(url_for(".directory_list"))

    return await render_template("login.html")


@app.route("/logout")
@login_required
async def do_logout():
    logout_user()
    return redirect(url_for(".do_login"))


@app.route("/")
@login_required
async def directory_list():
    return await render_template(
        "directories.html",
        dir_paths=filter(is_allowed_dir, next(os.walk(get_config().REPOS_PATH))[1])
    )


@app.get("/new")
@login_required
async def get_new_repo():
    return await render_template(
        "create-repo.html",
        dir_paths=filter(is_allowed_dir, next(os.walk(get_config().REPOS_PATH))[1]),
    )


@app.post("/new")
@login_required
async def post_new_repo():
    try:
        form = await request.form
        name = form["name"]
        directory = form["directory"]
        description = form.get("description")

        if description == "":
            description = None

        if name == "" or directory == "":
            abort(400, "repo name/directory cannot be blank")

        full_path = get_config().REPOS_PATH / directory
        name = name.strip().replace(" ", "-")
        full_repo_path = full_path / (name + ".git")

        if not full_path.exists():
            abort(400, "directory does not exist")
        if full_repo_path.exists():
            abort(400, "repo name already exists")
    except KeyError:
        abort(400, "missing required values")
    except ValueError:
        abort(400, "invalid values in form given")

    init_repo(
        full_path,
        name,
        True,
        get_config().DEFAULT_BRANCH
    )
    if description is not None:
        set_description(full_repo_path, description)
    return redirect(url_for(".repo_view", repo_dir=directory, repo_name=name))


@app.get("/new-dir")
@login_required
async def get_new_dir():
    return await render_template("create-dir.html")


@app.post("/new-dir")
@login_required
async def post_new_dir():
    repo_dir = (await request.form).get("name")

    if not repo_dir:
        abort(400)
    repo_dir = repo_dir.strip().replace(" ", "-")
    full_path = get_config().REPOS_PATH / repo_dir

    if full_path.exists():
        abort(400, "already exists")

    full_path.mkdir()

    return redirect(url_for(".repo_list", directory=repo_dir))


@app.get("/<directory>/delete")
@login_required
async def get_dir_delete(directory: str):
    full_path = get_config().REPOS_PATH / directory
    if not full_path.exists():
        abort(400, "directory does not exist")
    try:
        next(full_path.iterdir())
    except StopIteration:
        full_path.rmdir()
    else:
        abort(400, "directory not empty")
    return redirect(url_for(".directory_list"))


@app.route("/<directory>/repos")
@login_required
async def repo_list(directory):
    repo_paths = find_repos(get_config().REPOS_PATH / directory, True)
    return await render_template(
        "repos.html",
        directory=directory,
        repo_paths=repo_paths
    )


@app.route("/<repo_dir>/repos/<repo_name>/", defaults={"branch": None})
@app.route("/<repo_dir>/repos/<repo_name>/tree/<branch>")
@login_required
async def repo_view(repo_dir: str, repo_name: str, branch: str):
    repo_path = get_config().REPOS_PATH / repo_dir / (repo_name + ".git")
    if not repo_path.exists():
        abort(404)

    ssh_url = get_config().REPOS_SSH_BASE + ":" +\
         str(repo_path.relative_to(get_config().REPOS_PATH))

    head = None
    branches = None
    root_tree = None

    try:
        head, branches = get_branches(repo_path)
    except NoBranchesException:
        pass
    else:
        if branch is None:
            branch = head
        elif branch not in branches:
            if head != branch:
                abort(404)

        branches = list(branches)
        branches.append(head)

        root_tree = ls_tree(repo_path, branch, False, False)

    return await render_template(
        "repository.html",
        repo_dir=repo_dir,
        repo_name=repo_name,
        curr_branch=branch,
        head=head,
        branches=branches,
        ssh_url=ssh_url,
        repo_description=get_description(repo_path),
        root_tree=root_tree,
    )


@app.route("/<repo_dir>/repos/<repo_name>/delete", methods=["GET"])
@login_required
async def repo_delete(repo_dir: str, repo_name: str):
    repo_path = get_config().REPOS_PATH / repo_dir / (repo_name + ".git")
    if not repo_path.exists():
        abort(404)
    shutil.rmtree(repo_path)
    return redirect(url_for(".repo_list", directory=repo_dir))


@app.route("/<repo_dir>/repos/<repo_name>/set-description", methods=["POST"])
@login_required
async def repo_set_description(repo_dir: str, repo_name: str):
    repo_path = get_config().REPOS_PATH / repo_dir / (repo_name + ".git")
    if not repo_path.exists():
        abort(404)

    new_description = (await request.form).get("repo-description")
    if not new_description:
        abort(400)

    set_description(repo_path, new_description)

    return redirect(url_for(".repo_view", repo_dir=repo_dir, repo_name=repo_name))


@app.route("/<repo_dir>/repos/<repo_name>/set-name", methods=["POST"])
@login_required
async def repo_set_name(repo_dir: str, repo_name: str):
    repo_path = get_config().REPOS_PATH / repo_dir / (repo_name + ".git")
    if not repo_path.exists():
        abort(404)

    new_name = (await request.form).get("repo-name")
    if not new_name:
        abort(400)
    new_name = new_name.strip().replace(" ", "-")
    repo_path.rename(get_config().REPOS_PATH / repo_dir / (new_name + ".git"))

    return redirect(url_for(".repo_view", repo_dir=repo_dir, repo_name=new_name))


@app.route("/<repo_dir>/repos/<repo_name>/maintenance")
@login_required
async def repo_maintenance_run(repo_dir: str, repo_name: str):
    repo_path = get_config().REPOS_PATH / repo_dir / (repo_name + ".git")
    if not repo_path.exists():
        abort(404)
    run_maintenance(repo_path)
    return redirect(url_for(".repo_view", repo_dir=repo_dir, repo_name=repo_name))


@app.route("/<repo_dir>/repos/<repo_name>/commits/<branch>")
@login_required
async def repo_commit_log(repo_dir: str, repo_name: str, branch: str):
    try:
        repo_path = get_config().REPOS_PATH / repo_dir / (repo_name + ".git")
        if not repo_path.exists():
            abort(404)

        head = None
        branches = None

        try:
            head, branches = get_branches(repo_path)
        except NoBranchesException:
            pass
        else:
            if branch is None:
                branch = head
            elif branch not in branches:
                if head != branch:
                    abort(404)

            branches = list(branches)
            branches.append(head)

        logs = get_logs(repo_path, branch)
        return await render_template(
            "commit_log.html",
            logs=logs,
            curr_branch=branch,
            branches=branches,
            head=head,
            repo_dir=repo_dir,
            repo_name=repo_name
        )
    except UnknownRevisionException:
        abort(404)


@app.route("/<repo_dir>/repos/<repo_name>/archive.<archive_type>")
@login_required
async def repo_archive(repo_dir: str, repo_name: str, archive_type: str):
    try:
        ArchiveTypes(archive_type)
    except ValueError:
        abort(404)

    repo_path = get_config().REPOS_PATH / repo_dir / (repo_name + ".git")
    if not repo_path.exists():
        abort(404)

    content = get_archive(repo_path, archive_type)
    response = await make_response(content)
    response.mimetype = "application/" + archive_type
    return response


def create_app() -> Quart:
    # load config
    get_config()
    app.secret_key = get_config().SECRET_KEY
    # this is allowing us to run through a proxy
    app.config["QUART_AUTH_COOKIE_SECURE"] = False

    auth_manager.init_app(app)
    return app
