# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import logging
from datetime import datetime
from typing import Optional, Dict

from ogr.abstract import GitProject
from ogr.services.pagure import PagureProject

from packit_service.models import (
    TestingFarmResult,
    AbstractProjectObjectDbType,
    PullRequestModel,
    TFTTestRunTargetModel,
    ProjectEventModel,
)
from packit_service.worker.events.event import AbstractResultEvent
from packit_service.worker.helpers.testing_farm import TestingFarmJobHelper

logger = logging.getLogger(__name__)


class TestingFarmResultsEvent(AbstractResultEvent):
    __test__ = False

    def __init__(
        self,
        pipeline_id: str,
        result: TestingFarmResult,
        compose: str,
        summary: str,
        log_url: str,
        copr_build_id: str,
        copr_chroot: str,
        commit_sha: str,
        project_url: str,
        created: datetime,
        identifier: Optional[str] = None,
    ):
        super().__init__(project_url=project_url)
        self.pipeline_id: str = pipeline_id
        self.result: TestingFarmResult = result
        self.compose: str = compose
        self.summary: str = summary
        self.log_url: str = log_url
        self.copr_build_id: str = copr_build_id
        self.copr_chroot: str = copr_chroot
        self.commit_sha: str = commit_sha
        self.created: datetime = created
        self.identifier: Optional[str] = identifier

        # Lazy properties
        self._pr_id: Optional[int] = None
        self._db_project_object: Optional[AbstractProjectObjectDbType] = None
        self._db_project_event: Optional[ProjectEventModel] = None

    @property
    def pr_id(self) -> Optional[int]:
        if not self._pr_id and isinstance(self.db_project_object, PullRequestModel):
            self._pr_id = self.db_project_object.pr_id
        return self._pr_id

    def get_dict(self, default_dict: Optional[Dict] = None) -> dict:
        result = super().get_dict()
        result["result"] = result["result"].value
        result["pr_id"] = self.pr_id
        return result

    def get_db_project_object(self) -> Optional[AbstractProjectObjectDbType]:
        run_model = TFTTestRunTargetModel.get_by_pipeline_id(
            pipeline_id=self.pipeline_id
        )
        return run_model.get_project_event_object() if run_model else None

    def get_db_project_event(self) -> Optional[ProjectEventModel]:
        run_model = TFTTestRunTargetModel.get_by_pipeline_id(
            pipeline_id=self.pipeline_id
        )
        return run_model.get_project_event_model() if run_model else None

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

    @staticmethod
    def parse(event) -> "Optional[TestingFarmResultsEvent]":
        """this corresponds to testing farm results event"""
        if event.get("source") != "testing-farm" or not event.get("request_id"):
            return None

        request_id: str = event["request_id"]
        logger.info(f"Testing farm notification event. Request ID: {request_id}")

        tft_test_run = TFTTestRunTargetModel.get_by_pipeline_id(request_id)

        # Testing Farm sends only request/pipeline id in a notification.
        # We need to get more details ourselves.
        # It'd be much better to do this in TestingFarmResultsHandler.run(),
        # but all the code along the way to get there expects we already know the details.
        # TODO: Get missing info from db instead of querying TF
        event = TestingFarmJobHelper.get_request_details(request_id)
        if not event:
            # Something's wrong with TF, raise exception so that we can re-try later.
            raise Exception(f"Failed to get {request_id} details from TF.")

        (
            project_url,
            ref,
            result,
            summary,
            copr_build_id,
            copr_chroot,
            compose,
            log_url,
            created,
            identifier,
        ) = TestingFarmJobHelper.parse_data(tft_test_run, event)

        logger.debug(
            f"project_url: {project_url}, ref: {ref}, result: {result}, "
            f"summary: {summary!r}, copr-build: {copr_build_id}:{copr_chroot},\n"
            f"log_url: {log_url}"
        )

        return TestingFarmResultsEvent(
            pipeline_id=request_id,
            result=result,
            compose=compose,
            summary=summary,
            log_url=log_url,
            copr_build_id=copr_build_id,
            copr_chroot=copr_chroot,
            commit_sha=ref,
            project_url=project_url,
            created=created,
            identifier=identifier,
        )
