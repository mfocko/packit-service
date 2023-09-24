# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

from logging import getLogger
from os import getenv
from typing import Dict, Optional

from ogr.abstract import Comment, GitProject
from ogr.parsing import RepoUrl
from packit.config import PackageConfig
from packit.constants import DISTGIT_INSTANCES
from packit.utils import nested_get

from packit_service.config import PackageConfigGetter, ServiceConfig
from packit_service.service.db_project_events import (
    AddBranchPushEventToDb,
    AddPullRequestEventToDb,
)
from packit_service.utils import get_packit_commands_from_comment
from packit_service.worker.events.enums import (
    PullRequestAction,
    PullRequestCommentAction,
)
from packit_service.worker.events.comment import AbstractPRCommentEvent
from packit_service.worker.events.event import AbstractForgeIndependentEvent

logger = getLogger(__name__)


class AbstractPagureEvent(AbstractForgeIndependentEvent):
    def __init__(self, project_url: str, pr_id: Optional[int] = None, **kwargs):
        super().__init__(pr_id=pr_id)
        self.project_url: str = project_url
        self.git_ref: Optional[str] = None  # git ref that can be 'git checkout'-ed
        self.identifier: Optional[
            str
        ] = None  # will be shown to users -- e.g. in logs or in the copr-project name


class PushPagureEvent(AddBranchPushEventToDb, AbstractPagureEvent):
    def __init__(
        self,
        repo_namespace: str,
        repo_name: str,
        git_ref: str,
        project_url: str,
        commit_sha: str,
        committer: str,
    ):
        super().__init__(project_url=project_url)
        self.repo_namespace = repo_namespace
        self.repo_name = repo_name
        self.git_ref = git_ref
        self.commit_sha = commit_sha
        self.identifier = git_ref
        self.committer = committer

    @staticmethod
    def parse(event) -> "Optional[PushPagureEvent]":
        """this corresponds to dist-git event when someone pushes new commits"""
        topic = event.get("topic")
        if topic != "org.fedoraproject.prod.git.receive":
            return None

        logger.info(f"Dist-git commit event, topic: {topic}")

        dg_repo_namespace = nested_get(event, "commit", "namespace")
        dg_repo_name = nested_get(event, "commit", "repo")

        if not (dg_repo_namespace and dg_repo_name):
            logger.warning("No full name of the repository.")
            return None

        dg_branch = nested_get(event, "commit", "branch")
        dg_commit = nested_get(event, "commit", "rev")
        if not (dg_branch and dg_commit):
            logger.warning("Target branch/rev for the new commits is not set.")
            return None

        username = nested_get(event, "commit", "username")

        logger.info(
            f"New commits added to dist-git repo {dg_repo_namespace}/{dg_repo_name},"
            f"rev: {dg_commit}, branch: {dg_branch}"
        )

        dg_base_url = getenv("DISTGIT_URL", DISTGIT_INSTANCES["fedpkg"].url)
        dg_project_url = f"{dg_base_url}{dg_repo_namespace}/{dg_repo_name}"

        return PushPagureEvent(
            repo_namespace=dg_repo_namespace,
            repo_name=dg_repo_name,
            git_ref=dg_branch,
            project_url=dg_project_url,
            commit_sha=dg_commit,
            committer=username,
        )


