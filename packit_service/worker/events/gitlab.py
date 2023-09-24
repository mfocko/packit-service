# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import logging
from typing import Dict, Optional

from ogr.abstract import GitProject, Comment
from ogr.parsing import parse_git_repo
from packit.utils import nested_get

from packit_service.exceptions import PackitParserException
from packit_service.service.db_project_events import (
    AddPullRequestEventToDb,
    AddBranchPushEventToDb,
    AddReleaseEventToDb,
)
from packit_service.worker.events.comment import (
    AbstractIssueCommentEvent,
    AbstractPRCommentEvent,
)
from packit_service.worker.events.enums import GitlabEventAction
from packit_service.worker.events.event import AbstractForgeIndependentEvent

logger = logging.getLogger(__name__)


class AbstractGitlabEvent(AbstractForgeIndependentEvent):
    def __init__(self, project_url: str, pr_id: Optional[int] = None, **kwargs):
        super().__init__(pr_id=pr_id)
        self.project_url: str = project_url
        self.git_ref: Optional[str] = None
        self.identifier: Optional[
            str
        ] = None  # will be shown to users -- e.g. in logs or in the copr-project name

    @staticmethod
    def is_push_create_event(event) -> bool:
        """The given push event is a create push event?

        Returns:
            True if the push event is a create
            branch/tag event and not a delete one.
            False otherwise.
        """

        ref = event.get("ref")
        actor = event.get("user_username")

        if not (ref and event.get("commits") and event.get("before") and actor):
            return False
        elif event.get("after").startswith("0000000"):
            logger.info(f"GitLab push event on '{ref}' by {actor} to delete branch/tag")
            return False

        return True

    @classmethod
    def parse_common_push_data(cls, event) -> tuple:
        """A gitlab push and a gitlab tag push have many common data
        parsable in the same way.

        Returns:
            a tuple like (actor, project_url, parsed_url, ref, head_commit)
        Raises:
            PackitParserException
        """
        if not (raw_ref := event.get("ref")):
            raise PackitParserException("No ref info from event.")
        before = event.get("before")
        checkout_sha = event.get("checkout_sha")
        actor = event.get("user_username")
        commits = event.get("commits", [])
        number_of_commits = event.get("total_commits_count")

        if not cls.is_push_create_event(event):
            raise PackitParserException(
                "Event is not a push create event, stop parsing"
            )

        # The first item in the list should be the head (newest) commit,
        # but rather not assume anything and select the "checkout_sha" one.
        head_commit = next(c for c in commits if c["id"] == checkout_sha)

        logger.info(
            f"Gitlab push event on '{raw_ref}': {before[:8]} -> {checkout_sha[:8]} "
            f"by {actor} "
            f"({number_of_commits} {'commit' if number_of_commits == 1 else 'commits'})"
        )

        if not (project_url := nested_get(event, "project", "web_url")):
            raise PackitParserException(
                "Target project url not found in the event, stop parsing"
            )
        parsed_url = parse_git_repo(potential_url=project_url)
        logger.info(
            f"Project: "
            f"repo={parsed_url.repo} "
            f"namespace={parsed_url.namespace} "
            f"url={project_url}."
        )
        ref = raw_ref.split("/", maxsplit=2)[-1]

        return actor, project_url, parsed_url, ref, head_commit


class PushGitlabEvent(AddBranchPushEventToDb, AbstractGitlabEvent):
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
    def parse(event) -> "Optional[PushGitlabEvent]":
        """
        Look into the provided event and see if it's one for a new push to the gitlab branch.
        https://docs.gitlab.com/ee/user/project/integrations/webhooks.html#push-events
        """

        if event.get("object_kind") != "push":
            return None

        try:
            (
                _,
                project_url,
                parsed_url,
                ref,
                head_commit,
            ) = AbstractGitlabEvent.parse_common_push_data(event)
        except PackitParserException as e:
            logger.info(e)
            return None

        return PushGitlabEvent(
            repo_namespace=parsed_url.namespace,
            repo_name=parsed_url.repo,
            git_ref=ref,
            project_url=project_url,
            commit_sha=head_commit.get("id"),
        )


