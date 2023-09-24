# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import logging
from typing import Union, Optional, Dict

from packit.config import JobConfigTriggerType
from packit.utils import nested_get

from ogr.abstract import GitProject
from ogr.services.pagure import PagureProject

from packit_service.config import ServiceConfig
from packit_service.constants import KojiBuildState, KojiTaskState
from packit_service.models import (
    AbstractProjectObjectDbType,
    KojiBuildTargetModel,
    KojiBuildGroupModel,
    PullRequestModel,
    ProjectReleaseModel,
    ProjectEventModel,
    GitBranchModel,
    PipelineModel,
)
from packit_service.worker.events.event import (
    use_for_job_config_trigger,
    AbstractResultEvent,
)

logger = logging.getLogger(__name__)


class AbstractKojiEvent(AbstractResultEvent):
    def __init__(
        self,
        build_id: int,
        rpm_build_task_id: Optional[int] = None,
    ):
        super().__init__()
        self.build_id = build_id
        self.rpm_build_task_id = rpm_build_task_id

        # Lazy properties
        self._target: Optional[str] = None
        self._build_model: Optional[KojiBuildTargetModel] = None
        self._build_model_searched = False

    @property
    def build_model(self) -> Optional[KojiBuildTargetModel]:
        if not self._build_model_searched and not self._build_model:
            self._build_model = KojiBuildTargetModel.get_by_build_id(
                build_id=self.build_id
            )
            self._build_model_searched = True
        return self._build_model

    def get_db_project_object(self) -> Optional[AbstractProjectObjectDbType]:
        return self.build_model.get_project_event_object() if self.build_model else None

    def get_db_project_event(self) -> Optional[ProjectEventModel]:
        return self.build_model.get_project_event_model() if self.build_model else None

    @property
    def target(self) -> Optional[str]:
        if not self._target and self.build_model:
            self._target = self.build_model.target
        return self._target

    @staticmethod
    def get_koji_rpm_build_web_url(
        rpm_build_task_id: int,
        koji_web_url: str = "https://koji.fedoraproject.org",
    ) -> str:
        """
        Constructs the web URL for the given Koji task.
        You can redefine the Koji instance using the one defined in the service config.
        """
        return f"{koji_web_url}/koji/taskinfo?taskID={rpm_build_task_id}"

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result.pop("_build_model")
        result.pop("_build_model_searched")
        return result