class PullRequestCommentPagureEvent(AbstractPRCommentEvent, AbstractPagureEvent):
    def __init__(
        self,
        action: PullRequestCommentAction,
        pr_id: int,
        base_repo_namespace: str,
        base_repo_name: str,
        base_repo_owner: str,
        base_ref: Optional[str],
        target_repo: str,
        project_url: str,
        user_login: str,
        comment: str,
        comment_id: int,
        commit_sha: str = "",
        comment_object: Optional[Comment] = None,
    ):
        super().__init__(
            pr_id=pr_id,
            project_url=project_url,
            comment=comment,
            comment_id=comment_id,
            commit_sha=commit_sha,
            comment_object=comment_object,
        )
        self.action = action
        self.base_repo_namespace = base_repo_namespace
        self.base_repo_name = base_repo_name
        self.base_repo_owner = base_repo_owner
        self.base_ref = base_ref
        self.target_repo = target_repo
        self.user_login = user_login
        self.identifier = str(pr_id)
        self.git_ref = None  # pr_id will be used for checkout

        self._repo_url: Optional[RepoUrl] = None

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        d = self.__dict__
        d["repo_name"] = self.repo_name
        d["repo_namespace"] = self.repo_namespace
        result = super().get_dict(d)
        result.pop("_repo_url")
        result["action"] = result["action"].value
        return result

    def get_base_project(self) -> GitProject:
        project = self.project.service.get_project(
            namespace=self.base_repo_namespace,
            repo=self.base_repo_name,
            username=self.base_repo_owner,
            is_fork=False,
        )
        logger.debug(f"Base project: {project} owned by {self.base_repo_owner}")
        return project

    def get_packages_config(self) -> Optional[PackageConfig]:
        comment = self.__dict__["comment"]
        commands = get_packit_commands_from_comment(
            comment, ServiceConfig.get_service_config().comment_command_prefix
        )
        if not commands:
            return super().get_packages_config()
        command = commands[0]
        args = commands[1] if len(commands) > 1 else ""
        if command == "pull-from-upstream" and "--with-pr-config" not in args:
            # when retriggering pull-from-upstream from PR comment
            # take packages config from the downstream default branch
            logger.debug(
                f"Getting packages_config:\n"
                f"\tproject: {self.project}\n"
                f"\tbase_project: {self.base_project}\n"
                f"\treference: {self.base_project.default_branch}\n"
            )
            packages_config = PackageConfigGetter.get_package_config_from_repo(
                base_project=self.base_project,
                project=self.project,
                reference=self.base_project.default_branch,
                pr_id=None,
                fail_when_missing=True,
            )
            return packages_config
        else:
            return super().get_packages_config()

    @property
    def repo_url(self) -> Optional[RepoUrl]:
        if not self._repo_url:
            self._repo_url = RepoUrl.parse(
                self.packages_config.upstream_project_url
                if self.packages_config
                else None
            )
        return self._repo_url

    @property
    def repo_namespace(self) -> Optional[str]:
        return self.repo_url.namespace if self.repo_url else None

    @property
    def repo_name(self) -> Optional[str]:
        return self.repo_url.repo if self.repo_url else None

    @staticmethod
    def parse(event) -> "Optional[PullRequestCommentPagureEvent]":
        if ".pagure.pull-request.comment." not in (topic := event.get("topic", "")):
            return None
        logger.info(f"Pagure PR comment event, topic: {topic}")

        action = PullRequestCommentAction.created.value
        pr_id = event["pullrequest"]["id"]
        pagure_login = event["agent"]
        if pagure_login in {"packit", "packit-stg"}:
            logger.debug("Our own comment.")
            return None

        base_repo_namespace = event["pullrequest"]["project"]["namespace"]
        base_repo_name = event["pullrequest"]["project"]["name"]
        repo_from = event["pullrequest"]["repo_from"]
        base_repo_owner = repo_from["user"]["name"] if repo_from else pagure_login
        target_repo = repo_from["name"] if repo_from else base_repo_name
        https_url = event["pullrequest"]["project"]["full_url"]
        commit_sha = event["pullrequest"]["commit_stop"]

        if "added" in event["topic"]:
            comment = event["pullrequest"]["comments"][-1]["comment"]
            comment_id = event["pullrequest"]["comments"][-1]["id"]
        else:
            raise ValueError(
                f"Unknown comment location in response for {event['topic']}"
            )

        return PullRequestCommentPagureEvent(
            action=PullRequestCommentAction[action],
            pr_id=pr_id,
            base_repo_namespace=base_repo_namespace,
            base_repo_name=base_repo_name,
            base_repo_owner=base_repo_owner,
            base_ref=None,
            target_repo=target_repo,
            project_url=https_url,
            commit_sha=commit_sha,
            user_login=pagure_login,
            comment=comment,
            comment_id=comment_id,
        )


