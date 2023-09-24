# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT
from logging import getLogger
from os import getenv
from typing import Optional, Dict

from ogr.abstract import GitProject
from ogr.parsing import RepoUrl
from packit.constants import DISTGIT_INSTANCES
from packit.utils import nested_get

from packit.config import PackageConfig, JobConfigTriggerType
from packit_service.config import ServiceConfig, PackageConfigGetter
from packit_service.models import ProjectReleaseModel, ProjectEventModel
from packit_service.worker.events import Event
from packit_service.worker.events.event import use_for_job_config_trigger

logger = getLogger(__name__)


# the decorator is needed in case the DB project event is not created (not valid arguments)
# but we still want to report from pre_check of the PullFromUpstreamHandler
@use_for_job_config_trigger(trigger_type=JobConfigTriggerType.release)
class NewHotnessUpdateEvent(Event):
    def __init__(
        self,
        package_name: str,
        version: str,
        distgit_project_url: str,
    ):
        super().__init__()
        self.package_name = package_name
        self.version = version
        self.distgit_project_url = distgit_project_url

        self._repo_url: Optional[RepoUrl] = None
        self._db_project_object: Optional[ProjectReleaseModel]
        self._db_project_event: Optional[ProjectEventModel]

    @property
    def project(self):
        if not self._project:
            self._project = self.get_project()
        return self._project

    def get_project(self) -> Optional[GitProject]:
        if not self.distgit_project_url:
            return None

        return ServiceConfig.get_service_config().get_project(
            url=self.distgit_project_url
        )

    @property
    def base_project(self):
        return None

    def _add_release_and_event(self):
        if not self._db_project_object or not self._db_project_event:
            if not (
                self.tag_name
                and self.repo_name
                and self.repo_namespace
                and self.project_url
            ):
                logger.info(
                    "Not going to create the DB project event, not valid arguments."
                )
                return None

            (
                self._db_project_object,
                self._db_project_event,
            ) = ProjectEventModel.add_release_event(
                tag_name=self.tag_name,
                namespace=self.repo_namespace,
                repo_name=self.repo_name,
                project_url=self.project_url,
                commit_hash=None,
            )

    @property
    def db_project_object(self) -> Optional[ProjectReleaseModel]:
        if not self._db_project_object:
            self._add_release_and_event()
        return self._db_project_object

    @property
    def db_project_event(self) -> Optional[ProjectEventModel]:
        if not self._db_project_event:
            self._add_release_and_event()
        return self._db_project_event

    @property
    def packages_config(self):
        if not self._package_config_searched and not self._package_config:
            self._package_config = self.get_packages_config()
            self._package_config_searched = True
        return self._package_config

    def get_packages_config(self) -> Optional[PackageConfig]:
        logger.debug(f"Getting package_config:\n" f"\tproject: {self.project}\n")

        package_config = PackageConfigGetter.get_package_config_from_repo(
            base_project=None,
            project=self.project,
            pr_id=None,
            reference=None,
            fail_when_missing=False,
        )

        return package_config

    @property
    def project_url(self) -> Optional[str]:
        return (
            self.packages_config.upstream_project_url if self.packages_config else None
        )

    @property
    def repo_url(self) -> Optional[RepoUrl]:
        if not self._repo_url:
            self._repo_url = RepoUrl.parse(self.project_url)
        return self._repo_url

    @property
    def repo_namespace(self) -> Optional[str]:
        return self.repo_url.namespace if self.repo_url else None

    @property
    def repo_name(self) -> Optional[str]:
        return self.repo_url.repo if self.repo_url else None

    @property
    def tag_name(self):
        if not (self.packages_config and self.packages_config.upstream_tag_template):
            return self.version

        return self.packages_config.upstream_tag_template.format(version=self.version)

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        d = self.__dict__
        d["project_url"] = self.project_url
        d["tag_name"] = self.tag_name
        d["repo_name"] = self.repo_name
        d["repo_namespace"] = self.repo_namespace
        result = super().get_dict(d)
        result.pop("_repo_url")
        return result

    @staticmethod
    def parse(event) -> "Optional[NewHotnessUpdateEvent]":
        if "hotness.update.bug.file" not in event.get("topic", ""):
            return None

        # "package" should contain the Fedora package name directly
        # see https://github.com/fedora-infra/the-new-hotness/blob/
        # 363acd33623dadd5fc3b60a83a528926c7c21fc1/hotness/hotness_consumer.py#L385
        # and https://github.com/fedora-infra/the-new-hotness/blob/
        # 363acd33623dadd5fc3b60a83a528926c7c21fc1/hotness/hotness_consumer.py#L444-L455
        #
        # we could get it also like this:
        # [package["package_name"]
        #   for package in event["trigger"]["msg"]["message"]["packages"]
        #   if package["distro"] == "Fedora"][0]
        package_name = event.get("package")
        dg_base_url = getenv("DISTGIT_URL", DISTGIT_INSTANCES["fedpkg"].url)

        distgit_project_url = f"{dg_base_url}rpms/{package_name}"

        version = nested_get(event, "trigger", "msg", "project", "version")

        logger.info(
            f"New hotness update event for package: {package_name}, version: {version}"
        )

        return NewHotnessUpdateEvent(
            package_name=package_name,
            version=version,
            distgit_project_url=distgit_project_url,
        )
