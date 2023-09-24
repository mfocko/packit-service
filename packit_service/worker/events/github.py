# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import logging
from typing import Dict, Optional, Tuple, Union, List, Set

from ogr.abstract import GitProject, Comment
from packit.config import JobConfigTriggerType
from packit.utils import nested_get

from packit_service.config import ServiceConfig, Deployment
from packit_service.models import (
    AllowlistStatus,
    GitBranchModel,
    ProjectReleaseModel,
    PullRequestModel,
    ProjectEventModel,
    AbstractProjectObjectDbType,
)
from packit_service.service.db_project_events import (
    AddPullRequestEventToDb,
    AddBranchPushEventToDb,
    AddReleaseEventToDb,
)
from packit_service.worker.events.comment import (
    AbstractPRCommentEvent,
    AbstractIssueCommentEvent,
)
from packit_service.worker.events.enums import (
    IssueCommentAction,
    PullRequestCommentAction,
    PullRequestAction,
)
from packit_service.worker.events.event import (
    Event,
    AbstractForgeIndependentEvent,
)
from packit_service.worker.handlers.abstract import MAP_CHECK_PREFIX_TO_HANDLER
from packit_service.worker.helpers.build.copr_build import CoprBuildJobHelper
from packit_service.worker.helpers.build.koji_build import KojiBuildJobHelper

logger = logging.getLogger(__name__)


class AbstractGithubEvent(AbstractForgeIndependentEvent):
    def __init__(self, project_url: str, pr_id: Optional[int] = None, **kwargs):
        super().__init__(pr_id=pr_id)
        self.project_url: str = project_url
        self.git_ref: Optional[str] = None  # git ref that can be 'git checkout'-ed
        self.identifier: Optional[
            str
        ] = None  # will be shown to users -- e.g. in logs or in the copr-project name


# TODO: Rename!!! We support more now, this is confusing!
class ReleaseEvent(AddReleaseEventToDb, AbstractGithubEvent):
    def __init__(
        self, repo_namespace: str, repo_name: str, tag_name: str, project_url: str
    ):
        super().__init__(project_url=project_url)
        self.repo_namespace = repo_namespace
        self.repo_name = repo_name
        self.tag_name = tag_name
        self.git_ref = tag_name
        self.identifier = tag_name
        self._commit_sha: Optional[str] = None

    @property
    def commit_sha(self) -> Optional[str]:  # type:ignore
        # mypy does not like properties
        if not self._commit_sha:
            self._commit_sha = self.project.get_sha_from_tag(tag_name=self.tag_name)
        return self._commit_sha

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["commit_sha"] = self.commit_sha
        return result

    @staticmethod
    def parse(event) -> "Optional[ReleaseEvent]":
        """
        https://developer.github.com/v3/activity/events/types/#releaseevent
        https://developer.github.com/v3/repos/releases/#get-a-single-release

        look into the provided event and see if it's one for a published github release;
        if it is, process it and return input for the job handler
        """
        action = event.get("action")
        release = event.get("release")
        if action != "published" or not release:
            return None

        logger.info(f"GitHub release {release} {action!r} event.")

        repo_namespace = nested_get(event, "repository", "owner", "login")
        repo_name = nested_get(event, "repository", "name")
        if not (repo_namespace and repo_name):
            logger.warning("No full name of the repository.")
            return None

        release_ref = nested_get(event, "release", "tag_name")
        if not release_ref:
            logger.warning("Release tag name is not set.")
            return None

        logger.info(
            f"New release event {release_ref!r} for repo {repo_namespace}/{repo_name}."
        )
        https_url = event["repository"]["html_url"]
        return ReleaseEvent(repo_namespace, repo_name, release_ref, https_url)


