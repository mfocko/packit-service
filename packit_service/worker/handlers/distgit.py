# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

"""
This file defines classes for job handlers specific for Fedmsg events
"""

import logging
from os import getenv

import shutil
from celery import Task
from packit.exceptions import PackitException

from packit.api import PackitAPI
from packit.config import JobConfig, JobType
from packit.config.aliases import get_branches
from packit.config.package_config import PackageConfig
from packit.local_project import LocalProject
from packit.utils.repo import RepositoryCache

from packit_service import sentry_integration
from packit_service.constants import (
    CONTACTS_URL,
    DEFAULT_RETRY_LIMIT,
    FILE_DOWNLOAD_FAILURE,
    MSG_RETRIGGER,
)
from packit_service.worker.events import (
    DistGitCommitEvent,
    PushPagureEvent,
    ReleaseEvent,
    IssueCommentEvent,
    IssueCommentGitlabEvent,
)
from packit_service.worker.handlers.abstract import (
    JobHandler,
    TaskName,
    configured_as,
    reacts_to,
    FedmsgHandler,
    run_for_comment,
)
from packit_service.worker.result import TaskResults

logger = logging.getLogger(__name__)


@configured_as(job_type=JobType.sync_from_downstream)
@reacts_to(event=DistGitCommitEvent)
class SyncFromDownstream(FedmsgHandler):
    """Sync new specfile changes to upstream after a new git push in the dist-git."""

    task_name = TaskName.sync_from_downstream

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        event: dict,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            event=event,
        )
        self.branch = event.get("branch")
        self.dg_branch = event.get("dg_branch")

    def run(self) -> TaskResults:
        self.local_project = LocalProject(
            git_project=self.project,
            working_dir=self.service_config.command_handler_work_dir,
            cache=RepositoryCache(
                cache_path=self.service_config.repository_cache,
                add_new=self.service_config.add_repositories_to_repository_cache,
            )
            if self.service_config.repository_cache
            else None,
        )
        self.api = PackitAPI(
            self.service_config,
            self.job_config,
            upstream_local_project=self.local_project,
            stage=self.service_config.use_stage(),
        )
        # rev is a commit
        # we use branch on purpose so we get the latest thing
        # TODO: check if rev is HEAD on {branch}, warn then?
        self.api.sync_from_downstream(
            dist_git_branch=self.dg_branch,
            upstream_branch=self.branch,
            sync_only_specfile=True,
        )
        return TaskResults(success=True, details={})


class AbortProposeDownstream(Exception):
    """Abort propose-downstream process"""