class MergeRequestGitlabEvent(AddPullRequestEventToDb, AbstractGitlabEvent):
    def __init__(
        self,
        action: GitlabEventAction,
        actor: str,
        object_id: int,
        object_iid: int,
        source_repo_namespace: str,
        source_repo_name: str,
        source_repo_branch: str,
        source_project_url: str,
        target_repo_namespace: str,
        target_repo_name: str,
        target_repo_branch: str,
        project_url: str,
        commit_sha: str,
        oldrev: Optional[str],
        title: str,
        description: str,
        url: str,
    ):
        super().__init__(
            project_url=project_url,
            pr_id=object_iid,
        )
        self.action = action
        self.actor = actor
        self.object_id = object_id
        self.identifier = str(object_iid)
        self.source_repo_namespace = source_repo_namespace
        self.source_repo_name = source_repo_name
        self.source_repo_branch = source_repo_branch
        self.source_project_url = source_project_url
        self.target_repo_namespace = target_repo_namespace
        self.target_repo_name = target_repo_name
        self.target_repo_branch = target_repo_branch
        self.project_url = project_url
        self.commit_sha = commit_sha
        self.oldrev = oldrev
        self.title = title
        self.description = description
        self.url = url

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["action"] = result["action"].value
        return result

    def get_base_project(self) -> GitProject:
        return self.project.service.get_project(
            namespace=self.source_repo_namespace,
            repo=self.source_repo_name,
        )

    @staticmethod
    def parse(event) -> "Optional[MergeRequestGitlabEvent]":
        """Look into the provided event and see if it's one for a new gitlab MR."""
        if event.get("object_kind") != "merge_request":
            return None

        state = event["object_attributes"]["state"]
        if state not in {"opened", "closed"}:
            return None
        action = nested_get(event, "object_attributes", "action")
        if action not in {"reopen", "update"}:
            action = state

        actor = event["user"]["username"]
        if not actor:
            logger.warning("No Gitlab username from event.")
            return None

        object_id = event["object_attributes"]["id"]
        if not object_id:
            logger.warning("No object id from the event.")
            return None

        object_iid = event["object_attributes"]["iid"]
        if not object_iid:
            logger.warning("No object iid from the event.")
            return None

        source_project_url = nested_get(event, "object_attributes", "source", "web_url")
        if not source_project_url:
            logger.warning("Source project url not found in the event.")
            return None
        parsed_source_url = parse_git_repo(potential_url=source_project_url)
        source_repo_branch = nested_get(event, "object_attributes", "source_branch")
        logger.info(
            f"Source: "
            f"url={source_project_url} "
            f"namespace={parsed_source_url.namespace} "
            f"repo={parsed_source_url.repo} "
            f"branch={source_repo_branch}."
        )

        target_project_url = nested_get(event, "project", "web_url")
        if not target_project_url:
            logger.warning("Target project url not found in the event.")
            return None
        parsed_target_url = parse_git_repo(potential_url=target_project_url)
        target_repo_branch = nested_get(event, "object_attributes", "target_branch")
        logger.info(
            f"Target: "
            f"url={target_project_url} "
            f"namespace={parsed_target_url.namespace} "
            f"repo={parsed_target_url.repo} "
            f"branch={target_repo_branch}."
        )

        commit_sha = nested_get(event, "object_attributes", "last_commit", "id")
        oldrev = nested_get(event, "object_attributes", "oldrev")

        title = nested_get(event, "object_attributes", "title")
        description = nested_get(event, "object_attributes", "description")
        url = nested_get(event, "object_attributes", "url")

        return MergeRequestGitlabEvent(
            action=GitlabEventAction[action],
            actor=actor,
            object_id=object_id,
            object_iid=object_iid,
            source_repo_namespace=parsed_source_url.namespace,
            source_repo_name=parsed_source_url.repo,
            source_repo_branch=source_repo_branch,
            source_project_url=source_project_url,
            target_repo_namespace=parsed_target_url.namespace,
            target_repo_name=parsed_target_url.repo,
            target_repo_branch=target_repo_branch,
            project_url=target_project_url,
            commit_sha=commit_sha,
            oldrev=oldrev,
            title=title,
            description=description,
            url=url,
        )