class PushGitHubEvent(AddBranchPushEventToDb, AbstractGithubEvent):
    def __init__(
        self,
        repo_namespace: str,
        repo_name: str,
        git_ref: str,
        project_url: str,
        commit_sha: str,
    ):
        super().__init__(project_url=project_url)
        self.repo_namespace = repo_namespace
        self.repo_name = repo_name
        self.git_ref = git_ref
        self.commit_sha = commit_sha
        self.identifier = git_ref

    @staticmethod
    def parse(event) -> "Optional[PushGitHubEvent]":
        """
        Look into the provided event and see if it's one for a new push to the github branch.
        """
        raw_ref = event.get("ref")
        before = event.get("before")
        pusher = nested_get(event, "pusher", "name")

        # https://developer.github.com/v3/activity/events/types/#pushevent
        # > Note: The webhook payload example following the table differs
        # > significantly from the Events API payload described in the table.
        head_commit = (
            event.get("head") or event.get("after") or event.get("head_commit")
        )

        if not (raw_ref and head_commit and before and pusher):
            return None
        elif event.get("deleted"):
            logger.info(
                f"GitHub push event on '{raw_ref}' by {pusher} to delete branch"
            )
            return None

        number_of_commits = event.get("size")
        if number_of_commits is None and "commits" in event:
            number_of_commits = len(event.get("commits"))

        ref = raw_ref.split("/", maxsplit=2)[-1]

        logger.info(
            f"GitHub push event on '{raw_ref}': {before[:8]} -> {head_commit[:8]} "
            f"by {pusher} "
            f"({number_of_commits} {'commit' if number_of_commits == 1 else 'commits'})"
        )

        repo_namespace = nested_get(event, "repository", "owner", "login")
        repo_name = nested_get(event, "repository", "name")

        if not (repo_namespace and repo_name):
            logger.warning("No full name of the repository.")
            return None

        repo_url = nested_get(event, "repository", "html_url")

        return PushGitHubEvent(
            repo_namespace=repo_namespace,
            repo_name=repo_name,
            git_ref=ref,
            project_url=repo_url,
            commit_sha=head_commit,
        )


class PullRequestGithubEvent(AddPullRequestEventToDb, AbstractGithubEvent):
    def __init__(
        self,
        action: PullRequestAction,
        pr_id: int,
        base_repo_namespace: str,
        base_repo_name: str,
        base_ref: str,
        target_repo_namespace: str,
        target_repo_name: str,
        project_url: str,
        commit_sha: str,
        actor: str,
    ) -> None:
        super().__init__(project_url=project_url, pr_id=pr_id)
        self.action = action
        self.base_repo_namespace = base_repo_namespace
        self.base_repo_name = base_repo_name
        self.base_ref = base_ref
        self.target_repo_namespace = target_repo_namespace
        self.target_repo_name = target_repo_name
        self.commit_sha = commit_sha
        self.actor = actor
        self.identifier = str(pr_id)
        self.git_ref = None  # pr_id will be used for checkout

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["action"] = result["action"].value
        return result

    def get_base_project(self) -> Optional[GitProject]:
        return None  # With Github app, we cannot work with fork repo

    @staticmethod
    def parse(event) -> "Optional[PullRequestGithubEvent]":
        """Look into the provided event and see if it's one for a new github PR."""
        if not event.get("pull_request"):
            return None

        pr_id = event.get("number")
        action = event.get("action")
        if action not in {"opened", "reopened", "synchronize"} or not pr_id:
            return None

        logger.info(f"GitHub PR#{pr_id} {action!r} event.")

        # we can't use head repo here b/c the app is set up against the upstream repo
        # and not the fork, on the other hand, we don't process packit.yaml from
        # the PR but what's in the upstream
        base_repo_namespace = nested_get(
            event, "pull_request", "head", "repo", "owner", "login"
        )
        base_repo_name = nested_get(event, "pull_request", "head", "repo", "name")

        if not (base_repo_name and base_repo_namespace):
            logger.warning("No full name of the repository.")
            return None

        base_ref = nested_get(event, "pull_request", "head", "sha")
        if not base_ref:
            logger.warning("Ref where the PR is coming from is not set.")
            return None

        user_login = nested_get(event, "pull_request", "user", "login")
        if not user_login:
            logger.warning("No GitHub login name from event.")
            return None

        target_repo_namespace = nested_get(
            event, "pull_request", "base", "repo", "owner", "login"
        )
        target_repo_name = nested_get(event, "pull_request", "base", "repo", "name")
        logger.info(f"Target repo: {target_repo_namespace}/{target_repo_name}.")

        commit_sha = nested_get(event, "pull_request", "head", "sha")
        https_url = event["repository"]["html_url"]
        return PullRequestGithubEvent(
            action=PullRequestAction[action],
            pr_id=pr_id,
            base_repo_namespace=base_repo_namespace,
            base_repo_name=base_repo_name,
            base_ref=base_ref,
            target_repo_namespace=target_repo_namespace,
            target_repo_name=target_repo_name,
            project_url=https_url,
            commit_sha=commit_sha,
            actor=user_login,
        )


