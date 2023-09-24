# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

"""
Parser is transforming github JSONs into `events` objects
"""
import logging
from typing import Optional, Union

from packit.utils import nested_get
from packit_service.worker.events import (
    AbstractCoprBuildEvent,
    KojiTaskEvent,
    PushPagureEvent,
    IssueCommentGitlabEvent,
    MergeRequestCommentGitlabEvent,
    MergeRequestGitlabEvent,
    PushGitlabEvent,
    InstallationEvent,
    IssueCommentEvent,
    PullRequestCommentGithubEvent,
    PullRequestGithubEvent,
    PushGitHubEvent,
    ReleaseEvent,
    TestingFarmResultsEvent,
    PullRequestCommentPagureEvent,
    PipelineGitlabEvent,
    CheckRerunCommitEvent,
    CheckRerunPullRequestEvent,
    CheckRerunReleaseEvent,
    ReleaseGitlabEvent,
    TagPushGitlabEvent,
    VMImageBuildResultEvent,
    CheckRerunEvent,
)
from packit_service.worker.events.koji import KojiBuildEvent
from packit_service.worker.events.new_hotness import NewHotnessUpdateEvent
from packit_service.worker.events.pagure import PullRequestFlagPagureEvent

logger = logging.getLogger(__name__)


class Parser:
    """
    Once we receive a new event (GitHub/GitLab webhook) for every event
    we need to have method inside the `Parser` class to create objects defined in `event.py`.
    """

    @staticmethod
    def parse_event(
        event: dict,
    ) -> Optional[
        Union[
            PullRequestGithubEvent,
            InstallationEvent,
            ReleaseEvent,
            TestingFarmResultsEvent,
            PullRequestCommentGithubEvent,
            IssueCommentEvent,
            AbstractCoprBuildEvent,
            PushGitHubEvent,
            MergeRequestGitlabEvent,
            KojiTaskEvent,
            KojiBuildEvent,
            MergeRequestCommentGitlabEvent,
            IssueCommentGitlabEvent,
            PushGitlabEvent,
            PipelineGitlabEvent,
            PullRequestFlagPagureEvent,
            PullRequestCommentPagureEvent,
            PushPagureEvent,
            CheckRerunCommitEvent,
            CheckRerunPullRequestEvent,
            CheckRerunReleaseEvent,
            NewHotnessUpdateEvent,
            ReleaseGitlabEvent,
            TagPushGitlabEvent,
            VMImageBuildResultEvent,
        ]
    ]:
        """
        Try to parse all JSONs that we process.

        When reacting to fedmsg events, be aware that we are squashing the structure
        so we take only `body` with the `topic` key included.
        See: https://github.com/packit/packit-service-fedmsg/blob/
             e53586bf7ace0c46fd6812fe8dc11491e5e6cf41/packit_service_fedmsg/consumer.py#L137

        :param event: JSON from GitHub/GitLab
        :return: event object
        """

        if not event:
            logger.warning("No event to process!")
            return None

        for response in map(
            lambda parser: parser(event),
            (
                PullRequestGithubEvent.parse,
                PullRequestCommentGithubEvent.parse,
                IssueCommentEvent.parse,
                ReleaseEvent.parse,
                PushGitHubEvent.parse,
                CheckRerunEvent.parse,
                InstallationEvent.parse,
                TestingFarmResultsEvent.parse,
                AbstractCoprBuildEvent.parse,
                MergeRequestGitlabEvent.parse,
                KojiTaskEvent.parse,
                KojiBuildEvent.parse,
                MergeRequestCommentGitlabEvent.parse,
                IssueCommentGitlabEvent.parse,
                PushGitlabEvent.parse,
                PipelineGitlabEvent.parse,
                PushPagureEvent.parse,
                PullRequestFlagPagureEvent.parse,
                PullRequestCommentPagureEvent.parse,
                NewHotnessUpdateEvent.parse,
                ReleaseGitlabEvent.parse,
                TagPushGitlabEvent.parse,
            ),
        ):
            if response:
                return response

        logger.debug("We don't process this event.")
        return None

    @staticmethod
    def parse_github_comment_event(
        event,
    ) -> Optional[Union[PullRequestCommentGithubEvent, IssueCommentEvent]]:
        """Check whether the comment event from GitHub comes from a PR or issue,
        and parse accordingly.
        """
        if nested_get(event, "issue", "pull_request"):
            return PullRequestCommentGithubEvent.parse(event)
        else:
            return IssueCommentEvent.parse(event)

    @staticmethod
    def parse_gitlab_comment_event(
        event,
    ) -> Optional[Union[MergeRequestCommentGitlabEvent, IssueCommentGitlabEvent]]:
        """Check whether the comment event from Gitlab comes from an MR or issue,
        and parse accordingly.
        """
        if event.get("merge_request"):
            return MergeRequestCommentGitlabEvent.parse(event)
        else:
            return IssueCommentGitlabEvent.parse(event)

    # The .__func__ are needed for Python < 3.10
    MAPPING = {
        "github": {
            "check_run": CheckRerunEvent.parse.__func__,  # type: ignore
            "pull_request": PullRequestGithubEvent.parse.__func__,  # type: ignore
            "issue_comment": parse_github_comment_event.__func__,  # type: ignore
            "release": ReleaseEvent.parse.__func__,  # type: ignore
            "push": PushGitHubEvent.parse.__func__,  # type: ignore
            "installation": InstallationEvent.parse.__func__,  # type: ignore
        },
        # https://docs.gitlab.com/ee/user/project/integrations/webhook_events.html
        "gitlab": {
            "Merge Request Hook": MergeRequestGitlabEvent.parse.__func__,  # type: ignore
            "Note Hook": parse_gitlab_comment_event.__func__,  # type: ignore
            "Push Hook": PushGitlabEvent.parse.__func__,  # type: ignore
            "Tag Push Hook": TagPushGitlabEvent.parse.__func__,  # type: ignore
            "Pipeline Hook": PipelineGitlabEvent.parse.__func__,  # type: ignore
            "Release Hook": ReleaseGitlabEvent.parse.__func__,  # type: ignore
        },
        "fedora-messaging": {
            "pagure.pull-request.flag.added": PullRequestFlagPagureEvent.parse.__func__,  # type: ignore
            "pagure.pull-request.flag.updated": PullRequestFlagPagureEvent.parse.__func__,  # type: ignore
            "pagure.pull-request.comment.added": PullRequestCommentPagureEvent.parse.__func__,  # type: ignore # noqa: E501
            "git.receive": PushPagureEvent.parse.__func__,  # type: ignore
            "copr.build.start": AbstractCoprBuildEvent.parse.__func__,  # type: ignore
            "copr.build.end": AbstractCoprBuildEvent.parse.__func__,  # type: ignore
            "buildsys.task.state.change": KojiTaskEvent.parse.__func__,  # type: ignore
            "buildsys.build.state.change": KojiBuildEvent.parse.__func__,  # type: ignore
            "hotness.update.bug.file": NewHotnessUpdateEvent.parse.__func__,  # type: ignore
        },
        "testing-farm": {
            "results": TestingFarmResultsEvent.parse.__func__,  # type: ignore
        },
    }