class MergeRequestCommentGitlabEvent(AbstractPRCommentEvent, AbstractGitlabEvent):
    def __init__(
        self,
        action: GitlabEventAction,
        object_id: int,
        object_iid: int,
        source_repo_namespace: str,
        source_repo_name: Optional[str],
        target_repo_namespace: str,
        target_repo_name: str,
        project_url: str,
        actor: str,
        comment: str,
        comment_id: int,
        commit_sha: str,
        comment_object: Optional[Comment] = None,
    ):
        super().__init__(
            project_url=project_url,
            pr_id=object_iid,
            comment=comment,
            comment_id=comment_id,
            commit_sha=commit_sha,
            comment_object=comment_object,
        )
        self.action = action
        self.object_id = object_id
        self.source_repo_name = source_repo_name
        self.source_repo_namespace = source_repo_namespace
        self.target_repo_namespace = target_repo_namespace
        self.target_repo_name = target_repo_name
        self.actor = actor
        self.identifier = str(object_iid)

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["action"] = result["action"].value
        return result

    def get_base_project(self) -> GitProject:
        return self.project.service.get_project(
            namespace=self.source_repo_namespace,
            repo=self.source_repo_name,
        )

    @staticmethod
    def parse(event) -> "Optional[MergeRequestCommentGitlabEvent]":
        """Look into the provided event and see if it is Gitlab MR comment event."""
        if event.get("object_kind") != "note":
            return None

        merge_request = event.get("merge_request")
        if not merge_request:
            return None

        state = nested_get(event, "merge_request", "state")
        if state != "opened":
            return None

        action = nested_get(event, "merge_request", "action")
        if action not in {"reopen", "update"}:
            action = state

        object_iid = nested_get(event, "merge_request", "iid")
        if not object_iid:
            logger.warning("No object iid from the event.")

        object_id = nested_get(event, "merge_request", "id")
        if not object_id:
            logger.warning("No object id from the event.")

        comment = nested_get(event, "object_attributes", "note")
        comment_id = nested_get(event, "object_attributes", "id")
        logger.info(
            f"Gitlab MR id#{object_id} iid#{object_iid} comment: {comment!r} id#{comment_id} "
            f"{action!r} event."
        )

        source_project_url = nested_get(event, "merge_request", "source", "web_url")
        if not source_project_url:
            logger.warning("Source project url not found in the event.")
            return None
        parsed_source_url = parse_git_repo(potential_url=source_project_url)
        logger.info(
            f"Source: "
            f"repo={parsed_source_url.repo} "
            f"namespace={parsed_source_url.namespace} "
            f"url={source_project_url}."
        )

        target_project_url = nested_get(event, "project", "web_url")
        if not target_project_url:
            logger.warning("Target project url not found in the event.")
            return None
        parsed_target_url = parse_git_repo(potential_url=target_project_url)
        logger.info(
            f"Target: "
            f"repo={parsed_target_url.repo} "
            f"namespace={parsed_target_url.namespace} "
            f"url={target_project_url}."
        )

        actor = nested_get(event, "user", "username")
        if not actor:
            logger.warning("No Gitlab username from event.")
            return None

        if actor in {"packit-as-a-service", "packit-as-a-service-stg"}:
            logger.debug("Our own comment.")
            return None

        commit_sha = nested_get(event, "merge_request", "last_commit", "id")
        if not commit_sha:
            logger.warning("No commit_sha from the event.")
            return None

        return MergeRequestCommentGitlabEvent(
            action=GitlabEventAction[action],
            object_id=object_id,
            object_iid=object_iid,
            source_repo_namespace=parsed_source_url.namespace,
            source_repo_name=parsed_source_url.repo,
            target_repo_namespace=parsed_target_url.namespace,
            target_repo_name=parsed_target_url.repo,
            project_url=target_project_url,
            actor=actor,
            comment=comment,
            commit_sha=commit_sha,
            comment_id=comment_id,
        )