@configured_as(job_type=JobType.propose_downstream)
@run_for_comment(command="propose-downstream")
@run_for_comment(command="propose-update")  # deprecated
@reacts_to(event=ReleaseEvent)
@reacts_to(event=IssueCommentEvent)
@reacts_to(event=IssueCommentGitlabEvent)
class ProposeDownstreamHandler(JobHandler):
    task_name = TaskName.propose_downstream

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        event: dict,
        task: Task = None,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            event=event,
        )
        self.task = task

    def sync_branch(self, branch: str):
        try:
            self.api.sync_release(dist_git_branch=branch, tag=self.data.tag_name)
        except Exception as ex:
            # the archive has not been uploaded to PyPI yet
            if FILE_DOWNLOAD_FAILURE in str(ex):
                # retry for the archive to become available
                logger.info(f"We were not able to download the archive: {ex}")
                # when the task hits max_retries, it raises MaxRetriesExceededError
                # and the error handling code would be never executed
                retries = self.task.request.retries
                if retries < int(getenv("CELERY_RETRY_LIMIT", DEFAULT_RETRY_LIMIT)):
                    # will retry in: 1m and then again in another 2m
                    delay = 60 * 2 ** retries
                    logger.info(f"Will retry for the {retries + 1}. time in {delay}s.")
                    # throw=False so that exception is not raised and task
                    # is not retried also automatically
                    self.task.retry(exc=ex, countdown=delay, throw=False)
                    raise AbortProposeDownstream()
            raise ex
        finally:
            self.api.up.local_project.reset("HEAD")

    def run(self) -> TaskResults:
        """
        Sync the upstream release to dist-git as a pull request.
        """

        self.local_project = LocalProject(
            git_project=self.project,
            working_dir=self.service_config.command_handler_work_dir,
            cache=RepositoryCache(
                cache_path=self.service_config.repository_cache,
                add_new=self.service_config.add_repositories_to_repository_cache,
            )
            if self.service_config.repository_cache
            else None,
        )

        self.api = PackitAPI(
            self.service_config,
            self.job_config,
            self.local_project,
            stage=self.service_config.use_stage(),
        )

        errors = {}
        default_dg_branch = self.api.dg.local_project.git_project.default_branch
        try:
            for branch in get_branches(
                *self.job_config.metadata.dist_git_branches, default=default_dg_branch
            ):
                try:
                    self.sync_branch(branch=branch)
                except AbortProposeDownstream:
                    return TaskResults(
                        success=True,  # do not create a Sentry issue
                        details={
                            "msg": "Not able to download archive. Task will be retried."
                        },
                    )
                except Exception as ex:
                    # eat the exception and continue with the execution
                    errors[branch] = str(ex)
                    sentry_integration.send_to_sentry(ex)
        finally:
            # remove temporary dist-git clone after we're done here - context:
            # 1. the dist-git repo is cloned on worker, not sandbox
            # 2. it's stored in /tmp, not in the mirrored sandbox PV
            # 3. it's not being cleaned up and it wastes pod's filesystem space
            shutil.rmtree(self.api.dg.local_project.working_dir)

        if errors:
            branch_errors = ""
            for branch, err in sorted(
                errors.items(), key=lambda branch_error: branch_error[0]
            ):
                err_without_new_lines = err.replace("\n", " ")
                branch_errors += f"| `{branch}` | `{err_without_new_lines}` |\n"

            msg_retrigger = MSG_RETRIGGER.format(
                job="update", command="propose-downstream", place="issue"
            )
            body_msg = (
                f"Packit failed on creating pull-requests in dist-git:\n\n"
                f"| dist-git branch | error |\n"
                f"| --------------- | ----- |\n"
                f"{branch_errors}\n\n"
                f"{msg_retrigger}\n"
            )

            self.project.create_issue(
                title=f"[packit] Propose downstream failed for release {self.data.tag_name}",
                body=body_msg,
            )

            return TaskResults(
                success=False,
                details={"msg": "Propose downstream failed.", "errors": errors},
            )

        return TaskResults(success=True, details={})


@configured_as(job_type=JobType.koji_build)
@reacts_to(event=PushPagureEvent)
class DownstreamKojiBuildHandler(JobHandler):
    """
    This handler can submit a build in Koji from a dist-git.
    """

    task_name = TaskName.downstream_koji_build

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        event: dict,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            event=event,
        )
        self.dg_branch = event.get("git_ref")

    def pre_check(self) -> bool:
        if self.data.event_type in (PushPagureEvent.__name__,):
            if self.data.git_ref != (
                configured_branch := self.job_config.metadata.branch or "main"
            ):
                logger.info(
                    f"Skipping build on '{self.data.git_ref}'. "
                    f"Koji build configured only for '{configured_branch}'."
                )
                return False
        return True

    def run(self) -> TaskResults:
        self.local_project = LocalProject(
            git_project=self.project,
            working_dir=self.service_config.command_handler_work_dir,
            cache=RepositoryCache(
                cache_path=self.service_config.repository_cache,
                add_new=self.service_config.add_repositories_to_repository_cache,
            )
            if self.service_config.repository_cache
            else None,
        )
        packit_api = PackitAPI(
            self.service_config,
            self.job_config,
            downstream_local_project=self.local_project,
            stage=self.service_config.use_stage(),
        )
        try:
            packit_api.build(
                dist_git_branch=self.dg_branch,
                scratch=self.job_config.metadata.scratch,
                nowait=True,
                from_upstream=False,
            )
        except PackitException as ex:
            packit_api.downstream_local_project.git_project.commit_comment(
                commit=packit_api.downstream_local_project.commit_hexsha,
                body="Koji build failed:\n"
                "```\n"
                "{ex}\n"
                "```\n\n"
                f"*Get in [touch with us]({CONTACTS_URL}) if you need some help.*",
            )
            raise ex
        return TaskResults(success=True, details={})