class PullRequestCommentGithubEvent(AbstractPRCommentEvent, AbstractGithubEvent):
    def __init__(
        self,
        action: PullRequestCommentAction,
        pr_id: int,
        base_repo_namespace: str,
        base_repo_name: Optional[str],
        base_ref: Optional[str],
        target_repo_namespace: str,
        target_repo_name: str,
        project_url: str,
        actor: str,
        comment: str,
        comment_id: int,
        commit_sha: Optional[str] = None,
        comment_object: Optional[Comment] = None,
    ) -> None:
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
        self.base_ref = base_ref
        self.target_repo_namespace = target_repo_namespace
        self.target_repo_name = target_repo_name
        self.actor = actor
        self.identifier = str(pr_id)
        self.git_ref = None  # pr_id will be used for checkout

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["action"] = result["action"].value
        return result

    def get_base_project(self) -> Optional[GitProject]:
        return None  # With Github app, we cannot work with fork repo

    @staticmethod
    def parse(event) -> "Optional[PullRequestCommentGithubEvent]":
        """Look into the provided event and see if it is Github PR comment event."""
        # This check is redundant when the method is called from parse_github_comment_event(),
        # but it's needed when called from parse_event().
        if not nested_get(event, "issue", "pull_request"):
            return None

        pr_id = nested_get(event, "issue", "number")
        action = event.get("action")
        if action not in {"created", "edited"} or not pr_id:
            return None

        comment = nested_get(event, "comment", "body")
        comment_id = nested_get(event, "comment", "id")
        logger.info(
            f"Github PR#{pr_id} comment: {comment!r} id#{comment_id} {action!r} event."
        )

        base_repo_namespace = nested_get(event, "issue", "user", "login")
        base_repo_name = nested_get(event, "repository", "name")
        if not (base_repo_name and base_repo_namespace):
            logger.warning("No full name of the repository.")
            return None

        user_login = nested_get(event, "comment", "user", "login")
        if not user_login:
            logger.warning("No GitHub login name from event.")
            return None
        if user_login in {"packit-as-a-service[bot]", "packit-as-a-service-stg[bot]"}:
            logger.debug("Our own comment.")
            return None

        target_repo_namespace = nested_get(event, "repository", "owner", "login")
        target_repo_name = nested_get(event, "repository", "name")

        logger.info(f"Target repo: {target_repo_namespace}/{target_repo_name}.")
        https_url = event["repository"]["html_url"]
        return PullRequestCommentGithubEvent(
            action=PullRequestCommentAction[action],
            pr_id=pr_id,
            base_repo_namespace=base_repo_namespace,
            base_repo_name=None,
            base_ref=None,  # the payload does not include this info
            target_repo_namespace=target_repo_namespace,
            target_repo_name=target_repo_name,
            project_url=https_url,
            actor=user_login,
            comment=comment,
            comment_id=comment_id,
        )


# TODO: Rename!!! We support more now, this is confusing!
class IssueCommentEvent(AbstractIssueCommentEvent, AbstractGithubEvent):
    def __init__(
        self,
        action: IssueCommentAction,
        issue_id: int,
        repo_namespace: str,
        repo_name: str,
        target_repo: str,
        project_url: str,
        actor: str,
        comment: str,
        comment_id: int,
        tag_name: str = "",
        base_ref: Optional[
            str
        ] = "master",  # default is master when working with issues
        comment_object: Optional[Comment] = None,
        dist_git_project_url=None,
    ) -> None:
        super().__init__(
            issue_id=issue_id,
            repo_namespace=repo_namespace,
            repo_name=repo_name,
            project_url=project_url,
            comment=comment,
            comment_id=comment_id,
            tag_name=tag_name,
            comment_object=comment_object,
            dist_git_project_url=dist_git_project_url,
        )
        self.action = action
        self.actor = actor
        self.base_ref = base_ref
        self.target_repo = target_repo
        self.identifier = str(issue_id)

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["action"] = result["action"].value
        return result

    @staticmethod
    def parse(event) -> "Optional[IssueCommentEvent]":
        """Look into the provided event and see if it is Github issue comment event."""
        # This check is redundant when the method is called from parse_github_comment_event(),
        # but it's needed when called from parse_event().
        if nested_get(event, "issue", "pull_request"):
            return None

        issue_id = nested_get(event, "issue", "number")
        action = event.get("action")
        if action != "created" or not issue_id:
            return None

        comment = nested_get(event, "comment", "body")
        comment_id = nested_get(event, "comment", "id")
        if not (comment and comment_id):
            logger.warning("No comment or comment id from the event.")
            return None

        logger.info(f"Github issue#{issue_id} comment: {comment!r} {action!r} event.")

        base_repo_namespace = nested_get(event, "repository", "owner", "login")
        base_repo_name = nested_get(event, "repository", "name")
        if not (base_repo_namespace and base_repo_name):
            logger.warning("No full name of the repository.")

        user_login = nested_get(event, "comment", "user", "login")
        if not user_login:
            logger.warning("No Github login name from event.")
            return None

        target_repo = nested_get(event, "repository", "full_name")
        logger.info(f"Target repo: {target_repo}.")
        https_url = nested_get(event, "repository", "html_url")
        return IssueCommentEvent(
            IssueCommentAction[action],
            issue_id,
            base_repo_namespace,
            base_repo_name,
            target_repo,
            https_url,
            user_login,
            comment,
            comment_id,
        )