class IssueCommentGitlabEvent(AbstractIssueCommentEvent, AbstractGitlabEvent):
    def __init__(
        self,
        action: GitlabEventAction,
        issue_id: int,
        repo_namespace: str,
        repo_name: str,
        project_url: str,
        actor: str,
        comment: str,
        comment_id: int,
        tag_name: str = "",
        comment_object: Optional[Comment] = None,
        dist_git_project_url=None,
    ):
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

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["action"] = result["action"].value
        return result

    @staticmethod
    def parse(event) -> "Optional[IssueCommentGitlabEvent]":
        """Look into the provided event and see if it is Gitlab Issue comment event."""
        if event.get("object_kind") != "note":
            return None

        issue = event.get("issue")
        if not issue:
            return None

        issue_id = nested_get(event, "issue", "iid")
        if not issue_id:
            logger.warning("No issue id from the event.")
            return None
        comment = nested_get(event, "object_attributes", "note")
        comment_id = nested_get(event, "object_attributes", "id")
        if not (comment and comment_id):
            logger.warning("No note or note id from the event.")
            return None

        state = nested_get(event, "issue", "state")
        if not state:
            logger.warning("No state from the event.")
            return None
        if state != "opened":
            return None
        action = nested_get(event, "object_attributes", "action")
        if action not in {"reopen", "update"}:
            action = state

        logger.info(
            f"Gitlab issue ID: {issue_id} comment: {comment!r} {action!r} event."
        )

        project_url = nested_get(event, "project", "web_url")
        if not project_url:
            logger.warning("Target project url not found in the event.")
            return None
        parsed_url = parse_git_repo(potential_url=project_url)
        logger.info(
            f"Project: "
            f"repo={parsed_url.repo} "
            f"namespace={parsed_url.namespace} "
            f"url={project_url}."
        )

        actor = nested_get(event, "user", "username")
        if not actor:
            logger.warning("No Gitlab username from event.")
            return None

        return IssueCommentGitlabEvent(
            action=GitlabEventAction[action],
            issue_id=issue_id,
            repo_namespace=parsed_url.namespace,
            repo_name=parsed_url.repo,
            project_url=project_url,
            actor=actor,
            comment=comment,
            comment_id=comment_id,
        )


class PipelineGitlabEvent(AbstractGitlabEvent):
    def __init__(
        self,
        project_url: str,
        project_name: str,
        pipeline_id: int,
        git_ref: str,
        status: str,
        detailed_status: str,
        commit_sha: str,
        source: str,
        merge_request_url: Optional[str],
    ):
        super().__init__(project_url=project_url)
        self.project_name = project_name
        self.pipeline_id = pipeline_id
        self.git_ref = git_ref
        self.status = status
        self.detailed_status = detailed_status
        self.commit_sha = commit_sha
        self.source = source
        self.merge_request_url = merge_request_url

    @staticmethod
    def parse(event) -> "Optional[PipelineGitlabEvent]":
        """
        Look into the provided event and see if it is Gitlab Pipeline event.
        https://docs.gitlab.com/ee/user/project/integrations/webhooks.html#pipeline-events
        """

        if event.get("object_kind") != "pipeline":
            return None

        # Project where the pipeline runs. In case of MR pipeline this can be
        # either source project or target project depending on pipeline type.
        project_url = nested_get(event, "project", "web_url")
        project_name = nested_get(event, "project", "name")

        pipeline_id = nested_get(event, "object_attributes", "id")

        # source branch name
        git_ref = nested_get(event, "object_attributes", "ref")
        # source commit sha
        commit_sha = nested_get(event, "object_attributes", "sha")
        status = nested_get(event, "object_attributes", "status")
        detailed_status = nested_get(event, "object_attributes", "detailed_status")
        # merge_request_event or push
        source = nested_get(event, "object_attributes", "source")
        # merge_request is null if source == "push"
        merge_request_url = nested_get(event, "merge_request", "url")

        return PipelineGitlabEvent(
            project_url=project_url,
            project_name=project_name,
            pipeline_id=pipeline_id,
            git_ref=git_ref,
            status=status,
            detailed_status=detailed_status,
            commit_sha=commit_sha,
            source=source,
            merge_request_url=merge_request_url,
        )


