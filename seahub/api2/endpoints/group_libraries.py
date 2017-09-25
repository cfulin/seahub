# Copyright (c) 2012-2016 Seafile Ltd.
import logging

from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

import seaserv
from seaserv import seafile_api, ccnet_api

from constance import config

from seahub.api2.utils import api_error
from seahub.api2.throttling import UserRateThrottle
from seahub.api2.authentication import TokenAuthentication
from seahub.api2.endpoints.utils import api_check_group

from seahub.utils import is_org_context, is_valid_dirent_name, \
        send_perm_audit_msg
from seahub.signals import repo_created
from seahub.group.utils import is_group_member, is_group_admin
from seahub.share.utils import is_repo_admin
from seahub.utils.timeutils import timestamp_to_isoformat_timestr
from seahub.share.models import ExtraGroupsSharePermission

from seahub.base.templatetags.seahub_tags import email2nickname, \
        email2contact_email

logger = logging.getLogger(__name__)

def get_group_repo_info(group_repo):
    group_repo_info = {}
    group_repo_info['repo_id'] = group_repo.repo_id
    group_repo_info['repo_name'] = group_repo.name

    group_repo_info['mtime'] = timestamp_to_isoformat_timestr(group_repo.last_modified)
    group_repo_info['permission'] = group_repo.permission
    group_repo_info['size'] = group_repo.size
    group_repo_info['encrypted'] = group_repo.encrypted

    repo_owner = group_repo.user
    group_repo_info['owner_email'] = repo_owner
    group_repo_info['owner_name'] = email2nickname(repo_owner)
    group_repo_info['owner_contact_name'] = email2contact_email(repo_owner)

    modifier = group_repo.last_modifier
    group_repo_info['modifier_email'] = modifier
    group_repo_info['modifier_name'] = email2nickname(modifier)
    group_repo_info['modifier_contact_email'] = email2contact_email(modifier)

    return group_repo_info

class GroupLibraries(APIView):
    authentication_classes = (TokenAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (UserRateThrottle,)

    @api_check_group # check whether group exists or not
    def get(self, request, group_id):
        """ Get all group libraries.

        Permission checking:
        1. is group member;
        """

        # only group member can get group libraries
        if not is_group_member(group_id, request.user.username):
            error_msg = 'Permission denied.'
            return api_error(status.HTTP_403_FORBIDDEN, error_msg)

        if is_org_context(request):
            org_id = request.user.org.org_id
            group_repos = seafile_api.get_org_group_repos(org_id, group_id)
        else:
            group_repos = seafile_api.get_repos_by_group(group_id)

        group_repos.sort(lambda x, y: cmp(y.last_modified, x.last_modified))

        result = []
        for repo in group_repos:
            group_repo_info = get_group_repo_info(repo)
            result.append(group_repo_info)

        return Response(result)

    @api_check_group
    def post(self, request, group_id):
        """ Add a group library.

        Permission checking:
        1. role permission, can_add_repo;
        1. is group member;
        """

        # argument check
        repo_name = request.data.get("repo_name", None)
        if not repo_name or \
                not is_valid_dirent_name(repo_name):
            error_msg = "repo_name invalid."
            return api_error(status.HTTP_400_BAD_REQUEST, error_msg)

        password = request.data.get("password", None)
        if password and not config.ENABLE_ENCRYPTED_LIBRARY:
            error_msg = 'NOT allow to create encrypted library.'
            return api_error(status.HTTP_403_FORBIDDEN, error_msg)

        permission = request.data.get('permission', 'rw')
        if permission not in ('r', 'rw'):
            error_msg = 'permission invalid.'
            return api_error(status.HTTP_400_BAD_REQUEST, error_msg)

        # permission check
        if not request.user.permissions.can_add_repo():
            error_msg = 'Permission denied.'
            return api_error(status.HTTP_403_FORBIDDEN, error_msg)

        if not is_group_member(group_id, request.user.username):
            error_msg = 'Permission denied.'
            return api_error(status.HTTP_403_FORBIDDEN, error_msg)

        # create group repo
        org_id = -1
        group_id = int(group_id)
        username = request.user.username

        if is_org_context(request):
            org_id = request.user.org.org_id
            repo_id = seafile_api.create_org_repo(repo_name,
                    '', username, password, org_id)

            seafile_api.add_org_group_repo(repo_id,
                    org_id, group_id, username, permission)
        else:
            repo_id = seafile_api.create_repo(repo_name,
                    '', username, password)

            seafile_api.set_group_repo(repo_id,
                    group_id, username, permission)

        library_template = request.data.get("library_template", '')
        repo_created.send(sender=None,
                          org_id=org_id,
                          creator=username,
                          repo_id=repo_id,
                          repo_name=repo_name,
                          library_template=library_template)

        # TODO, new seafile api for getting info of a single group library
        repo = seafile_api.get_repo(repo_id)
        group_repo_info = {}
        group_repo_info['repo_id'] = repo.id
        group_repo_info['repo_name'] = repo.name

        group_repo_info['mtime'] = timestamp_to_isoformat_timestr(repo.last_modified)
        group_repo_info['permission'] = permission
        group_repo_info['size'] = repo.size
        group_repo_info['encrypted'] = repo.encrypted

        group_repo_info['owner_email'] = username
        group_repo_info['owner_name'] = email2nickname(username)
        group_repo_info['owner_contact_name'] = email2contact_email(username)

        modifier = repo.last_modifier
        group_repo_info['modifier_email'] = modifier
        group_repo_info['modifier_name'] = email2nickname(modifier)
        group_repo_info['modifier_contact_email'] = email2contact_email(modifier)

        return Response(group_repo_info)

class GroupLibrary(APIView):
    authentication_classes = (TokenAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (UserRateThrottle,)

    @api_check_group
    def delete(self, request, group_id, repo_id):
        """ Delete a group library.

        Permission checking:
        1. is group admin;
        1. is repo owner;
        1. repo is shared to group with `admin` permission;
        """

        repo = seafile_api.get_repo(repo_id)
        if not repo:
            error_msg = 'Library %s not found.' % repo_id
            return api_error(status.HTTP_404_NOT_FOUND, error_msg)

        # only group admin or repo owner can delete group repo.
        group_id = int(group_id)
        username = request.user.username
        repo_owner = seafile_api.get_repo_owner(repo_id)

        if not is_group_admin(group_id, username) and \
                repo_owner != username and \
                not is_repo_admin(username, repo_id):
            error_msg = 'Permission denied.'
            return api_error(status.HTTP_403_FORBIDDEN, error_msg)

        if ccnet_api.is_org_group(group_id):
            org_id = ccnet_api.get_org_id_by_group(group_id)
            # TODO seafile_api.del_org_group_repo(repo_id, org_id, group_id)
            seaserv.del_org_group_repo(repo_id, org_id, group_id)
        else:
            seafile_api.unset_group_repo(repo_id, group_id, username)

        # delete extra share permission
        ExtraGroupsSharePermission.objects.delete_share_permission(repo_id, group_id)

        repo = seafile_api.get_repo(repo_id)
        origin_repo_id = repo.origin_repo_id or repo_id
        origin_path = repo.origin_path or '/'

        # TODO  seafile_api.get_single_group_library
        send_perm_audit_msg('delete-repo-perm', username, group_id,
                origin_repo_id, origin_path, '')

        return Response({'success': True})