class PullRequestPagureEvent(AddPullRequestEventToDb, AbstractPagureEvent):
    def __init__(
        self,
        action: PullRequestAction,
        pr_id: int,
        base_repo_namespace: str,
        base_repo_name: str,
        base_repo_owner: str,
        base_ref: str,
        target_repo: str,
        project_url: str,
        commit_sha: str,
        user_login: str,
    ):
        super().__init__(project_url=project_url, pr_id=pr_id)
        self.action = action
        self.base_repo_namespace = base_repo_namespace
        self.base_repo_name = base_repo_name
        self.base_repo_owner = base_repo_owner
        self.base_ref = base_ref
        self.target_repo = target_repo
        self.commit_sha = commit_sha
        self.user_login = user_login
        self.identifier = str(pr_id)
        self.git_ref = None  # pr_id will be used for checkout
        self.project_url = project_url

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["action"] = result["action"].value
        return result

    def get_base_project(self) -> GitProject:
        fork = self.project.service.get_project(
            namespace=self.base_repo_namespace,
            repo=self.base_repo_name,
            username=self.base_repo_owner,
            is_fork=True,
        )
        logger.debug(f"Base project: {fork} owned by {self.base_repo_owner}")
        return fork


class PullRequestFlagPagureEvent(AbstractPagureEvent):
    def __init__(
        self,
        username: str,
        comment: str,
        status: str,
        date_updated: int,
        url: str,
        commit_sha: str,
        pr_id: int,
        pr_url: str,
        pr_source_branch: str,
        project_url: str,
        project_name: str,
        project_namespace: str,
    ):
        super().__init__(project_url=project_url, pr_id=pr_id)
        self.username = username
        self.comment = comment
        self.status = status
        self.date_updated = date_updated
        self.url = url
        self.commit_sha = commit_sha
        self.pr_url = pr_url
        self.pr_source_branch = pr_source_branch
        self.project_name = project_name
        self.project_namespace = project_namespace

    @staticmethod
    def parse(event) -> "Optional[PullRequestFlagPagureEvent]":
        """
        Look into the provided event and see if it is Pagure PR Flag added/updated event.
        https://fedora-fedmsg.readthedocs.io/en/latest/topics.html#pagure-pull-request-flag-added
        https://fedora-fedmsg.readthedocs.io/en/latest/topics.html#pagure-pull-request-flag-updated
        """

        if ".pagure.pull-request.flag." not in (topic := event.get("topic", "")):
            return None
        logger.info(f"Pagure PR flag event, topic: {topic}")

        if (flag := event.get("flag")) is None:
            return None
        username = flag.get("username")
        comment = flag.get("comment")
        status = flag.get("status")
        date_updated = int(d) if (d := flag.get("date_updated")) else None
        url = flag.get("url")
        commit_sha = flag.get("commit_hash")

        pr_id: int = nested_get(event, "pullrequest", "id")
        pr_url = nested_get(event, "pullrequest", "full_url")
        pr_source_branch = nested_get(event, "pullrequest", "branch_from")

        project_url = nested_get(event, "pullrequest", "project", "full_url")
        project_name = nested_get(event, "pullrequest", "project", "name")
        project_namespace = nested_get(event, "pullrequest", "project", "namespace")

        return PullRequestFlagPagureEvent(
            username=username,
            comment=comment,
            status=status,
            date_updated=date_updated,
            url=url,
            commit_sha=commit_sha,
            pr_id=pr_id,
            pr_url=pr_url,
            pr_source_branch=pr_source_branch,
            project_url=project_url,
            project_name=project_name,
            project_namespace=project_namespace,
        )