class ReleaseGitlabEvent(AddReleaseEventToDb, AbstractGitlabEvent):
    def __init__(
        self,
        repo_namespace: str,
        repo_name: str,
        git_ref: str,
        tag_name: str,
        project_url: str,
        commit_sha: str,
    ):
        super().__init__(project_url=project_url)
        self.repo_namespace = repo_namespace
        self.repo_name = repo_name
        self.git_ref = git_ref
        self.tag_name = tag_name
        self._commit_sha = commit_sha

    @property
    def commit_sha(self):
        return self._commit_sha

    def get_dict(self, default_dict: Optional[dict] = None) -> dict:
        result = super().get_dict()
        result["commit_sha"] = self.commit_sha
        return result

    @staticmethod
    def parse(event) -> "Optional[ReleaseGitlabEvent]":
        """
        Look into the provided event and see if it's one for a new push to the gitlab branch.
        https://docs.gitlab.com/ee/user/project/integrations/webhooks.html#release-events
        """

        if event.get("object_kind") != "release":
            return None

        if event.get("action") != "create":
            return None

        project_url = nested_get(event, "project", "web_url")
        if not project_url:
            logger.warning("Target project url not found in the event.")
            return None
        parsed_url = parse_git_repo(potential_url=project_url)
        tag_name = event.get("tag")

        logger.info(
            f"Gitlab release with tag {tag_name} event on Project: "
            f"repo={parsed_url.repo} "
            f"namespace={parsed_url.namespace} "
            f"url={project_url}."
        )
        commit_sha = nested_get(event, "commit", "id")

        return ReleaseGitlabEvent(
            repo_namespace=parsed_url.namespace,
            repo_name=parsed_url.repo,
            project_url=project_url,
            tag_name=tag_name,
            git_ref=tag_name,
            commit_sha=commit_sha,
        )


class TagPushGitlabEvent(AddBranchPushEventToDb, AbstractGitlabEvent):
    def __init__(
        self,
        repo_namespace: str,
        repo_name: str,
        actor: str,
        git_ref: str,
        project_url: str,
        commit_sha: str,
        title: str,
        message: str,
    ):
        super().__init__(project_url=project_url)
        self.repo_namespace = repo_namespace
        self.repo_name = repo_name
        self.actor = actor
        self.git_ref = git_ref
        self.commit_sha = commit_sha
        self.title = title
        self.message = message

    @staticmethod
    def parse(event) -> "Optional[TagPushGitlabEvent]":
        """
        Look into the provided event and see if it's one for a new push to the gitlab branch.
        https://docs.gitlab.com/ee/user/project/integrations/webhooks.html#tag-events
        """

        if event.get("object_kind") != "tag_push":
            return None

        try:
            (
                actor,
                project_url,
                parsed_url,
                ref,
                head_commit,
            ) = AbstractGitlabEvent.parse_common_push_data(event)
        except PackitParserException as e:
            logger.info(e)
            return None

        logger.info(
            f"Gitlab tag push {ref} event with commit_sha {head_commit.get('id')} "
            f"by actor {actor} on Project: "
            f"repo={parsed_url.repo} "
            f"namespace={parsed_url.namespace} "
            f"url={project_url}."
        )

        return TagPushGitlabEvent(
            repo_namespace=parsed_url.namespace,
            repo_name=parsed_url.repo,
            actor=actor,
            git_ref=ref,
            project_url=project_url,
            commit_sha=head_commit.get("id"),
            title=head_commit.get("title"),
            message=head_commit.get("message"),
        )