class CheckRerunEvent(AbstractGithubEvent):
    def __init__(
        self,
        check_name_job: str,
        check_name_target: str,
        project_url: str,
        repo_namespace: str,
        repo_name: str,
        db_project_event: ProjectEventModel,
        commit_sha: str,
        actor: str,
        pr_id: Optional[int] = None,
        job_identifier: Optional[str] = None,
    ):
        super().__init__(project_url=project_url, pr_id=pr_id)
        self.check_name_job = check_name_job
        self.check_name_target = check_name_target
        self.repo_namespace = repo_namespace
        self.repo_name = repo_name
        self.commit_sha = commit_sha
        self.actor = actor
        self._db_project_event = db_project_event
        self._db_project_object: AbstractProjectObjectDbType = (
            db_project_event.get_project_event_object()
        )
        self.job_identifier = job_identifier

    @property
    def build_targets_override(self) -> Optional[Set[str]]:
        if self.check_name_job in {"rpm-build", "production-build", "koji-build"}:
            return {self.check_name_target}
        return None

    @property
    def tests_targets_override(self) -> Optional[Set[str]]:
        if self.check_name_job == "testing-farm":
            return {self.check_name_target}
        return None

    @property
    def branches_override(self) -> Optional[Set[str]]:
        if self.check_name_job == "propose-downstream":
            return {self.check_name_target}
        return None

    @staticmethod
    def parse_check_name(
        check_name: str, db_project_event: ProjectEventModel
    ) -> Optional[Tuple[str, str, str]]:
        """
        Parse the given name of the check run.

        Check name examples:
        "rpm-build:fedora-34-x86_64"
        "rpm-build:fedora-34-x86_64:identifier"
        "rpm-build:main:fedora-34-x86_64:identifier"
        "propose-downstream:f35"

        For the build and test runs, if the project event is release/commit, the branch
        name or release name is included in the check name - it can be ignored,
        since we are having the DB project event (obtained via external ID of the check).

        Returns:
            tuple of job name (e.g. rpm-build), target and identifier obtained from check run
            (or None if the name cannot be parsed)
        """
        check_name_parts = check_name.split(":", maxsplit=3)
        if len(check_name_parts) < 1:
            logger.warning(f"{check_name} cannot be parsed")
            return None
        check_name_job = check_name_parts[0]

        if check_name_job not in MAP_CHECK_PREFIX_TO_HANDLER:
            logger.warning(
                f"{check_name_job} not in {list(MAP_CHECK_PREFIX_TO_HANDLER.keys())}"
            )
            return None

        check_name_target, check_name_identifier = None, None
        db_project_object = db_project_event.get_project_event_object()

        if len(check_name_parts) == 2:
            _, check_name_target = check_name_parts
        elif len(check_name_parts) == 3:
            build_test_job_names = (
                CoprBuildJobHelper.status_name_build,
                CoprBuildJobHelper.status_name_test,
                KojiBuildJobHelper.status_name_build,
            )
            if (
                check_name_job in build_test_job_names
                and db_project_object.job_config_trigger_type
                in (
                    JobConfigTriggerType.commit,
                    JobConfigTriggerType.release,
                )
            ):
                (
                    _,
                    _,
                    check_name_target,
                ) = check_name_parts
            else:
                (
                    _,
                    check_name_target,
                    check_name_identifier,
                ) = check_name_parts
        elif len(check_name_parts) == 4:
            (
                _,
                _,
                check_name_target,
                check_name_identifier,
            ) = check_name_parts
        else:
            logger.warning(f"{check_name_job} cannot be parsed")
            check_name_job = None

        if not (check_name_job and check_name_target):
            logger.warning(
                f"We were not able to parse the job and target "
                f"from the check run name {check_name}."
            )
            return None

        logger.info(
            f"Check name job: {check_name_job}, check name target: {check_name_target}, "
            f"check name identifier: {check_name_identifier}"
        )

        return check_name_job, check_name_target, check_name_identifier

    @classmethod
    def parse(
        cls,
        event,
    ) -> """Optional[Union[
            CheckRerunPullRequestEvent,
            CheckRerunReleaseEvent,
            CheckRerunCommitEvent,
            ]]""":
        """Look into the provided event and see if it is Github check rerun event."""
        if not (
            nested_get(event, "check_run")
            and nested_get(event, "action") == "rerequested"
        ):
            return None

        check_name = nested_get(event, "check_run", "name")
        logger.info(f"Github check run {check_name} rerun event.")

        deployment = ServiceConfig.get_service_config().deployment
        app = nested_get(event, "check_run", "app", "slug")
        if (deployment == Deployment.prod and app != "packit-as-a-service") or (
            deployment == Deployment.stg and app != "packit-as-a-service-stg"
        ):
            logger.warning(f"Check run created by {app} and not us.")
            return None

        external_id = nested_get(event, "check_run", "external_id")

        if not external_id:
            logger.warning(
                "No external_id to identify the original project event provided."
            )
            return None

        db_project_event = ProjectEventModel.get_by_id(int(external_id))
        if not db_project_event:
            logger.warning(f"Job project event with ID {external_id} not found.")
            return None

        db_project_object = db_project_event.get_project_event_object()
        logger.info(f"Original project event: {db_project_event}")
        logger.info(f"Original project object: {db_project_object}")

        parse_result = cls.parse_check_name(check_name, db_project_event)
        if parse_result is None:
            return None

        check_name_job, check_name_target, check_name_identifier = parse_result

        repo_namespace = nested_get(event, "repository", "owner", "login")
        repo_name = nested_get(event, "repository", "name")
        actor = nested_get(event, "sender", "login")

        if not (repo_namespace and repo_name):
            logger.warning("No full name of the repository.")
            return None

        https_url = event["repository"]["html_url"]

        commit_sha = nested_get(event, "check_run", "head_sha")

        event = None
        if isinstance(db_project_object, PullRequestModel):
            event = CheckRerunPullRequestEvent(
                repo_namespace=repo_namespace,
                repo_name=repo_name,
                project_url=https_url,
                commit_sha=commit_sha,
                pr_id=db_project_object.pr_id,
                check_name_job=check_name_job,
                check_name_target=check_name_target,
                db_project_event=db_project_event,
                actor=actor,
                job_identifier=check_name_identifier,
            )

        elif isinstance(db_project_object, ProjectReleaseModel):
            event = CheckRerunReleaseEvent(
                repo_namespace=repo_namespace,
                repo_name=repo_name,
                project_url=https_url,
                commit_sha=commit_sha,
                tag_name=db_project_object.tag_name,
                check_name_job=check_name_job,
                check_name_target=check_name_target,
                db_project_event=db_project_event,
                actor=actor,
                job_identifier=check_name_identifier,
            )

        elif isinstance(db_project_object, GitBranchModel):
            event = CheckRerunCommitEvent(
                repo_namespace=repo_namespace,
                repo_name=repo_name,
                project_url=https_url,
                commit_sha=commit_sha,
                git_ref=db_project_object.name,
                check_name_job=check_name_job,
                check_name_target=check_name_target,
                db_project_event=db_project_event,
                actor=actor,
                job_identifier=check_name_identifier,
            )

        return event


