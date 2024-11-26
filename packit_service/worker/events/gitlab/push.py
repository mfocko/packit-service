# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

from packit_service.service.db_project_events import (
    AddBranchPushEventToDb,
)
from packit_service.worker.events.gitlab.abstract import GitlabEvent


class Push(AddBranchPushEventToDb, GitlabEvent):
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


class TagPush(AddBranchPushEventToDb, GitlabEvent):
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