@use_for_job_config_trigger(trigger_type=JobConfigTriggerType.commit)
class KojiBuildEvent(AbstractKojiEvent):
    def __init__(
        self,
        build_id: int,
        state: KojiBuildState,
        package_name: str,
        branch_name: str,
        commit_sha: str,
        namespace: str,
        repo_name: str,
        project_url: str,
        epoch: str,
        version: str,
        release: str,
        rpm_build_task_id: int,
        web_url: Optional[str] = None,
        old_state: Optional[KojiBuildState] = None,
    ):
        super().__init__(build_id)
        self.state = state
        self.old_state = old_state
        self.package_name = package_name
        self.rpm_build_task_id = rpm_build_task_id
        self.epoch = epoch
        self.version = version
        self.release = release
        self.web_url = web_url

        self.branch_name = branch_name
        self._commit_sha = commit_sha  # we know  it, no need to get it from db
        self.namespace = namespace
        self.repo_name = repo_name
        self.project_url = project_url

    @property
    def commit_sha(self) -> Optional[str]:  # type:ignore
        # mypy does not like properties
        return self._commit_sha

    @property
    def nvr(self) -> str:
        return f"{self.package_name}-{self.version}-{self.release}"

    @property
    def build_model(self) -> Optional[KojiBuildTargetModel]:
        if not super().build_model:
            _, event = ProjectEventModel.add_branch_push_event(
                branch_name=self.branch_name,
                repo_name=self.repo_name,
                namespace=self.namespace,
                project_url=self.project_url,
                commit_sha=self._commit_sha,
            )
            group = KojiBuildGroupModel.create(
                run_model=PipelineModel.create(
                    project_event=event, package_name=self.package_name
                )
            )
            self._build_model = KojiBuildTargetModel.create(
                build_id=str(self.build_id),
                web_url=self.web_url,
                target="noarch",  # TODO: where to get this info from?
                status=self.state.value,
                scratch=True,  # used by the event for scratch builds
                koji_build_group=group,
            )
        return self._build_model

    @property
    def git_ref(self) -> str:
        return self.branch_name

    @property
    def identifier(self) -> str:
        return self.branch_name

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["state"] = result["state"].value
        result["old_state"] = result["old_state"].value if self.old_state else None
        result["commit_sha"] = result.pop("_commit_sha")  # commit_sha is a property
        return result

    @classmethod
    def from_event_dict(cls, event: dict):
        return KojiBuildEvent(
            build_id=event.get("build_id"),
            state=KojiBuildState(raw_new) if (raw_new := event.get("state")) else None,
            old_state=(
                KojiBuildState(raw_old) if (raw_old := event.get("old_state")) else None
            ),
            rpm_build_task_id=event.get("rpm_build_task_id"),
            package_name=event.get("package_name"),
            project_url=event.get("project_url"),
            web_url=event.get("web_url"),
            branch_name=event.get("branch_name"),
            repo_name=event.get("repo_name"),
            namespace=event.get("namespace"),
            commit_sha=event.get("commit_sha"),
            epoch=event.get("epoch"),
            version=event.get("version"),
            release=event.get("release"),
        )

    @staticmethod
    def parse(event) -> "Optional[KojiBuildEvent]":
        if event.get("topic") != "org.fedoraproject.prod.buildsys.build.state.change":
            return None

        build_id = event.get("build_id")
        task_id = event.get("task_id")
        logger.info(f"Koji event: build_id={build_id} task_id={task_id}")

        new_state = (
            KojiBuildState.from_number(raw_new)
            if (raw_new := event.get("new")) is not None
            else None
        )
        old_state = (
            KojiBuildState.from_number(raw_old)
            if (raw_old := event.get("old")) is not None
            else None
        )

        version = event.get("version")
        epoch = event.get("epoch")

        # "release": "1.fc36"
        release = event.get("release")

        # "request": [
        #       "git+https://src.fedoraproject.org/rpms/packit.git#0eb3e12005cb18f15d3054020f7ac934c01eae08",
        #       "rawhide",
        #       {}
        #     ],
        raw_git_ref, fedora_target, _ = event.get("request")
        project_url = (
            raw_git_ref.split("#")[0].removeprefix("git+").removesuffix(".git")
        )
        package_name, commit_hash = raw_git_ref.split("/")[-1].split(".git#")
        branch_name = fedora_target.removesuffix("-candidate")

        return KojiBuildEvent(
            build_id=build_id,
            state=new_state,
            package_name=package_name,
            branch_name=branch_name,
            commit_sha=commit_hash,
            namespace="rmps",
            repo_name=package_name,
            project_url=project_url,
            epoch=epoch,
            version=version,
            release=release,
            rpm_build_task_id=task_id,
            web_url=KojiBuildEvent.get_koji_rpm_build_web_url(
                rpm_build_task_id=task_id,
                koji_web_url=ServiceConfig.get_service_config().koji_web_url,
            ),
            old_state=old_state,
        )