class CheckRerunCommitEvent(CheckRerunEvent):
    _db_project_object: GitBranchModel

    def __init__(
        self,
        project_url: str,
        repo_namespace: str,
        repo_name: str,
        commit_sha: str,
        git_ref: str,
        check_name_job: str,
        check_name_target: str,
        db_project_event,
        actor: str,
        job_identifier: Optional[str] = None,
    ):
        super().__init__(
            check_name_job=check_name_job,
            check_name_target=check_name_target,
            project_url=project_url,
            repo_namespace=repo_namespace,
            repo_name=repo_name,
            db_project_event=db_project_event,
            commit_sha=commit_sha,
            actor=actor,
            job_identifier=job_identifier,
        )
        self.identifier = git_ref
        self.git_ref = git_ref


class CheckRerunPullRequestEvent(CheckRerunEvent):
    _db_project_object: PullRequestModel

    def __init__(
        self,
        pr_id: int,
        repo_namespace: str,
        repo_name: str,
        project_url: str,
        commit_sha: str,
        check_name_job: str,
        check_name_target: str,
        db_project_event,
        actor: str,
        job_identifier: Optional[str] = None,
    ):
        super().__init__(
            check_name_job=check_name_job,
            check_name_target=check_name_target,
            project_url=project_url,
            repo_namespace=repo_namespace,
            repo_name=repo_name,
            db_project_event=db_project_event,
            commit_sha=commit_sha,
            pr_id=pr_id,
            actor=actor,
            job_identifier=job_identifier,
        )
        self.identifier = str(pr_id)
        self.git_ref = None


