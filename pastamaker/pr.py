# -*- encoding: utf-8 -*-
#
# Copyright © 2017 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import logging
import re
import time

import github

from pastamaker import config
from pastamaker import webhack

LOG = logging.getLogger(__name__)


def pretty(self):
    return "%s/%s/pull/%s@%s (%s, %s)" % (
        self.base.user.login, self.base.repo.name,
        self.number, self.base.ref, self.mergeable_state or "none",
        self.approvals)


@property
def mergeable_state_computed(self):
    return self.mergeable_state not in ["unknown", None]


@property
def approvals(self):
    if not hasattr(self, "_pastamaker_approvals"):
        allowed = [u.id for u in self.base.repo.get_collaborators()]

        users = set()
        fast_merge = False
        for review in self.get_reviews():
            if review.user.id not in allowed:
                continue
            elif review.state == 'APPROVED':
                if re.search("@pastamaker.*fast merge", review.body.lower()):
                    fast_merge = True
                users.add(review.user.login)
            elif review.state in ["DISMISSED", "CHANGES_REQUESTED"]:
                if review.user.login in users:
                    users.remove(review.user.login)
                    fast_merge = False
            elif review.state == 'COMMENTED':
                pass
            else:
                LOG.error("%s FIXME review state unhandled: %s",
                          self.pretty(), review.state)

        if fast_merge and len(users) >= 1:
            self._pastamaker_approvals = 42
        else:
            self._pastamaker_approvals = len(users)
    return self._pastamaker_approvals


@property
def approved(self):
    repo = self.base.repo.full_name
    branch = self.base.ref
    for name in ["%s@%s" % (repo, branch), repo, "-@%s" % branch, "default"]:
        if name in config.REQUIRED_APPROVALS:
            required = int(config.REQUIRED_APPROVALS[name])
            break
    else:
        required = int(config.REQUIRED_APPROVALS_DEFAULT)
    return self.approvals >= required


@property
def ci_status(self):
    # Only work for PR with less than 250 commites
    if not hasattr(self, "_pastamaker_ci_status"):
        self._pastamaker_ci_status = (list(self.get_commits())
                                      [-1].get_combined_status().state)
    return self._pastamaker_ci_status


@property
def pastamaker_priority(self):
    if not hasattr(self, "_pastamaker_priority"):
        if not self.approved:
            priority = -1
        elif (self.mergeable_state == "clean"
              and self.ci_status == "success"
              and self.update_branch_state == "clean"):
            # Best PR ever, up2date and CI OK
            priority = 11
        elif self.mergeable_state == "clean":
            priority = 10
        elif (self.mergeable_state == "blocked"
              and self.ci_status == "pending"
              and self.update_branch_state == "clean"):
            # Maybe clean soon, or maybe this is the previous run
            # selected PR that we just rebase
            priority = 10
        elif (self.mergeable_state == "behind"
              and self.update_branch_state not in ["unknown", "dirty"]):
            # Not up2date, but ready to merge, is branch updatable
            if self.ci_status == "success":
                priority = 7
            elif self.ci_status == "pending":
                priority = 5
            else:
                priority = -1
        else:
            priority = -1
        setattr(self, "_pastamaker_priority", priority)
        LOG.info("%s prio: %s, %s, %s, %s, %s", self.pretty(), priority,
                 self.approved, self.mergeable_state, self.ci_status,
                 self.update_branch_state)
    return self._pastamaker_priority


def pastamaker_update(self, force=False):
    for attr in ["_pastamaker_priority",
                 "_pastamaker_ci_status",
                 "_pastamaker_approvals"]:
        if hasattr(self, attr):
            delattr(self, attr)

    if not force and self.mergeable_state_computed:
        return self

    # Github is currently processing this PR, we wait the completion
    while True:
        LOG.info("%s, refreshing...", self.pretty())
        self.update()
        if self.mergeable_state_computed:
            break
        time.sleep(0.42)  # you known, this one always work

    LOG.info("%s, refreshed", self.pretty())
    return self


def pastamaker_merge(self, **post_parameters):
    post_parameters["sha"] = self.head.sha
    # FIXME(sileht): use self.merge when it will
    # support sha and merge_method arguments
    try:
        post_parameters['merge_method'] = "rebase"
        headers, data = self._requester.requestJsonAndCheck(
            "PUT", self.url + "/merge", input=post_parameters)
        return github.PullRequestMergeStatus.PullRequestMergeStatus(
            self._requester, headers, data, completed=True)
    except github.GithubException as e:
        if e.data["message"] != "This branch can't be rebased":
            LOG.exception("%s merge fail: %d, %s",
                          self.pretty(), e.status, e.data["message"])
            return

        # If rebase fail retry with merge
        post_parameters['merge_method'] = "merge"
        try:
            headers, data = self._requester.requestJsonAndCheck(
                "PUT", self.url + "/merge", input=post_parameters)
            return github.PullRequestMergeStatus.PullRequestMergeStatus(
                self._requester, headers, data, completed=True)
        except github.GithubException as e:
            LOG.exception("%s merge fail: %d, %s",
                          self.pretty(), e.status, e.data["message"])

        # FIXME(sileht): depending on the kind of failure we can endloop
        # to try to merge the pr again and again.
        # to repoduce the issue


def from_event(repo, data):
    # TODO(sileht): do it only once in handle()
    # NOTE(sileht): Convert event payload, into pygithub object
    # instead of querying the API
    if "pull_request" in data:
        return github.PullRequest.PullRequest(
            repo._requester, {}, data["pull_request"], completed=True)


def monkeypatch_github():
    p = github.PullRequest.PullRequest
    p.pretty = pretty
    p.mergeable_state_computed = mergeable_state_computed
    p.approved = approved
    p.approvals = approvals
    p.ci_status = ci_status
    p.pastamaker_update = pastamaker_update
    p.pastamaker_merge = pastamaker_merge
    p.pastamaker_priority = pastamaker_priority

    # Missing Github API
    p.update_branch = webhack.web_github_update_branch
    p.update_branch_state = webhack.web_github_branch_status