class KojiTaskEvent(AbstractKojiEvent):
    """
    Used for scratch builds.
    """

    def __init__(
        self,
        build_id: int,
        state: KojiTaskState,
        old_state: Optional[KojiTaskState] = None,
        rpm_build_task_id: Optional[int] = None,
        start_time: Optional[Union[int, float, str]] = None,
        completion_time: Optional[Union[int, float, str]] = None,
    ):
        super().__init__(build_id=build_id, rpm_build_task_id=rpm_build_task_id)
        self.state = state
        self.old_state = old_state
        self.start_time: Optional[Union[int, float, str]] = start_time
        self.completion_time: Optional[Union[int, float, str]] = completion_time

        # Lazy properties
        self._pr_id: Optional[int] = None
        self._identifier: Optional[str] = None
        self._git_ref: Optional[str] = None
        self._commit_sha: Optional[str] = None

    @property
    def pr_id(self) -> Optional[int]:
        if not self._pr_id and isinstance(self.db_project_object, PullRequestModel):
            self._pr_id = self.db_project_object.pr_id
        return self._pr_id

    @property
    def commit_sha(self) -> Optional[str]:  # type:ignore
        # mypy does not like properties
        if not self.build_model:
            return None

        if not self._commit_sha:
            self._commit_sha = self.build_model.commit_sha
        return self._commit_sha

    @property
    def git_ref(self) -> str:
        if not self._git_ref:
            if isinstance(self.db_project_object, PullRequestModel):
                self._git_ref = self.commit_sha
            elif isinstance(self.db_project_object, ProjectReleaseModel):
                self._git_ref = self.db_project_object.tag_name
            elif isinstance(self.db_project_object, GitBranchModel):
                self._git_ref = self.db_project_object.name
            else:
                self._git_ref = self.commit_sha
        return self._git_ref

    @property
    def identifier(self) -> str:
        if not self._identifier:
            if isinstance(self.db_project_object, PullRequestModel):
                self._identifier = str(self.db_project_object.pr_id)
            elif isinstance(self.db_project_object, ProjectReleaseModel):
                self._identifier = self.db_project_object.tag_name
            elif isinstance(self.db_project_object, GitBranchModel):
                self._identifier = self.db_project_object.name
            else:
                self._identifier = self.commit_sha
        return self._identifier

    @classmethod
    def from_event_dict(cls, event: dict):
        return KojiTaskEvent(
            build_id=event.get("build_id"),
            state=KojiTaskState(event.get("state")) if event.get("state") else None,
            old_state=(
                KojiTaskState(event.get("old_state"))
                if event.get("old_state")
                else None
            ),
            rpm_build_task_id=event.get("rpm_build_task_id"),
            start_time=event.get("start_time"),
            completion_time=event.get("completion_time"),
        )

    def get_base_project(self) -> Optional[GitProject]:
        if self.pr_id is not None:
            if isinstance(self.project, PagureProject):
                pull_request = self.project.get_pr(pr_id=self.pr_id)
                return self.project.service.get_project(
                    namespace=self.project.namespace,
                    username=pull_request.author,
                    repo=self.project.repo,
                    is_fork=True,
                )
            else:
                return None  # With Github app, we cannot work with fork repo
        return self.project

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["state"] = result["state"].value
        result["old_state"] = result["old_state"].value
        result["commit_sha"] = self.commit_sha
        result["pr_id"] = self.pr_id
        result["git_ref"] = self.git_ref
        result["identifier"] = self.identifier
        return result

    @staticmethod
    def get_koji_build_logs_url(
        rpm_build_task_id: int,
        koji_logs_url: str = "https://kojipkgs.fedoraproject.org",
    ) -> str:
        """
        Constructs the log URL for the given Koji task.
        You can redefine the Koji instance using the one defined in the service config.
        TODO: this does not work for non-scratch builds
        """
        return (
            f"{koji_logs_url}//work/tasks/"
            f"{rpm_build_task_id % 10000}/{rpm_build_task_id}/build.log"
        )

    @staticmethod
    def parse(event) -> "Optional[KojiTaskEvent]":
        if event.get("topic") != "org.fedoraproject.prod.buildsys.task.state.change":
            return None

        build_id = event.get("id")
        logger.info(f"Koji event: build_id={build_id}")

        state = nested_get(event, "info", "state")

        if not state:
            logger.debug("Cannot find build state.")
            return None

        state_enum = KojiTaskState(event.get("new")) if "new" in event else None
        old_state = KojiTaskState(event.get("old")) if "old" in event else None

        start_time = nested_get(event, "info", "start_time")
        completion_time = nested_get(event, "info", "completion_time")

        rpm_build_task_id = None
        for children in nested_get(event, "info", "children", default=[]):
            if children.get("method") == "buildArch":
                rpm_build_task_id = children.get("id")
                break

        return KojiTaskEvent(
            build_id=build_id,
            state=state_enum,
            old_state=old_state,
            start_time=start_time,
            completion_time=completion_time,
            rpm_build_task_id=rpm_build_task_id,
        )