class CheckRerunReleaseEvent(CheckRerunEvent):
    _db_project_object: ProjectReleaseModel

    def __init__(
        self,
        repo_namespace: str,
        repo_name: str,
        tag_name: str,
        project_url: str,
        commit_sha: str,
        check_name_job: str,
        check_name_target: str,
        db_project_event,
        actor: str,
        job_identifier: Optional[str] = None,
    ):
        super().__init__(
            check_name_job=check_name_job,
            check_name_target=check_name_target,
            project_url=project_url,
            repo_namespace=repo_namespace,
            repo_name=repo_name,
            db_project_event=db_project_event,
            commit_sha=commit_sha,
            actor=actor,
            job_identifier=job_identifier,
        )
        self.tag_name = tag_name
        self.git_ref = tag_name
        self.identifier = tag_name


# TODO: Rename!!! We support more now, this is confusing
class InstallationEvent(Event):
    def __init__(
        self,
        installation_id: int,
        account_login: str,
        account_id: int,
        account_url: str,
        account_type: str,
        created_at: Union[int, float, str],
        repositories: List[str],
        sender_id: int,
        sender_login: str,
        status: AllowlistStatus = AllowlistStatus.waiting,
    ):
        super().__init__(created_at)
        self.installation_id = installation_id
        self.actor = account_login
        # account == namespace (user/organization) into which the app has been installed
        self.account_login = account_login
        self.account_id = account_id
        self.account_url = account_url
        self.account_type = account_type
        # repos within the account/namespace
        self.repositories = repositories
        # sender == user who installed the app into 'account'
        self.sender_id = sender_id
        self.sender_login = sender_login
        self.status = status

    @classmethod
    def from_event_dict(cls, event: dict):
        return InstallationEvent(
            installation_id=event.get("installation_id"),
            account_login=event.get("account_login"),
            account_id=event.get("account_id"),
            account_url=event.get("account_url"),
            account_type=event.get("account_type"),
            created_at=event.get("created_at"),
            repositories=event.get("repositories"),
            sender_id=event.get("sender_id"),
            sender_login=event.get("sender_login"),
        )

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["status"] = result["status"].value
        return result

    @property
    def packages_config(self):
        return None

    @property
    def project(self):
        return self.get_project()

    def get_project(self):
        return None

    @staticmethod
    def parse(event) -> "Optional[InstallationEvent]":
        """Look into the provided event and see if it is Github App installation details."""
        # Check if installation key in JSON isn't enough, we have to check the account as well
        if not nested_get(event, "installation", "account"):
            return None

        action = event["action"]
        if action != "created":
            # We're currently not interested in removed/deleted/updated event.
            return None
        installation_id = event["installation"]["id"]
        # if action == 'created' then repos are in repositories
        repositories = event.get("repositories", [])
        repo_names = [repo["full_name"] for repo in repositories]

        logger.info(f"Github App installation {action!r} event. id: {installation_id}")
        logger.debug(
            f"account: {event['installation']['account']}, "
            f"repositories: {repo_names}, sender: {event['sender']}"
        )

        # namespace (user/organization) into which the app has been installed
        account_login = event["installation"]["account"]["login"]
        account_id = event["installation"]["account"]["id"]
        account_url = event["installation"]["account"]["url"]
        account_type = event["installation"]["account"]["type"]  # User or Organization
        created_at = event["installation"]["created_at"]

        # user who installed the app into 'account'
        sender_id = event["sender"]["id"]
        sender_login = event["sender"]["login"]

        return InstallationEvent(
            installation_id,
            account_login,
            account_id,
            account_url,
            account_type,
            created_at,
            repo_names,
            sender_id,
            sender_login,
        )
